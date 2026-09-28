"""AgentCore Platform v1.0"""

# TRV-C2-004 — generate-mode inner nodes ①〜⑤ (docs/02_design.md).
#
#   ① FacilityProfileNode   — structure the facility description (LLM)
#   ② ContextEnrichmentNode — season / target / trend context (deterministic)
#   ③ PastPlanRetrievalNode — vector search + DB price stats (hybrid KB)
#   ④ PlanGenerationNode    — draft N plans (LLM; price from history only)
#   ⑤ RefinementNode        — tone/typo polish (LLM) + S-3 domain check
#
# LLM / KbService instances arrive via constructor injection (never State).

from __future__ import annotations

import datetime
import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from langgraph.errors import GraphBubbleUp

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.common import upstream_error
from src.services.llm_client import parse_llm_json

_SEASON_BY_MONTH = {
    12: "冬", 1: "冬", 2: "冬",
    3: "春", 4: "春", 5: "春",
    6: "夏", 7: "夏", 8: "夏",
    9: "秋", 10: "秋", 11: "秋",
}


# Provider failures are reported as an allow-listed category, never as the provider's
# own message: `error_log` is checkpointed, and a provider message can echo the request
# content that caused the rejection. The message is read to classify and then dropped.
_LLM_ERROR_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("content_filtered", ("content_filter", "content management policy", "jailbreak", "responsible ai")),
    ("rate_limited", ("rate limit", "too many requests", "quota")),
    ("provider_unavailable", ("timeout", "timed out", "connection", "unavailable", "overloaded")),
)


def _llm_error(exc: Exception) -> str:
    """Classify a provider failure into an allow-listed, payload-free category.

    Returns e.g. `content_filtered [BadRequestError] (HTTP 400)`. The exception type
    alone is not actionable — Azure returns `BadRequestError` both for a content-filter
    block and for an ordinary malformed request — so the category carries the meaning
    while nothing derived from the request text is persisted.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()  # read for classification only; never stored
    category = ""
    for name, markers in _LLM_ERROR_CATEGORIES:
        if any(marker in text for marker in markers):
            category = name
            break
    if not category:
        if isinstance(status, int) and status == 429:
            category = "rate_limited"
        elif isinstance(status, int) and status >= 500:
            category = "provider_unavailable"
        elif isinstance(status, int) and 400 <= status < 500:
            category = "provider_rejected"
        else:
            category = "provider_error"
    suffix = f" (HTTP {status})" if isinstance(status, int) else ""
    return f"{category} [{type(exc).__name__}]{suffix}"


class FacilityProfileNode(FunctionNode):
    """① Structure raw facility text (or the facility's web page) into a profile.

    facility_text takes precedence; facility_url is fetched via the
    SSRF-hardened url_fetcher (injectable seam — tests pass a fake).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None, fetcher: Any = None) -> None:
        super().__init__()
        self._llm = llm
        self._fetcher = fetcher

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "generate":
            return {}
        params = state.get("request_params") or {}
        facility_text = str(params.get("facility_text") or "")
        if not facility_text.strip() and params.get("facility_url"):
            from src.services.url_fetcher import UrlFetchError, fetch_facility_text
            fetch = self._fetcher or fetch_facility_text
            try:
                facility_text = fetch(str(params["facility_url"]))
            except UrlFetchError as e:
                return {"status": AgentStatus.ERROR.value,
                        "error_log": [f"FacilityProfileNode: facility_url fetch failed — {e}"]}
            emit_trace_event("facility_url_fetched",
                             {"url": str(params["facility_url"])[:200],
                              "chars": len(facility_text)}, state)
        if self._llm is None:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["FacilityProfileNode: no LLM configured"]}
        try:
            response = self._llm.complete([
                {"role": "user", "content": (
                    "[TASK:profile] 次の宿泊施設情報を JSON で構造化してください。"
                    'キー: summary, strengths(list), concept, room_types(list), location。'
                    "JSON のみを返すこと。\n[FACILITY]\n" + facility_text
                )},
            ])
        except (GraphBubbleUp, SecurityViolationError):
            # HITL interrupts and security violations are the framework's to handle.
            raise
        except Exception as exc:  # noqa: BLE001 - provider rejection/outage must be reported, not raised opaquely
            emit_trace_event("facility_profile_llm_failed", {"error_type": type(exc).__name__}, state)
            return {"status": AgentStatus.ERROR.value,
                    "error_log": [f"FacilityProfileNode: LLM call failed — {_llm_error(exc)}"]}
        profile = parse_llm_json(response, fallback_key="summary")
        if not isinstance(profile, dict):
            profile = {"summary": str(profile)}
        emit_trace_event("facility_profiled", {"keys": sorted(profile.keys())}, state)
        return {"facility_profile": profile, "status": AgentStatus.SUCCESS.value}


