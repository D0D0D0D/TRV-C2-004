"""AgentCore Platform v1.0"""

# TRV-C2-004 inner workflow entry node.
#
# The inner graph receives only user_input (a JSON string serialized by the
# outer GraphNode.extract_input()) — custom outer-state fields do NOT cross
# the subgraph boundary. This node re-hydrates mode / tenant_id /
# request_params into the inner State before the mode branch.

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.common import upstream_error


class UnpackNode(FunctionNode):
    """Parse the serialized outer request into inner State fields."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if upstream_error(state):
            return {}
        raw = state.get("user_input", "")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except (json.JSONDecodeError, ValueError):
            payload = {}
        if not isinstance(payload, dict) or not payload.get("tenant_id"):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["UnpackNode: malformed inner payload (mode/tenant_id missing)"],
            }
        mode = str(payload.get("mode", "generate"))
        emit_trace_event("inner_request_unpacked", {"mode": mode}, state)
        return {
            "mode": mode,
            "tenant_id": str(payload["tenant_id"]),
            "request_params": payload.get("request_params", {}),
            "revision_requests": [],
            "revision_round": 0,
            "status": AgentStatus.SUCCESS.value,
        }
