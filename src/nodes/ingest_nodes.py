"""AgentCore Platform v1.0"""

# TRV-C2-004 — ingest-mode inner nodes (docs/02_design.md).
#
#   ① CsvParseNode     — parse & row-validate the PMS CSV export
#   ② SchemaMapNode    — map columns onto the KB schema
#   ③ KbBulkUpsertNode — idempotent bulk upsert via kb_service (DB → vector)

from __future__ import annotations

import csv
import io
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.common import upstream_error
from src.services.kb_service import ALL_FIELDS

# Default CSV column aliases → KB fields. Callers may override / extend via
# request_params.column_map ({"csv column": "kb_field"}).
_DEFAULT_ALIASES = {
    "plan_id": "plan_id", "プランid": "plan_id", "プランコード": "plan_id",
    "title": "title", "plan_name": "title", "プラン名": "title", "タイトル": "title",
    "pitch_text": "pitch_text", "description": "pitch_text", "訴求文": "pitch_text", "説明": "pitch_text",
    "experience_text": "experience_text", "体験": "experience_text", "内容": "experience_text",
    "season": "season", "季節": "season",
    "target_segment": "target_segment", "ターゲット": "target_segment", "客層": "target_segment",
    "room_type": "room_type", "客室タイプ": "room_type", "部屋タイプ": "room_type",
    "price_band": "price_band", "価格帯": "price_band", "料金帯": "price_band",
    "included": "included", "含まれるもの": "included", "特典": "included",
    "theme": "theme", "テーマ": "theme",
    "sales_volume": "sales_volume", "販売数": "sales_volume",
    "occupancy": "occupancy", "稼働率": "occupancy",
}


class CsvParseNode(FunctionNode):
    """① Parse the CSV payload; keep valid rows, record skipped rows."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "ingest":
            return {}
        payload = str((state.get("request_params") or {}).get("csv_payload") or "")
        try:
            reader = csv.DictReader(io.StringIO(payload))
            rows = [dict(r) for r in reader]
        except csv.Error as e:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": [f"CsvParseNode: CSV parse failed — {e}"]}
        if not rows:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["CsvParseNode: CSV has no data rows"]}
        parsed, skipped = [], []
        for i, row in enumerate(rows):
            cleaned = {str(k).strip(): str(v).strip() for k, v in row.items()
                       if k is not None and v is not None}
            if any(cleaned.values()):
                parsed.append(cleaned)
            else:
                skipped.append({"row": i, "reason": "empty row"})
        emit_trace_event("csv_parsed", {"rows": len(parsed), "skipped": len(skipped)}, state)
        return {"parsed_rows": parsed, "skipped_rows": skipped, "status": AgentStatus.SUCCESS.value}


class SchemaMapNode(FunctionNode):
    """② Map parsed CSV rows onto the KB record schema (deterministic).

    Column resolution: request_params.column_map overrides → default aliases
    (Japanese/English). Unmapped columns are dropped; missing optional fields
    stay None (the KB schema tolerates sparse performance metadata).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "ingest":
            return {}
        rows = state.get("parsed_rows") or []
        overrides = {str(k).strip().lower(): v
                     for k, v in ((state.get("request_params") or {}).get("column_map") or {}).items()}
        records, skipped = [], list(state.get("skipped_rows") or [])
        for i, row in enumerate(rows):
            record: dict[str, Any] = {}
            for col, value in row.items():
                key = col.strip().lower()
                field = overrides.get(key) or _DEFAULT_ALIASES.get(key)
                if field and value != "":
                    record[field] = value
            if not str(record.get("title") or "").strip():
                skipped.append({"row": i, "reason": "no title column mapped"})
                continue
            records.append({f: record.get(f) for f in ("plan_id",) + tuple(ALL_FIELDS)
                            if f in record})
        if not records:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["SchemaMapNode: no rows could be mapped to the KB schema "
                                  "(check column names or pass request_params.column_map)"]}
        emit_trace_event("schema_mapped", {"records": len(records), "skipped": len(skipped)}, state)
        return {"kb_records": records, "skipped_rows": skipped, "status": AgentStatus.SUCCESS.value}


class KbBulkUpsertNode(FunctionNode):
    """③ Idempotent bulk upsert into the hybrid KB (DB first, vector synced)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: Any = None) -> None:
        super().__init__()
        self._kb = kb_service

    def execute(self, state: dict) -> dict:
        if upstream_error(state) or state.get("mode") != "ingest":
            return {}
        if self._kb is None:
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["KbBulkUpsertNode: no KbService configured"]}
        result = self._kb.upsert_plans(state.get("kb_records") or [],
                                       tenant_id=state.get("tenant_id", ""))
        result["pre_skipped"] = len(state.get("skipped_rows") or [])
        emit_trace_event("kb_ingested", result, state)
        return {"ingest_result": result, "status": AgentStatus.SUCCESS.value}
