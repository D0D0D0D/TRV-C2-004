"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus values (e.g. AgentStatus.SUCCESS.value) — raw enum
#    members are msgpack-unregistered in checkpoints (LangGraph warns and
#    will block); the str value round-trips safely and route() resolves it
#  - Read input_context via state.get("input_context", {}) — read-only [C1]
#  - Never import from mediator/, api/, or other agents

from __future__ import annotations

import json
from typing import ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

VALID_MODES = ("generate", "ingest", "feedback")

# S-2 domain limits (docs/02_design.md §セキュリティ設計)
MAX_INPUT_BYTES = 512 * 1024        # request as a whole
MAX_CSV_BYTES = 5 * 1024 * 1024     # ingest csv payload


class PreProcessNode(FunctionNode):
    """Validate the request, resolve the mode, and extract request_params.

    Accepts either a JSON object as user_input (full request) or plain text
    (treated as generate-mode facility_text). input_context values are merged
    in as defaults so platform callers can pass tenant_id out-of-band.
    """

    # S-1: handles facility-proprietary data — standard business operation.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]

        if not user_input or not str(user_input).strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        params = self._parse_request(str(user_input))
        for key, value in (input_context or {}).items():
            params.setdefault(key, value)

        mode = str(params.get("mode", "generate")).lower()
        if mode not in VALID_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: unknown mode '{mode}' (valid: {', '.join(VALID_MODES)})"],
            }

        tenant_id = str(params.get("tenant_id") or "").strip()
        if not tenant_id:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: tenant_id is required (KB isolation key)"],
            }

        error = self._validate_mode_payload(mode, params)
        if error:
            return {"status": AgentStatus.ERROR.value, "error_log": [f"PreProcessNode: {error}"]}

        emit_trace_event(
            "input_validated",
            {"mode": mode, "tenant_id": tenant_id, "param_keys": sorted(params.keys())},
            state,
        )
        return {
            "mode": mode,
            "tenant_id": tenant_id,
            "request_params": params,
            "validated_input": json.dumps(
                {"mode": mode, "tenant_id": tenant_id, "request_params": params},
                ensure_ascii=False,
                default=str,
            ),
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _parse_request(raw: str) -> dict:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # Accept both flat and nested shapes:
                #   {"mode":..., "tenant_id":..., "csv_payload":...}
                #   {"mode":..., "tenant_id":..., "request_params": {...}}
                nested = parsed.pop("request_params", None)
                if isinstance(nested, dict):
                    parsed = {**nested, **parsed}
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        # Plain text → generate-mode facility description.
        return {"mode": "generate", "facility_text": raw.strip()}

    @staticmethod
    def _validate_mode_payload(mode: str, params: dict) -> str | None:
        if mode == "generate":
            has_text = bool(str(params.get("facility_text") or "").strip())
            url = str(params.get("facility_url") or "").strip()
            if not has_text and not url:
                return "generate mode requires facility_text or facility_url"
            if url and not url.startswith(("http://", "https://")):
                return f"facility_url must be http(s), got: {url[:60]}"
        elif mode == "ingest":
            csv_payload = params.get("csv_payload")
            if not isinstance(csv_payload, str) or not csv_payload.strip():
                return "ingest mode requires csv_payload (CSV text exported from PMS)"
        elif mode == "feedback":
            if not isinstance(params.get("adopted_plan"), dict):
                return "feedback mode requires adopted_plan (dict)"
        return None

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 domain checks: request size caps (default PII scan already ran)."""
        raw = state.get("user_input", "")
        if isinstance(raw, str) and len(raw.encode("utf-8", errors="ignore")) > MAX_CSV_BYTES:
            raise SecurityViolationError(
                f"S-2: request exceeds {MAX_CSV_BYTES} bytes"
            )
        if isinstance(raw, str) and "csv_payload" not in raw and \
                len(raw.encode("utf-8", errors="ignore")) > MAX_INPUT_BYTES:
            raise SecurityViolationError(
                f"S-2: non-ingest request exceeds {MAX_INPUT_BYTES} bytes"
            )
        return state
