# TRV-C2-004 — Unit Tests: KbService (hybrid 2-layer store)

from src.services.kb_service import KbService, embedding_text, make_plan_key
from tests.fakes import FakeVectorStore, RecordingDatabase


def _record(**over):
    base = {"title": "海辺の夏プラン", "pitch_text": "波音の宿", "experience_text": "露天風呂",
            "season": "夏", "target_segment": "カップル", "price_band": "20000-30000"}
    base.update(over)
    return base


class TestPlanKey:
    def test_plan_id_preferred(self):
        assert make_plan_key({"plan_id": "PMS-1", "title": "A", "season": "夏"}) == "id:PMS-1"

    def test_natural_key_fallback(self):
        assert make_plan_key({"title": "A", "season": "夏"}) == "nk:A|夏"


class TestUpsertIdempotency:
    def test_double_ingest_updates_not_duplicates(self):
        kb = KbService()
        first = kb.upsert_plans([_record()], tenant_id="t1")
        second = kb.upsert_plans([_record()], tenant_id="t1")
        assert first == {"inserted": 1, "updated": 0, "skipped": 0, "errors": []}
        assert second["updated"] == 1 and second["inserted"] == 0
        assert len(kb._mem.all_for_tenant("t1")) == 1

    def test_title_required(self):
        kb = KbService()
        result = kb.upsert_plans([{"season": "夏"}], tenant_id="t1")
        assert result["skipped"] == 1 and result["errors"]

    def test_vector_synced_after_db_write(self):
        vs = FakeVectorStore()
        kb = KbService(vector_store=vs)
        kb.upsert_plans([_record()], tenant_id="t1")
        assert len(vs.upsert_calls) == 1
        doc = vs.upsert_calls[0][0]
        assert doc["metadata"]["tenant_id"] == "t1"
        assert "海辺の夏プラン" in doc["text"]


class TestFeedback:
    def test_adopted_flag_update(self):
        kb = KbService()
        kb.upsert_plans([_record()], tenant_id="t1")
        result = kb.append_feedback(_record(), tenant_id="t1")
        assert result["outcome"] == "updated"
        row = kb._mem.get("t1", make_plan_key(_record()))
        assert row["adopted_flag"] is True

    def test_reindex_only_when_text_changed(self):
        vs = FakeVectorStore()
        kb = KbService(vector_store=vs)
        kb.upsert_plans([_record()], tenant_id="t1")
        # performance-meta-only update → no re-embedding
        r1 = kb.append_feedback(_record(), tenant_id="t1")
        assert r1["reindexed"] is False and len(vs.upsert_calls) == 1
        # text change → re-embedding
        r2 = kb.append_feedback(_record(pitch_text="改稿した訴求文"), tenant_id="t1")
        assert r2["reindexed"] is True and len(vs.upsert_calls) == 2

    def test_operator_edited_persisted(self):
        kb = KbService()
        kb.append_feedback(_record(), tenant_id="t1", operator_edited=True)
        row = kb._mem.get("t1", make_plan_key(_record()))
        assert row["operator_edited"] is True


class TestSearch:
    def test_tenant_isolation(self):
        vs = FakeVectorStore()
        kb = KbService(vector_store=vs)
        kb.upsert_plans([_record()], tenant_id="t1")
        kb.upsert_plans([_record(title="他社プラン")], tenant_id="t2")
        hits = kb.search_similar("海辺の夏プラン", tenant_id="t1")
        keys = {h.get("plan_key") for h in hits}
        assert all(kb._mem.get("t1", k) for k in keys if k)
        assert not any("他社" in str(h.get("title")) for h in hits)

    def test_scan_fallback_without_vector_store(self):
        kb = KbService()
        kb.upsert_plans([_record()], tenant_id="t1")
        hits = kb.search_similar("海辺の夏プラン 夏", tenant_id="t1")
        assert hits and hits[0]["title"] == "海辺の夏プラン"


class TestPriceStats:
    def test_aggregates_over_full_history(self):
        kb = KbService()
        kb.upsert_plans([_record(title=f"P{i}") for i in range(4)], tenant_id="t1")
        kb.upsert_plans([_record(title="Q", season="秋", price_band="15000-20000")], tenant_id="t1")
        stats = kb.price_stats(tenant_id="t1", season="夏")
        assert stats["sample_size"] == 4
        assert stats["price_bands"] == {"20000-30000": 4}

    def test_empty_history_yields_zero_sample(self):
        kb = KbService()
        assert kb.price_stats(tenant_id="t-none")["sample_size"] == 0

    def test_filter_is_strict_no_cross_season_fallback(self):
        """設計違反防止: 夏のリクエストに冬の実績で価格を出してはならない
        （レビュー指摘 F-01 — フィルタ 0 件は 0 件のまま返す）。"""
        kb = KbService()
        kb.upsert_plans([_record(title="冬プラン", season="冬")], tenant_id="t1")
        assert kb.price_stats(tenant_id="t1", season="夏")["sample_size"] == 0

    def test_filter_is_strict_no_cross_target_fallback(self):
        kb = KbService()
        kb.upsert_plans([_record()], tenant_id="t1")  # target: カップル
        assert kb.price_stats(tenant_id="t1", season="夏",
                              target_segment="家族")["sample_size"] == 0


class TestSqlPath:
    def test_insert_goes_through_database(self):
        db = RecordingDatabase()
        kb = KbService(db=db)
        kb.upsert_plans([_record()], tenant_id="t1")
        verbs = [q[0] for q in db.queries]
        assert "CREATE" in verbs        # schema ensured
        assert "SELECT_ONE" in verbs    # existence check
        assert "INSERT" in verbs        # insert on miss

    def test_update_on_existing_row(self):
        db = RecordingDatabase()
        db.fetch_one_returns = [dict(_record(), tenant_id="t1", plan_key="nk:海辺の夏プラン|夏")]
        kb = KbService(db=db)
        kb.upsert_plans([_record()], tenant_id="t1")
        assert "UPDATE" in [q[0] for q in db.queries]


class TestEmbeddingText:
    def test_concatenates_text_fields(self):
        text = embedding_text(_record())
        assert "海辺の夏プラン" in text and "波音の宿" in text and "露天風呂" in text
