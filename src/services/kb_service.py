"""AgentCore Platform v1.0"""

# TRV-C2-004 — Past-plan KB service (hybrid 2-layer store).
#
# docs/02_design.md §過去プラン KB:
#   DB (shared.services.database.BaseDatabase)      = system of record
#   Vector (shared.services.vector_store)           = derived semantic index
#   Sync is strictly one-way (DB write confirmed → vector index updated).
#   Write paths are exactly two: ingest (bulk upsert) / feedback (append+update).
#
# Nodes never touch the stores directly — everything goes through this seam,
# so the schema definition and sync contract live in exactly one place.
# Tests inject fakes (or use the in-memory fallback) — no external infra needed.
#
# Multi-tenant isolation: every record carries tenant_id and every read/write
# is tenant-scoped here. The platform-side isolation mechanism is the FW's
# concern; this service guarantees no query ever crosses tenants.

from __future__ import annotations

from typing import Any

TABLE = "trv_c2_004_plans"

# KB schema — hybrid system-of-record and derived-index split
_TEXT_FIELDS = ("title", "pitch_text", "experience_text")
_META_FIELDS = ("season", "target_segment", "room_type", "price_band", "included", "theme")
_PERF_FIELDS = ("sales_volume", "occupancy", "adopted_flag", "operator_edited")
ALL_FIELDS = ("plan_key", "tenant_id") + _TEXT_FIELDS + _META_FIELDS + _PERF_FIELDS

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    tenant_id       VARCHAR(128) NOT NULL,
    plan_key        VARCHAR(512) NOT NULL,
    title           TEXT,
    pitch_text      TEXT,
    experience_text TEXT,
    season          VARCHAR(64),
    target_segment  VARCHAR(128),
    room_type       VARCHAR(128),
    price_band      VARCHAR(64),
    included        TEXT,
    theme           VARCHAR(128),
    sales_volume    INTEGER,
    occupancy       FLOAT,
    adopted_flag    BOOLEAN,
    operator_edited BOOLEAN,
    PRIMARY KEY (tenant_id, plan_key)
)
"""


def make_plan_key(record: dict) -> str:
    """Idempotency key: PMS plan id when present, else title+season natural key.

    Human decision 2026-07-02: plan id column is optional in the CSV —
    natural key is the default, plan id is preferred when supplied.
    """
    plan_id = str(record.get("plan_id") or "").strip()
    if plan_id:
        return f"id:{plan_id}"
    title = str(record.get("title") or "").strip()
    season = str(record.get("season") or "").strip()
    return f"nk:{title}|{season}"


def embedding_text(record: dict) -> str:
    """Free-text embedded into the vector index (title + pitch + experience)."""
    return "\n".join(str(record.get(f) or "") for f in _TEXT_FIELDS).strip()


class _InMemoryStore:
    """Dict-backed fallback used when no BaseDatabase is injected.

    Local development / unit tests / STG smoke (placeholder deploy) only.
    Production wiring injects a real BaseDatabase (Postgres/MySQL).
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    def upsert(self, row: dict) -> str:
        key = (row["tenant_id"], row["plan_key"])
        outcome = "updated" if key in self.rows else "inserted"
        self.rows[key] = {**self.rows.get(key, {}), **row}
        return outcome

    def get(self, tenant_id: str, plan_key: str) -> dict | None:
        return self.rows.get((tenant_id, plan_key))

    def all_for_tenant(self, tenant_id: str) -> list[dict]:
        return [r for (t, _), r in self.rows.items() if t == tenant_id]


