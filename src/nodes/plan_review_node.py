"""AgentCore Platform v1.0"""

# TRV-C2-004 — ⑥ PlanReviewNode: HITL (D6 backbone interrupt) review loop.
#
# docs/02_design.md §動作モード:
#   - interrupt() presents the refined drafts to the human reviewer.
#   - Approval intent is resolved in two stages (human decision 2026-07-02 —
#     a fixed word list alone is too deterministic for natural replies):
#       1. fast-path: unambiguous approval tokens ("approve", "承認", ...)
#       2. otherwise: LLM intent classification (approve / revise)
#     Unclassifiable feedback falls to the SAFE side = revise (never
#     auto-publish on an ambiguous reply).
#   - Revision instructions are appended to revision_requests; inner routing
#     loops back to PlanGenerationNode.
#   - hitl_draft stores the presented drafts (idempotency guard — LangGraph
#     re-executes this node from the top on resume).
#   - revision_round is a template-owned counter; hitl_count is framework-owned
#     and MUST NOT be written here (agent_state.py contract).
#   - hitl_allowed=False (or hitl disabled) → auto-approve passthrough.

from __future__ import annotations

import json
from typing import Any, ClassVar

from langgraph.types import interrupt

from framework.nodes.function_node import FunctionNode
from langgraph.errors import GraphBubbleUp

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.common import upstream_error
from src.services.llm_client import parse_llm_json

DEFAULT_MAX_REVISIONS = 8

# Stage-1 fast path: unambiguous approval tokens only. Everything else goes
# to the LLM intent classifier (stage 2).
_APPROVE_TOKENS = ("approve", "approved", "承認", "採用")


def _instructions(feedback: Any) -> str:
    if isinstance(feedback, dict):
        return str(feedback.get("instructions") or feedback.get("comment") or feedback)
    return str(feedback)


class PlanReviewNode(FunctionNode):
    """⑥ Present drafts for human review; loop on revision instructions."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, hitl_enabled: bool = True,
                 max_revisions: int = DEFAULT_MAX_REVISIONS,
                 llm: Any = None) -> None:
        super().__init__()
        self._hitl_enabled = hitl_enabled
        self._max_revisions = max_revisions
        self._llm = llm

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "generate":
            return {}
        drafts = state.get("plan_drafts") or []
        round_no = int(state.get("revision_round") or 0)

        # HITL disabled by config, denied by the caller (hitl_allowed=False),
        # or revision ceiling reached → auto-approve passthrough.
        if (not self._hitl_enabled
                or not state.get("hitl_allowed", True)
                or round_no >= self._max_revisions):
            emit_trace_event("plan_review_skipped",
                             {"reason": "hitl_disabled_or_ceiling", "revision_round": round_no},
                             state)
            return {"review_decision": "approved", "status": AgentStatus.SUCCESS.value}

        # D6: suspends here on first execution; returns feedback on resume.
        feedback = interrupt({
            "draft": drafts,
            "reason": "plan review — approve or give revision instructions",
            "revision_round": round_no,
        })

        intent = self._classify_intent(feedback, state)
        if intent == "approve":
            emit_trace_event("plan_review_approved", {"revision_round": round_no}, state)
            return {
                "review_decision": "approved",
                "hitl_draft": drafts,
                "status": AgentStatus.SUCCESS.value,
            }

        instructions = _instructions(feedback)
        emit_trace_event("plan_review_revision",
                         {"revision_round": round_no + 1,
                          "instructions_preview": instructions[:120]},
                         state)
        return {
            "review_decision": "revise",
            "revision_requests": (state.get("revision_requests") or []) + [instructions],
            "revision_round": round_no + 1,
            "hitl_draft": drafts,
            "status": AgentStatus.SUCCESS.value,
        }

    # -- intent resolution -------------------------------------------------

    def _classify_intent(self, feedback: Any, state: dict) -> str:
        """Resolve reviewer intent: 'approve' or 'revise'.

        Stage 1 — deterministic fast path for unambiguous tokens.
        Stage 2 — LLM classification for natural-language replies
                  ("いいよ、それで行こう" etc.).
        Fail-safe — anything unclassifiable is treated as revise: an
        ambiguous reply must never auto-publish the drafts.
        """
        if isinstance(feedback, bool):
            return "approve" if feedback else "revise"
        text = ""
        if isinstance(feedback, dict):
            action = str(feedback.get("action", "")).strip().lower()
            if action in _APPROVE_TOKENS:
                return "approve"
            if action in ("revise", "reject", "修正"):
                return "revise"
            if feedback.get("instructions") or feedback.get("comment"):
                return "revise"  # explicit instructions = revision request
            text = json.dumps(feedback, ensure_ascii=False, default=str)
        else:
            text = str(feedback).strip()
            if text.lower() in _APPROVE_TOKENS:
                return "approve"
        if not text:
            return "revise"
        if self._llm is None:
            return "revise"
        try:
            response = self._llm.complete([
                {"role": "user", "content": (
                    "[TASK:review_intent] 次の発話は、提示された宿泊プラン案への"
                    "レビュー応答です。「このまま採用してよい（approve）」か"
                    "「修正・変更を求めている（revise）」かを判定し、"
                    'JSON {"intent": "approve"|"revise"} のみを返してください。'
                    "判断に迷う場合は revise。\n[FEEDBACK]\n" + text[:2000]
                )},
            ])
            verdict = parse_llm_json(response)
            intent = str(verdict.get("intent", "")).lower() if isinstance(verdict, dict) else ""
            if intent in ("approve", "revise"):
                emit_trace_event("plan_review_intent_classified",
                                 {"intent": intent, "via": "llm"}, state)
                return intent
        except (GraphBubbleUp, SecurityViolationError):
            # framework control flow / security violations must not fail safe silently
            raise
        except Exception:  # noqa: BLE001 — classification failure must not crash the review
            pass
        return "revise"
