# TRV-C2-004 — Integration Tests: full outer-graph invoke across all 3 modes
# incl. the HITL revise→approve round-trip (TourPlanningProcurementRoutingAgent verified failure modes).

import json

from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import AccommodationPlanBrandingAgent
from src.services.kb_service import KbService
from tests.fakes import FakeLLM, FakeVectorStore

SUCCESS = AgentStatus.SUCCESS.value
ERROR = AgentStatus.ERROR.value
AWAITING = AgentStatus.AWAITING_HUMAN.value

CSV = "プラン名,季節,価格帯,ターゲット\n海辺プラン,夏,20000-30000,カップル\n山プラン,秋,15000-20000,家族\n"


def build_agent(kb=None, hitl=True, llm=None):
    agent = AccommodationPlanBrandingAgent(config={
        "kb_service": kb if kb is not None else KbService(vector_store=FakeVectorStore()),
        "llm": llm if llm is not None else FakeLLM(),
        "memory_enabled": True,
        "hitl": {"enabled": hitl, "max_hitl": 8},
        "max_revisions": 8,
        "max_retry": 3,
    })
    agent.compile(checkpointer=InMemorySaver())
    return agent


def invoke(agent, payload, session="s1", trust=TrustLevel.VERIFIED_EXTERNAL):
    ctx = InvocationContext(session_id=session, caller_trust_level=trust)
    return agent.invoke(json.dumps(payload, ensure_ascii=False), ctx=ctx)


class TestIngestMode:
    def test_csv_ingest_end_to_end(self):
        agent = build_agent()
        out = invoke(agent, {"mode": "ingest", "tenant_id": "t1",
                             "request_params": {"csv_payload": CSV}})
        assert out["status"] == SUCCESS
        assert out["output"]["ingest_result"]["inserted"] == 2

    def test_reingest_is_idempotent(self):
        kb = KbService()
        agent = build_agent(kb=kb)
        invoke(agent, {"mode": "ingest", "tenant_id": "t1",
                       "request_params": {"csv_payload": CSV}}, session="a")
        out = invoke(agent, {"mode": "ingest", "tenant_id": "t1",
                             "request_params": {"csv_payload": CSV}}, session="b")
        assert out["output"]["ingest_result"] == {
            "inserted": 0, "updated": 2, "skipped": 0, "errors": [], "pre_skipped": 0}
        assert len(kb._mem.all_for_tenant("t1")) == 2


class TestGenerateModeHitl:
    def test_hitl_suspends_then_approves(self):
        """PB-7 core: interrupt propagates to the caller as awaiting_human,
        and resume→approve reaches status success (not just suspend —
        the resume-loop failure mode is only visible on the full round-trip)."""
        agent = build_agent()
        out = invoke(agent, {"mode": "generate", "tenant_id": "t1",
                             "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"}})
        assert out["status"] == AWAITING
        assert out["thread_id"]
        final = agent.resume(thread_id=out["thread_id"], feedback="approve")
        assert final["status"] == SUCCESS
        assert final["output"]["plans"]

    def test_hitl_revision_loop_reflects_instructions(self):
        agent = build_agent()
        out = invoke(agent, {"mode": "generate", "tenant_id": "t1",
                             "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"}})
        assert out["status"] == AWAITING
        out = agent.resume(thread_id=out["thread_id"],
                           feedback={"instructions": "もっとお得なタイトルに"})
        assert out["status"] == AWAITING  # second review round
        final = agent.resume(thread_id=out["thread_id"], feedback="approve")
        assert final["status"] == SUCCESS
        assert final["output"]["revision_rounds"] == 1
        assert any("修正反映版" in p["title"] for p in final["output"]["plans"])

    def test_natural_language_approval(self):
        """「いいよ、それで行こう」のような自然文承認が LLM 意図分類で通る。"""
        agent = build_agent()
        out = invoke(agent, {"mode": "generate", "tenant_id": "t1",
                             "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"}})
        assert out["status"] == AWAITING
        final = agent.resume(thread_id=out["thread_id"], feedback="いいよ、それで行こう")
        assert final["status"] == SUCCESS
        assert final["output"]["plans"]

    def test_hitl_disabled_runs_straight_through(self):
        agent = build_agent(hitl=False)
        out = invoke(agent, {"mode": "generate", "tenant_id": "t1",
                             "request_params": {"facility_text": "海辺の温泉旅館"}})
        assert out["status"] == SUCCESS
        assert out["output"]["plans"]

    def test_price_null_without_history_and_present_with(self):
        kb = KbService(vector_store=FakeVectorStore())
        agent = build_agent(kb=kb, hitl=False)
        out = invoke(agent, {"mode": "generate", "tenant_id": "t-empty",
                             "request_params": {"facility_text": "宿", "season": "夏"}})
        assert all(p["price_range"] is None for p in out["output"]["plans"])
        assert "価格は未提示" in out["output"]["price_note"]

        invoke(agent, {"mode": "ingest", "tenant_id": "t2",
                       "request_params": {"csv_payload": CSV}}, session="x")
        out = invoke(agent, {"mode": "generate", "tenant_id": "t2",
                             "request_params": {"facility_text": "宿", "season": "夏"}}, session="y")
        assert any(p["price_range"] for p in out["output"]["plans"])

    def test_price_null_when_history_is_other_season_only(self):
        """レビュー指摘 F-01/F-03: 他季節の実績しか無いとき価格を出してはならない。"""
        agent = build_agent(hitl=False)
        invoke(agent, {"mode": "ingest", "tenant_id": "t3",
                       "request_params": {
                           "csv_payload": "プラン名,季節,価格帯\n冬プラン,冬,30000-40000\n"}},
               session="w")
        out = invoke(agent, {"mode": "generate", "tenant_id": "t3",
                             "request_params": {"facility_text": "宿", "season": "夏"}},
                     session="z")
        assert all(p["price_range"] is None for p in out["output"]["plans"])
        assert "価格は未提示" in out["output"]["price_note"]