class KbService:
    """Hybrid past-plan KB: DB system-of-record + derived vector index.

    :param db: shared.services.database.BaseDatabase impl, or None for the
        in-memory fallback store.
    :param vector_store: shared.services.vector_store.BaseVectorStore impl,
        or None to skip vector indexing (search falls back to metadata scan
        over the record store — degraded but functional).
    :param top_k: default number of reference plans returned by search.
    """

    def __init__(self, db: Any = None, vector_store: Any = None, top_k: int = 5) -> None:
        self._db = db
        self._vs = vector_store
        self._mem = _InMemoryStore() if db is None else None
        self._schema_ready = False
        self.top_k = top_k

    # -- DB layer (system of record) ----------------------------------------

    def _ensure_schema(self) -> None:
        if self._db is not None and not self._schema_ready:
            self._db.execute(_DDL)
            self._schema_ready = True

    def _db_get(self, tenant_id: str, plan_key: str) -> dict | None:
        if self._db is None:
            return self._mem.get(tenant_id, plan_key)
        self._ensure_schema()
        return self._db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE tenant_id = %(tenant_id)s AND plan_key = %(plan_key)s",
            {"tenant_id": tenant_id, "plan_key": plan_key},
        )

    def _db_upsert(self, row: dict) -> str:
        if self._db is None:
            return self._mem.upsert(row)
        self._ensure_schema()
        existing = self._db_get(row["tenant_id"], row["plan_key"])
        payload = {f: row.get(f) for f in ALL_FIELDS}
        if existing is None:
            cols = ", ".join(ALL_FIELDS)
            marks = ", ".join(f"%({f})s" for f in ALL_FIELDS)
            self._db.execute(f"INSERT INTO {TABLE} ({cols}) VALUES ({marks})", payload)
            return "inserted"
        sets = ", ".join(f"{f} = %({f})s" for f in ALL_FIELDS if f not in ("tenant_id", "plan_key"))
        self._db.execute(
            f"UPDATE {TABLE} SET {sets} WHERE tenant_id = %(tenant_id)s AND plan_key = %(plan_key)s",
            payload,
        )
        return "updated"

    def _db_rows(self, tenant_id: str) -> list[dict]:
        if self._db is None:
            return self._mem.all_for_tenant(tenant_id)
        self._ensure_schema()
        return self._db.fetch_many(
            f"SELECT * FROM {TABLE} WHERE tenant_id = %(tenant_id)s", {"tenant_id": tenant_id}
        )

    # -- Vector layer (derived index) -----------------------------------------

    def _vector_sync(self, row: dict) -> None:
        """One-way sync: called only after the DB write succeeded."""
        if self._vs is None:
            return
        text = embedding_text(row)
        if not text:
            return
        self._vs.upsert([
            {
                "id": f"{row['tenant_id']}:{row['plan_key']}",
                "text": text,
                "metadata": {
                    "tenant_id": row["tenant_id"],
                    "plan_key": row["plan_key"],
                    "season": row.get("season"),
                    "target_segment": row.get("target_segment"),
                    "price_band": row.get("price_band"),
                },
            }
        ])

    # -- Public API (the only surface nodes may use) ---------------------------

    def upsert_plans(self, records: list[dict], tenant_id: str) -> dict:
        """Ingest path: bulk idempotent upsert. DB first, then vector sync."""
        result = {"inserted": 0, "updated": 0, "skipped": 0, "errors": []}
        for i, record in enumerate(records):
            if not str(record.get("title") or "").strip():
                result["skipped"] += 1
                result["errors"].append(f"row {i}: title is required")
                continue
            row = {f: record.get(f) for f in ALL_FIELDS}
            row["tenant_id"] = tenant_id
            row["plan_key"] = make_plan_key(record)
            outcome = self._db_upsert(row)
            self._vector_sync(row)
            result[outcome] += 1
        return result

    def append_feedback(self, record: dict, tenant_id: str, operator_edited: bool = False) -> dict:
        """Feedback path: adopted-plan append/update (adopted_flag=True).

        Re-embeds into the vector index only when the plan text changed
        (docs/02_design.md — performance meta updates never re-embed).
        """
        plan_key = make_plan_key(record)
        existing = self._db_get(tenant_id, plan_key) or {}
        row = {**existing, **{f: record.get(f) for f in ALL_FIELDS if record.get(f) is not None}}
        row["tenant_id"] = tenant_id
        row["plan_key"] = plan_key
        row["adopted_flag"] = True
        row["operator_edited"] = bool(operator_edited or existing.get("operator_edited"))
        outcome = self._db_upsert(row)
        text_changed = embedding_text(existing) != embedding_text(row)
        if outcome == "inserted" or text_changed:
            self._vector_sync(row)
        return {"plan_key": plan_key, "outcome": outcome, "reindexed": outcome == "inserted" or text_changed}

    def search_similar(
        self,
        query_text: str,
        tenant_id: str,
        season: str | None = None,
        target_segment: str | None = None,
        top_k: int | None = None,
    ) -> list[dict]:
        """Generate path: semantic search + metadata filter (hybrid).

        BaseVectorStore.search(query, top_k) has no filter parameter, so we
        over-fetch and post-filter by payload metadata here (tenant filter is
        mandatory; season/target are soft filters — applied only when they
        leave at least one hit, so sparse KBs still return references).
        """
        k = top_k or self.top_k
        if self._vs is None:
            hits = self._scan_fallback(query_text, tenant_id)
        else:
            raw = self._vs.search(query_text, top_k=max(k * 5, 25))
            hits = [h for h in raw if (h.get("metadata") or {}).get("tenant_id") == tenant_id]
        for field, wanted in (("season", season), ("target_segment", target_segment)):
            if wanted:
                narrowed = [h for h in hits if (h.get("metadata") or {}).get(field) == wanted]
                if narrowed:
                    hits = narrowed
        results = []
        for h in hits[:k]:
            meta = h.get("metadata") or {}
            row = self._db_get(tenant_id, meta.get("plan_key", "")) or {}
            results.append({**row, "_score": h.get("score")})
        return results

    def _scan_fallback(self, query_text: str, tenant_id: str) -> list[dict]:
        """Degraded search when no vector store is wired: naive term overlap."""
        terms = set(str(query_text).split())
        scored = []
        for row in self._db_rows(tenant_id):
            text = embedding_text(row)
            score = sum(1 for t in terms if t and t in text)
            scored.append((score, {"metadata": {"tenant_id": tenant_id, "plan_key": row["plan_key"],
                                                "season": row.get("season"),
                                                "target_segment": row.get("target_segment")},
                           "score": score}))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [h for _, h in scored]

    def price_stats(self, tenant_id: str, season: str | None = None,
                    target_segment: str | None = None) -> dict:
        """Generate path: price_band distribution over the FULL record set.

        Human decision 2026-07-02: price suggestions come from MATCHING
        historical records only — when there is no history for the requested
        season/target the caller must present the plan without a price.
        Filters are therefore STRICT (no fallback to the full history — a
        summer request must never price itself off winter records; that would
        violate the design rule). Aggregation runs over the DB (system of
        record), not just the top-N vector hits, so the sample is the whole
        matching tenant history.
        """
        rows = self._db_rows(tenant_id)
        if season:
            rows = [r for r in rows if r.get("season") == season]
        if target_segment:
            rows = [r for r in rows if r.get("target_segment") == target_segment]
        bands: dict[str, int] = {}
        for r in rows:
            band = str(r.get("price_band") or "").strip()
            if band:
                bands[band] = bands.get(band, 0) + 1
        return {"sample_size": sum(bands.values()), "price_bands": bands}
