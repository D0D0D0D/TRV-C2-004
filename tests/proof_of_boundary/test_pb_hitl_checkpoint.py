# PB-7: HITL interrupt propagation — interrupt() inside execute() raises
#        GraphInterrupt, propagates through BaseNode.__call__() and the
#        GraphNode subgraph boundary WITHOUT being caught by the application
#        error boundary (status is awaiting_human, never error).
# PB-5: Checkpoint safety — the suspended checkpoint contains only
#        msgpack-safe primitives and no credential-pattern strings.

import json

from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import AccommodationPlanBrandingAgent
from src.services.kb_service import KbService
from tests.fakes import FakeLLM, FakeVectorStore

AWAITING = AgentStatus.AWAITING_HUMAN.value
SUCCESS = AgentStatus.SUCCESS.value

_PRIMITIVES = (str, int, float, bool, type(None))


def _assert_msgpack_safe(value, path="state"):
    if isinstance(value, _PRIMITIVES):
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_msgpack_safe(v, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_msgpack_safe(v, f"{path}.{k}")
        return
    raise AssertionError(f"non-primitive object in checkpoint at {path}: {type(value)!r}")


def _build():
    agent = AccommodationPlanBrandingAgent(config={
        "kb_service": KbService(vector_store=FakeVectorStore()),
        "llm": FakeLLM(),
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 8},
    })
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _suspend(agent):
    ctx = InvocationContext(session_id="pb", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    payload = {"mode": "generate", "tenant_id": "t1",
               "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"}}
    return agent.invoke(json.dumps(payload, ensure_ascii=False), ctx=ctx)


class TestPB7InterruptPropagation:
    def test_interrupt_reaches_caller_not_error_boundary(self):
        agent = _build()
        out = _suspend(agent)
        assert out["status"] == AWAITING, (
            "GraphInterrupt must propagate through BaseNode.__call__() and the "
            f"GraphNode boundary — got status {out['status']!r}")
        assert out["thread_id"]
        assert out.get("hitl_metadata") is not None

    def test_resume_completes_to_success(self):
        """Suspend-only checks cannot catch the resume-loop failure mode
        (fresh inner checkpointer per get_subgraph() call re-interrupts
        forever — TourPlanningProcurementRoutingAgent). Assert the full round-trip terminates."""
        agent = _build()
        out = _suspend(agent)
        final = agent.resume(thread_id=out["thread_id"], feedback="approve")
        assert final["status"] == SUCCESS
        assert final["output"]["plans"]

    def test_multi_round_resume_terminates(self):
        agent = _build()
        out = _suspend(agent)
        out = agent.resume(thread_id=out["thread_id"], feedback={"instructions": "改善して"})
        assert out["status"] == AWAITING
        final = agent.resume(thread_id=out["thread_id"], feedback="approve")
        assert final["status"] == SUCCESS


class TestPB5CheckpointSafety:
    def test_suspended_checkpoint_is_primitives_only(self):
        agent = _build()
        out = _suspend(agent)
        snapshot = agent._compiled.get_state(
            {"configurable": {"thread_id": out["thread_id"]}})
        assert snapshot and snapshot.values
        _assert_msgpack_safe(dict(snapshot.values))

    def test_no_credential_patterns_in_checkpoint(self):
        from shared.security.credential_detector import detect_credentials
        agent = _build()
        out = _suspend(agent)
        snapshot = agent._compiled.get_state(
            {"configurable": {"thread_id": out["thread_id"]}})
        blob = json.dumps(dict(snapshot.values), ensure_ascii=False, default=str)
        assert detect_credentials(blob) == []

    def test_no_service_objects_in_checkpoint(self):
        """KbService / LLM instances are constructor-injected — they must
        never appear in the checkpointed state."""
        agent = _build()
        out = _suspend(agent)
        snapshot = agent._compiled.get_state(
            {"configurable": {"thread_id": out["thread_id"]}})
        blob = json.dumps(dict(snapshot.values), ensure_ascii=False, default=str)
        assert "KbService" not in blob and "FakeLLM" not in blob