class ContextEnrichmentNode(FunctionNode):
    """② Derive season / target / trend context from the request or calendar.

    External trend APIs are out of scope (human decision 2026-07-02) — trend
    keywords come only from the caller's request.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "generate":
            return {}
        params = state.get("request_params") or {}
        season = str(params.get("season") or "").strip()
        if not season:
            season = _SEASON_BY_MONTH[datetime.date.today().month]
        context = {
            "season": season,
            "target_segment": str(params.get("target_segment") or "").strip() or None,
            "appeal_axis": params.get("appeal_axis"),
            "price_hint": params.get("price_hint"),
            "trend_keywords": params.get("trend_keywords") or [],
            "num_plans": int(params.get("num_plans") or 3),
        }
        emit_trace_event("context_enriched", {"season": season,
                                              "target_segment": context["target_segment"]}, state)
        return {"planning_context": context, "status": AgentStatus.SUCCESS.value}


class PastPlanRetrievalNode(FunctionNode):
    """③ Hybrid retrieval: similar past plans (vector) + price stats (DB)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: Any = None) -> None:
        super().__init__()
        self._kb = kb_service

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "generate":
            return {}
        if self._kb is None:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["PastPlanRetrievalNode: no KbService configured"]}
        context = state.get("planning_context") or {}
        profile = state.get("facility_profile") or {}
        query = " ".join(filter(None, [
            str(profile.get("concept") or ""),
            str(profile.get("summary") or "")[:200],
            str(context.get("season") or ""),
            str(context.get("target_segment") or ""),
            " ".join(context.get("trend_keywords") or []),
        ]))
        references = self._kb.search_similar(
            query,
            tenant_id=state.get("tenant_id", ""),
            season=context.get("season"),
            target_segment=context.get("target_segment"),
        )
        stats = self._kb.price_stats(
            tenant_id=state.get("tenant_id", ""),
            season=context.get("season"),
            target_segment=context.get("target_segment"),
        )
        emit_trace_event("plans_retrieved",
                         {"hits": len(references), "price_sample": stats.get("sample_size", 0)},
                         state)
        return {"reference_plans": _strip_scores(references),
                "price_stats": stats,
                "status": AgentStatus.SUCCESS.value}