class TestFeedbackMode:
    def test_adoption_updates_kb(self):
        kb = KbService()
        agent = build_agent(kb=kb)
        invoke(agent, {"mode": "ingest", "tenant_id": "t1",
                       "request_params": {"csv_payload": CSV}}, session="a")
        out = invoke(agent, {"mode": "feedback", "tenant_id": "t1",
                             "request_params": {
                                 "adopted_plan": {"title": "海辺プラン", "season": "夏",
                                                  "pitch_text": "人間が磨いた訴求文"},
                                 "original_draft": {"title": "海辺プラン", "season": "夏",
                                                    "pitch_text": "生成そのまま"},
                             }}, session="b")
        assert out["status"] == SUCCESS
        row = kb._mem.get("t1", "nk:海辺プラン|夏")
        assert row["adopted_flag"] is True and row["operator_edited"] is True


class TestErrorPaths:
    def test_missing_tenant_id_errors_cleanly(self):
        agent = build_agent()
        out = invoke(agent, {"mode": "generate",
                             "request_params": {"facility_text": "x"}})
        assert out["status"] == ERROR

    def test_unknown_mode_errors_cleanly(self):
        agent = build_agent()
        out = invoke(agent, {"mode": "drop", "tenant_id": "t1"})
        assert out["status"] == ERROR

    def test_bad_csv_errors_cleanly(self):
        agent = build_agent()
        out = invoke(agent, {"mode": "ingest", "tenant_id": "t1",
                             "request_params": {"csv_payload": "onlyheader\n"}})
        assert out["status"] == ERROR


class TestProviderFailureReachesTheCaller:
    """A provider rejection once surfaced as `status: error` with `error_log: None`.

    Node-level tests prove the node reports the failure; only an end-to-end invoke proves
    the caller actually receives it (`AgentBaseGraph.get_output()` does not forward the
    channel by default).
    """

    class _Exploding(FakeLLM):
        def complete(self, messages):
            prompt = "\n".join(m.get("content", "") for m in messages)
            if "[TASK:profile]" in prompt:
                raise RuntimeError("content_filter: the prompt triggered the policy; echoed=CANARY_REQUEST_TEXT")
            return super().complete(messages)

    def _invoke(self):
        agent = build_agent(hitl=False, llm=self._Exploding())
        return agent.invoke(
            json.dumps({"mode": "generate", "tenant_id": "t1",
                        "request_params": {"facility_text": "海辺の宿", "season": "冬"}}),
            ctx=InvocationContext.for_internal(caller_id="e2e"),
            session_id="e2e-provider-failure",
        )

    def test_caller_receives_the_reason(self):
        result = self._invoke()
        assert result["status"] == AgentStatus.ERROR.value
        assert result.get("error_log"), "caller still gets no reason — issue #13 is not fixed"
        assert any("FacilityProfileNode" in entry for entry in result["error_log"])
        assert any("content_filtered" in entry for entry in result["error_log"])

    def test_the_provider_payload_never_reaches_the_caller(self):
        """The category is derived from the provider message; the message itself is dropped."""
        result = self._invoke()
        assert "CANARY_REQUEST_TEXT" not in json.dumps(result, ensure_ascii=False, default=str)

    def test_the_caller_sees_no_traceback(self):
        result = self._invoke()
        assert all("Traceback" not in entry for entry in result.get("error_log") or [])

    def test_the_reason_is_not_repeated(self):
        result = self._invoke()
        entries = result.get("error_log") or []
        assert len(entries) == len(set(entries))

    def test_the_failure_is_audited(self, monkeypatch):
        """S-4: the failure path emits its own domain event, not only the success path."""
        from src.nodes import generate_nodes

        seen: list[tuple[str, dict]] = []
        original = generate_nodes.emit_trace_event
        monkeypatch.setattr(
            generate_nodes, "emit_trace_event",
            lambda event, payload, state: (seen.append((event, payload)), original(event, payload, state))[1],
        )
        self._invoke()
        failed = [payload for name, payload in seen if name == "facility_profile_llm_failed"]
        assert failed, "no domain event was emitted for the provider failure"
        assert failed[0]["error_type"] == "RuntimeError"
        assert "CANARY_REQUEST_TEXT" not in json.dumps(failed[0], ensure_ascii=False)
