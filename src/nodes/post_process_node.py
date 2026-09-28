"""AgentCore Platform v1.0"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.common import upstream_error


class PostProcessNode(FunctionNode):
    """Format the mode-specific result into the final structured output."""

    # S-1: emits facility-proprietary drafts — standard business operation.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if upstream_error(state):
            emit_trace_event("output_skipped_upstream_error", {}, state)
            return {}
        result = state.get("result") or {}
        if not isinstance(result, dict):
            result = {"raw": result}
        mode = result.get("mode") or state.get("mode", "generate")
        formatted: dict = {"template_id": "TRV-C2-004", "mode": mode}
        if mode == "generate":
            plans = result.get("plans") or []
            price_stats = result.get("price_stats") or {}
            formatted["plans"] = plans
            formatted["plan_count"] = len(plans)
            formatted["revision_rounds"] = result.get("revision_rounds", 0)
            formatted["price_note"] = (
                "価格レンジは自施設の過去プラン実績に基づく参考値です。"
                if price_stats.get("sample_size")
                else "過去プラン実績が無いため価格は未提示です（実績蓄積後に提示されます）。"
            )
        elif mode == "ingest":
            formatted["ingest_result"] = result.get("ingest_result") or {}
        elif mode == "feedback":
            formatted["feedback_result"] = result.get("feedback_result") or {}
        emit_trace_event("output_formatted", {"mode": mode}, state)
        # _tenant_id is an internal marker consumed (and stripped) by the S-3
        # gate below — it never reaches the merged state or the caller.
        return {"result": formatted, "status": AgentStatus.SUCCESS.value,
                "_tenant_id": str(state.get("tenant_id") or "")}

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain check: no tenant identifiers leaked into plan text.

        The default credential scan already ran; this guards against the
        internal tenant_id string appearing verbatim inside customer-facing
        plan drafts (cross-boundary metadata leak). The _tenant_id marker set
        by execute() is popped here so it never leaves the gate.
        """
        tenant = str(result.pop("_tenant_id", "") or "")
        formatted = result.get("result")
        if tenant and isinstance(formatted, dict):
            plans_text = json.dumps(formatted.get("plans", []), ensure_ascii=False, default=str)
            if tenant in plans_text:
                raise RuntimeError("S-3 output gate: tenant_id leaked into plan text")
        return result
