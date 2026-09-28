# TRV-C2-004 — Unit Tests: outer + inner nodes
# (TC-01 state contract / TC-02 SecurityViolationError / TC-05 audit events /
#  TC-08 trust level — per-node behaviour; graph-level order is PB-6)

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.feedback_nodes import KbAppendNode, PlanNormalizeNode
from src.nodes.generate_nodes import (
    ContextEnrichmentNode,
    FacilityProfileNode,
    PastPlanRetrievalNode,
    PlanGenerationNode,
    RefinementNode,
)
from src.nodes.ingest_nodes import CsvParseNode, KbBulkUpsertNode, SchemaMapNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import MAX_CSV_BYTES, PreProcessNode
from src.services.kb_service import KbService
from tests.fakes import FakeLLM

SUCCESS = AgentStatus.SUCCESS.value
ERROR = AgentStatus.ERROR.value


def _req(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


class TestPreProcessNode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_generate_flat_shape(self):
        out = self.node.execute({"user_input": _req(
            {"mode": "generate", "tenant_id": "t1", "facility_text": "海辺の宿"})})
        assert out["status"] == SUCCESS and out["mode"] == "generate"
        assert out["request_params"]["facility_text"] == "海辺の宿"

    def test_generate_nested_request_params_shape(self):
        out = self.node.execute({"user_input": _req(
            {"mode": "ingest", "tenant_id": "t1",
             "request_params": {"csv_payload": "title\nA\n"}})})
        assert out["status"] == SUCCESS and out["request_params"]["csv_payload"]

    def test_plain_text_becomes_generate(self):
        out = self.node.execute({"user_input": "海辺の温泉旅館です",
                                 "input_context": {"tenant_id": "t1"}})
        assert out["status"] == SUCCESS and out["mode"] == "generate"
        assert out["request_params"]["facility_text"] == "海辺の温泉旅館です"

    def test_tenant_id_required(self):
        out = self.node.execute({"user_input": _req({"mode": "generate", "facility_text": "x"})})
        assert out["status"] == ERROR and "tenant_id" in out["error_log"][0]

    def test_unknown_mode_rejected(self):
        out = self.node.execute({"user_input": _req({"mode": "delete_all", "tenant_id": "t1"})})
        assert out["status"] == ERROR

    def test_facility_url_only_is_accepted(self):
        out = self.node.execute({"user_input": _req(
            {"mode": "generate", "tenant_id": "t1", "facility_url": "https://example.com/onsen"})})
        assert out["status"] == SUCCESS

    def test_facility_url_bad_scheme_rejected(self):
        out = self.node.execute({"user_input": _req(
            {"mode": "generate", "tenant_id": "t1", "facility_url": "ftp://example.com"})})
        assert out["status"] == ERROR and "facility_url" in out["error_log"][0]

    def test_generate_without_text_or_url_rejected(self):
        out = self.node.execute({"user_input": _req(
            {"mode": "generate", "tenant_id": "t1"})})
        assert out["status"] == ERROR

    def test_empty_input(self):
        assert self.node.execute({"user_input": ""})["status"] == ERROR

    def test_s2_size_cap_raises_security_violation(self):
        """TC-02: SecurityViolationError fires on oversized input."""
        state = {"user_input": "x" * (MAX_CSV_BYTES + 1)}
        with pytest.raises(SecurityViolationError):
            self.node._extra_security_gate_input(state)

    def test_trust_level_declared(self):
        """TC-08: standard business operation requires VERIFIED_EXTERNAL."""
        assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


class TestGenerateNodes:
    def test_facility_profile_structures_text(self):
        node = FacilityProfileNode(llm=FakeLLM())
        out = node.execute({"mode": "generate",
                            "request_params": {"facility_text": "海辺の温泉旅館"}})
        assert out["status"] == SUCCESS
        assert out["facility_profile"]["concept"] == "静かな大人の宿"

    def test_facility_profile_requires_llm(self):
        out = FacilityProfileNode().execute({"mode": "generate", "request_params": {}})
        assert out["status"] == ERROR

    def test_facility_profile_fetches_url_via_seam(self):
        fetched = {}
        def fake_fetch(url):
            fetched["url"] = url
            return "海辺の温泉旅館。全室オーシャンビュー。"
        node = FacilityProfileNode(llm=FakeLLM(), fetcher=fake_fetch)
        out = node.execute({"mode": "generate",
                            "request_params": {"facility_url": "https://example.com/onsen"}})
        assert out["status"] == SUCCESS
        assert fetched["url"] == "https://example.com/onsen"

    def test_facility_profile_url_fetch_failure_is_clean_error(self):
        from src.services.url_fetcher import UrlFetchError
        def failing_fetch(url):
            raise UrlFetchError("resolved address 10.0.0.1 is not public (SSRF guard)")
        node = FacilityProfileNode(llm=FakeLLM(), fetcher=failing_fetch)
        out = node.execute({"mode": "generate",
                            "request_params": {"facility_url": "https://internal.example/"}})
        assert out["status"] == ERROR and "fetch failed" in out["error_log"][0]

    def test_facility_text_takes_precedence_over_url(self):
        def must_not_fetch(url):
            raise AssertionError("fetcher must not be called when facility_text is present")
        node = FacilityProfileNode(llm=FakeLLM(), fetcher=must_not_fetch)
        out = node.execute({"mode": "generate",
                            "request_params": {"facility_text": "テキストあり",
                                               "facility_url": "https://example.com"}})
        assert out["status"] == SUCCESS

    def test_context_enrichment_uses_request_season(self):
        out = ContextEnrichmentNode().execute(
            {"mode": "generate", "request_params": {"season": "冬", "target_segment": "家族"}})
        assert out["planning_context"]["season"] == "冬"
        assert out["planning_context"]["target_segment"] == "家族"

    def test_context_enrichment_defaults_season_from_calendar(self):
        out = ContextEnrichmentNode().execute({"mode": "generate", "request_params": {}})
        assert out["planning_context"]["season"] in ("春", "夏", "秋", "冬")

    def test_retrieval_returns_references_and_price_stats(self):
        kb = KbService()
        kb.upsert_plans([{"title": "夏プラン", "pitch_text": "海", "season": "夏",
                          "price_band": "20000-30000"}], tenant_id="t1")
        node = PastPlanRetrievalNode(kb_service=kb)
        out = node.execute({"mode": "generate", "tenant_id": "t1",
                            "facility_profile": {"concept": "海"},
                            "planning_context": {"season": "夏"}})
        assert out["status"] == SUCCESS
        assert out["price_stats"]["sample_size"] == 1
        assert out["reference_plans"]

    def test_generation_nulls_price_without_history(self):
        node = PlanGenerationNode(llm=FakeLLM())
        out = node.execute({"mode": "generate", "planning_context": {"num_plans": 2},
                            "price_stats": {"sample_size": 0, "price_bands": {}}})
        assert out["status"] == SUCCESS
        assert all(p["price_range"] is None for p in out["plan_candidates"])

    def test_generation_keeps_price_with_history(self):
        node = PlanGenerationNode(llm=FakeLLM())
        out = node.execute({"mode": "generate", "planning_context": {"num_plans": 2},
                            "price_stats": {"sample_size": 3, "price_bands": {"20000-30000": 3}}})
        assert any(p["price_range"] for p in out["plan_candidates"])

    def test_refinement_never_loses_drafts(self):
        class BrokenLLM(FakeLLM):
            def complete(self, messages):
                return {"content": "not json at all —"}
        node = RefinementNode(llm=BrokenLLM())
        candidates = [{"title": "A"}]
        out = node.execute({"mode": "generate", "plan_candidates": candidates})
        assert out["plan_drafts"] == candidates

    def test_upstream_error_passthrough(self):
        for node in (FacilityProfileNode(llm=FakeLLM()), ContextEnrichmentNode(),
                     PlanGenerationNode(llm=FakeLLM()), RefinementNode(llm=FakeLLM())):
            assert node.execute({"mode": "generate", "status": ERROR}) == {}

    def test_s3_banned_expression_blocked(self):
        node = RefinementNode(llm=FakeLLM())
        with pytest.raises(RuntimeError, match="S-3"):
            node._extra_security_gate_output(
                {"plan_drafts": [{"pitch_text": "この温泉で病気が治る"}]})


class TestLlmCallFailure:
    """A provider rejection (content filter, rate limit, 5xx) must
    surface as an actionable error, never as an unhandled exception that reaches the
    caller as `status: error` with `error_log: None`."""

    class ExplodingLLM(FakeLLM):
        def __init__(self, exc: Exception | None = None) -> None:
            super().__init__()
            self._exc = exc or RuntimeError("content_filter: the response was filtered")

        def complete(self, messages):
            raise self._exc

    def test_facility_profile_reports_the_provider_failure(self):
        out = FacilityProfileNode(llm=self.ExplodingLLM()).execute(
            {"mode": "generate", "request_params": {"facility_text": "海辺の宿"}}
        )
        assert out["status"] == AgentStatus.ERROR.value
        assert out["error_log"] and "FacilityProfileNode: LLM call failed" in out["error_log"][0]
        assert "RuntimeError" in out["error_log"][0]

    def test_plan_generation_reports_the_provider_failure(self):
        out = PlanGenerationNode(llm=self.ExplodingLLM()).execute(
            {"mode": "generate", "planning_context": {"num_plans": 2}}
        )
        assert out["status"] == AgentStatus.ERROR.value
        assert out["error_log"] and "PlanGenerationNode: LLM call failed" in out["error_log"][0]

    def test_refinement_keeps_the_drafts_on_provider_failure(self):
        """Polishing is additive: losing the provider must not lose the plans."""
        candidates = [{"title": "A", "price_range": None}]
        out = RefinementNode(llm=self.ExplodingLLM()).execute(
            {"mode": "generate", "plan_candidates": candidates}
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["plan_drafts"] == candidates

    def test_error_log_carries_no_part_of_the_provider_message(self):
        """`error_log` is checkpointed: bounding the message is not enough, it must be absent.

        A provider rejection commonly echoes the offending request text, so persisting any
        slice of it would checkpoint the very content that triggered the block.
        """
        exc = RuntimeError("content_filter blocked: CANARY_REQUEST_TEXT was flagged as jailbreak")
        out = FacilityProfileNode(llm=self.ExplodingLLM(exc)).execute(
            {"mode": "generate", "request_params": {"facility_text": "海辺の宿"}}
        )
        entry = out["error_log"][0]
        assert "CANARY_REQUEST_TEXT" not in entry
        assert "content_filtered" in entry, "the category must still be actionable"

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (RuntimeError("The response was filtered due to the content management policy"), "content_filtered"),
            (RuntimeError("Rate limit reached for gpt-4o"), "rate_limited"),
            (RuntimeError("Connection timed out"), "provider_unavailable"),
            (RuntimeError("something else entirely"), "provider_error"),
        ],
    )
    def test_provider_failures_map_to_allow_listed_categories(self, exc, expected):
        """The exception type alone is not actionable — Azure returns the same class for
        a content-filter block and an ordinary malformed request."""
        out = FacilityProfileNode(llm=self.ExplodingLLM(exc)).execute(
            {"mode": "generate", "request_params": {"facility_text": "海辺の宿"}}
        )
        assert expected in out["error_log"][0]

    def test_an_http_status_is_reported_when_the_provider_gives_one(self):
        class Status429(RuntimeError):
            status_code = 429

        out = FacilityProfileNode(llm=self.ExplodingLLM(Status429("slow down"))).execute(
            {"mode": "generate", "request_params": {"facility_text": "海辺の宿"}}
        )
        assert "rate_limited" in out["error_log"][0] and "HTTP 429" in out["error_log"][0]

    def test_a_working_provider_is_untouched(self):
        """Regression guard for the success path — deliberately still passes without the fix."""
        llm = FakeLLM()
        out = FacilityProfileNode(llm=llm).execute(
            {"mode": "generate", "request_params": {"facility_text": "海辺の宿"}}
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        assert out["facility_profile"]["summary"]
        assert "[TASK:profile]" in llm.calls[0], "the prompt sent to the provider must not have changed"

    def test_control_flow_exceptions_are_not_swallowed(self):
        """A HITL interrupt or a security violation belongs to the framework, not to this handler."""
        from framework.errors import SecurityViolationError
        from langgraph.errors import GraphInterrupt

        for exc in (GraphInterrupt(("payload",)), SecurityViolationError("blocked")):
            with pytest.raises(type(exc)):
                FacilityProfileNode(llm=self.ExplodingLLM(exc)).execute(
                    {"mode": "generate", "request_params": {"facility_text": "海辺の宿"}}
                )


class TestIngestNodes:
    CSV = "プラン名,季節,価格帯,ターゲット\n海辺プラン,夏,20000-30000,カップル\n山プラン,秋,15000-20000,家族\n"

    def test_csv_parse(self):
        out = CsvParseNode().execute({"mode": "ingest",
                                      "request_params": {"csv_payload": self.CSV}})
        assert out["status"] == SUCCESS and len(out["parsed_rows"]) == 2

    def test_csv_parse_empty_errors(self):
        out = CsvParseNode().execute({"mode": "ingest", "request_params": {"csv_payload": "title\n"}})
        assert out["status"] == ERROR

    def test_schema_map_japanese_aliases(self):
        parsed = CsvParseNode().execute({"mode": "ingest",
                                         "request_params": {"csv_payload": self.CSV}})
        out = SchemaMapNode().execute({"mode": "ingest", "request_params": {},
                                       "parsed_rows": parsed["parsed_rows"],
                                       "skipped_rows": []})
        assert out["status"] == SUCCESS
        rec = out["kb_records"][0]
        assert rec["title"] == "海辺プラン" and rec["season"] == "夏"
        assert rec["price_band"] == "20000-30000" and rec["target_segment"] == "カップル"

    def test_schema_map_column_map_override(self):
        out = SchemaMapNode().execute({
            "mode": "ingest",
            "request_params": {"column_map": {"名称": "title"}},
            "parsed_rows": [{"名称": "特別プラン"}],
            "skipped_rows": [],
        })
        assert out["kb_records"][0]["title"] == "特別プラン"

    def test_schema_map_unmappable_errors(self):
        out = SchemaMapNode().execute({"mode": "ingest", "request_params": {},
                                       "parsed_rows": [{"unknown_col": "x"}],
                                       "skipped_rows": []})
        assert out["status"] == ERROR

    def test_bulk_upsert(self):
        kb = KbService()
        out = KbBulkUpsertNode(kb_service=kb).execute({
            "mode": "ingest", "tenant_id": "t1",
            "kb_records": [{"title": "A", "season": "夏"}], "skipped_rows": []})
        assert out["ingest_result"]["inserted"] == 1


class TestFeedbackNodes:
    def test_normalize_detects_operator_edit(self):
        out = PlanNormalizeNode().execute({
            "mode": "feedback",
            "request_params": {
                "adopted_plan": {"title": "A", "pitch_text": "改稿"},
                "original_draft": {"title": "A", "pitch_text": "元"},
            }})
        assert out["kb_records"][0]["operator_edited"] is True

    def test_normalize_unedited(self):
        plan = {"title": "A", "pitch_text": "同一"}
        out = PlanNormalizeNode().execute({
            "mode": "feedback",
            "request_params": {"adopted_plan": dict(plan), "original_draft": dict(plan)}})
        assert out["kb_records"][0]["operator_edited"] is False

    def test_append_marks_adopted(self):
        kb = KbService()
        out = KbAppendNode(kb_service=kb).execute({
            "mode": "feedback", "tenant_id": "t1",
            "kb_records": [{"title": "A", "season": "夏", "operator_edited": True}]})
        assert out["status"] == SUCCESS
        row = kb._mem.get("t1", "nk:A|夏")
        assert row["adopted_flag"] is True and row["operator_edited"] is True


class TestPostProcessNode:
    def test_generate_output_with_price_note(self):
        out = PostProcessNode().execute({
            "mode": "generate", "status": SUCCESS,
            "result": {"mode": "generate", "plans": [{"title": "A"}],
                       "price_stats": {"sample_size": 0}, "revision_rounds": 1}})
        formatted = out["result"]
        assert formatted["plan_count"] == 1
        assert "価格は未提示" in formatted["price_note"]

    def test_upstream_error_passthrough(self):
        assert PostProcessNode().execute({"status": ERROR}) == {}

    def test_s3_tenant_leak_blocked(self):
        """レビュー指摘 F-02: S-3 ゲートは実 tenant_id で検査し、漏洩時に遮断する。"""
        node = PostProcessNode()
        out = node.execute({
            "mode": "generate", "status": SUCCESS, "tenant_id": "hotel-001",
            "result": {"mode": "generate",
                       "plans": [{"title": "hotel-001 特別プラン"}],
                       "price_stats": {"sample_size": 1}}})
        with pytest.raises(RuntimeError, match="tenant_id leaked"):
            node._extra_security_gate_output(out)

    def test_s3_tenant_marker_stripped_on_clean_output(self):
        node = PostProcessNode()
        out = node.execute({
            "mode": "generate", "status": SUCCESS, "tenant_id": "hotel-001",
            "result": {"mode": "generate", "plans": [{"title": "海辺プラン"}],
                       "price_stats": {"sample_size": 1}}})
        cleaned = node._extra_security_gate_output(out)
        assert "_tenant_id" not in cleaned


class TestTrustLevels:
    def test_all_nodes_declare_verified_external(self):
        """TC-08: every domain node declares an explicit non-ANONYMOUS level."""
        import src.nodes as nodes_pkg
        import importlib
        import inspect
        import pkgutil
        from framework.nodes.base_node import BaseNode

        for _, modname, _ in pkgutil.walk_packages(nodes_pkg.__path__, prefix="src.nodes."):
            module = importlib.import_module(modname)
            for attr in vars(module).values():
                if (isinstance(attr, type) and issubclass(attr, BaseNode)
                        and attr.__module__ == modname and not inspect.isabstract(attr)):
                    assert attr.required_trust_level == TrustLevel.VERIFIED_EXTERNAL, \
                        f"{attr.__name__} must declare VERIFIED_EXTERNAL"


class TestPlanReviewIntent:
    """承認判定 2 段階（固定語彙 fast-path → LLM 意図分類・判定不能は revise）。"""

    def _node(self, llm=None):
        from src.nodes.plan_review_node import PlanReviewNode
        return PlanReviewNode(llm=llm)

    def test_fast_path_tokens(self):
        node = self._node()
        assert node._classify_intent("approve", {}) == "approve"
        assert node._classify_intent("承認", {}) == "approve"
        assert node._classify_intent({"action": "採用"}, {}) == "approve"

    def test_natural_language_approval_via_llm(self):
        node = self._node(llm=FakeLLM())
        assert node._classify_intent("いいよ、それで行こう", {}) == "approve"

    def test_natural_language_revision_via_llm(self):
        node = self._node(llm=FakeLLM())
        assert node._classify_intent("タイトルをもっと目立つように直して", {}) == "revise"

    def test_instructions_dict_is_always_revision(self):
        node = self._node(llm=FakeLLM())
        assert node._classify_intent({"instructions": "安くして"}, {}) == "revise"

    def test_unclassifiable_falls_to_safe_side(self):
        node = self._node(llm=None)  # no LLM → ambiguous text cannot be approved
        assert node._classify_intent("いいよ、それで行こう", {}) == "revise"

    def test_llm_failure_falls_to_safe_side(self):
        class ExplodingLLM(FakeLLM):
            def complete(self, messages):
                raise RuntimeError("boom")
        node = self._node(llm=ExplodingLLM())
        assert node._classify_intent("いい感じ", {}) == "revise"


class TestBuildLlm:
    def test_missing_provider_raises_config_error(self):
        from framework.errors import ConfigError
        from src.services.llm_client import build_llm
        for cfg in (None, {}, {"provider": "static"}, {"provider": "unknown"}):
            with pytest.raises(ConfigError):
                build_llm(cfg)
