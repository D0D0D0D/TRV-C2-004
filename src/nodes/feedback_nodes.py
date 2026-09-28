"""AgentCore Platform v1.0"""

# TRV-C2-004 — feedback-mode inner nodes (docs/02_design.md).
#
#   ① PlanNormalizeNode — normalize the adopted plan; detect operator edits
#   ② KbAppendNode      — append/update the KB record (adopted_flag=True)

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.common import upstream_error
from src.services.kb_service import ALL_FIELDS


class PlanNormalizeNode(FunctionNode):
    """① Normalize the adopted plan into a KB record.

    operator_edited: True when the adopted plan differs from the original
    draft (request_params.original_draft, optional) — the human-improvement
    signal that feeds the learning loop.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "feedback":
            return {}
        params = state.get("request_params") or {}
        adopted = params.get("adopted_plan")
        if not isinstance(adopted, dict) or not str(adopted.get("title") or "").strip():
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["PlanNormalizeNode: adopted_plan.title is required"]}
        record = {f: adopted.get(f) for f in ("plan_id",) + tuple(ALL_FIELDS) if f in adopted}
        original = params.get("original_draft")
        operator_edited = (
            isinstance(original, dict)
            and json.dumps(original, sort_keys=True, ensure_ascii=False, default=str)
            != json.dumps(adopted, sort_keys=True, ensure_ascii=False, default=str)
        )
        emit_trace_event("feedback_normalized",
                         {"operator_edited": bool(operator_edited)}, state)
        return {"kb_records": [{**record, "operator_edited": bool(operator_edited)}],
                "status": AgentStatus.SUCCESS.value}


class KbAppendNode(FunctionNode):
    """② Append/update the adopted plan in the hybrid KB."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: Any = None) -> None:
        super().__init__()
        self._kb = kb_service

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "feedback":
            return {}
        if self._kb is None:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["KbAppendNode: no KbService configured"]}
        records = state.get("kb_records") or []
        if not records:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["KbAppendNode: nothing to append"]}
        record = records[0]
        result = self._kb.append_feedback(
            record,
            tenant_id=state.get("tenant_id", ""),
            operator_edited=bool(record.get("operator_edited")),
        )
        emit_trace_event("kb_feedback_appended", result, state)
        return {"feedback_result": result, "status": AgentStatus.SUCCESS.value}