class PlanGenerationNode(FunctionNode):
    """④ Draft N plan candidates (LLM).

    Price ranges are proposed ONLY when historical price stats exist
    (human decision 2026-07-02); otherwise plans carry price_range: null.
    HITL revision rounds feed revision_requests back into the prompt.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "generate":
            return {}
        if self._llm is None:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["PlanGenerationNode: no LLM configured"]}
        context = state.get("planning_context") or {}
        stats = state.get("price_stats") or {}
        has_history = bool(stats.get("sample_size"))
        revisions = state.get("revision_requests") or []
        prompt = {
            "facility_profile": state.get("facility_profile") or {},
            "planning_context": context,
            "reference_plans": state.get("reference_plans") or [],
            "price_stats": stats if has_history else None,
            "revision_requests": revisions,
        }
        price_rule = (
            "price_range は price_stats の実績分布に基づく場合のみ提示。"
            if has_history else
            "実績データが無いため price_range は必ず null にする（推測価格の提示は禁止）。"
        )
        try:
            response = self._llm.complete([
                {"role": "user", "content": (
                    f"[TASK:generate] 宿泊プラン案を {context.get('num_plans', 3)} 件、JSON 配列で生成してください。"
                    "各要素のキー: title, pitch_text, experience_text, included(list), "
                    "target_segment, season, theme, price_range。"
                    + price_rule
                    + ("直近の人間レビュー修正指示を必ず反映すること。" if revisions else "")
                    + "\n[INPUT]\n" + json.dumps(prompt, ensure_ascii=False, default=str)
                )},
            ])
        except (GraphBubbleUp, SecurityViolationError):
            # HITL interrupts and security violations are the framework's to handle.
            raise
        except Exception as exc:  # noqa: BLE001 - see FacilityProfileNode
            emit_trace_event("plan_generation_llm_failed", {"error_type": type(exc).__name__}, state)
            return {"status": AgentStatus.ERROR.value,
                    "error_log": [f"PlanGenerationNode: LLM call failed — {_llm_error(exc)}"]}
        candidates = parse_llm_json(response)
        if isinstance(candidates, dict):
            candidates = [candidates]
        if not isinstance(candidates, list) or not candidates:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["PlanGenerationNode: LLM returned no parseable plans"]}
        if not has_history:
            for plan in candidates:
                if isinstance(plan, dict):
                    plan["price_range"] = None
        emit_trace_event("plans_generated",
                         {"count": len(candidates), "revision_round": state.get("revision_round", 0),
                          "price_from_history": has_history},
                         state)
        return {"plan_candidates": candidates, "status": AgentStatus.SUCCESS.value}


class RefinementNode(FunctionNode):
    """⑤ Polish tone / typos / brand consistency (LLM)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "generate":
            return {}
        candidates = state.get("plan_candidates") or []
        if self._llm is None:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["RefinementNode: no LLM configured"]}
        try:
            response = self._llm.complete([
                {"role": "user", "content": (
                    "[TASK:refine] 次の宿泊プラン案 JSON 配列を、誤字脱字・言葉遣い・トンマナ・"
                    "ブランド整合の観点で推敲し、同じスキーマの JSON 配列のみを返してください。"
                    "price_range の値は変更禁止。\n[DRAFTS]\n"
                    + json.dumps(candidates, ensure_ascii=False, default=str)
                )},
            ])
        except (GraphBubbleUp, SecurityViolationError):
            # HITL interrupts and security violations are the framework's to handle.
            raise
        except Exception as exc:  # noqa: BLE001 - polishing is additive; keep the drafts we already have
            emit_trace_event("plan_refinement_llm_failed", {"error_type": type(exc).__name__, "count": len(candidates)}, state)
            return {"plan_drafts": candidates, "status": AgentStatus.SUCCESS.value}
        drafts = parse_llm_json(response)
        if isinstance(drafts, dict):
            drafts = [drafts]
        if (not isinstance(drafts, list) or not drafts
                or not all(isinstance(d, dict) and d.get("title") for d in drafts)):
            drafts = candidates  # refinement must never lose the drafts
        emit_trace_event("plans_refined", {"count": len(drafts)}, state)
        return {"plan_drafts": drafts, "status": AgentStatus.SUCCESS.value}

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain check: block obviously unsafe expressions in drafts.

        Content-filtering variant. The default credential scan already ran on
        string values; this scans the structured drafts for a small deny-list
        (docs/02_design.md §セキュリティ設計).
        """
        banned = ("完全に安全", "絶対に儲かる", "医療効果", "病気が治る")
        for plan in result.get("plan_drafts") or []:
            text = json.dumps(plan, ensure_ascii=False, default=str)
            for word in banned:
                if word in text:
                    raise RuntimeError(
                        f"S-3 output gate: banned expression '{word}' in generated plan"
                    )
        return result


def _strip_scores(references: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not str(k).startswith("_")} for r in references]
