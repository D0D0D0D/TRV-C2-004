# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# PB-7: HITL Interrupt Propagation — exact-name file required by the
# scaffold-integrity check for HITL-enabled templates.
#
# This template enables HITL (PlanReviewNode calls interrupt()), so the
# assertions below run for real. Substantive coverage (revise-loop, checkpoint
# safety) lives in tests/integration/test_agent_modes.py and
# test_pb_hitl_checkpoint.py; this exact-name file re-exercises the core
# suspend + resume boundary and the hitl_allowed=False guard.

from __future__ import annotations

import json
import pathlib
import warnings

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import AccommodationPlanBrandingAgent
from src.services.kb_service import KbService
from tests.fakes import FakeLLM, FakeVectorStore

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)

_GENERATE = {"mode": "generate", "tenant_id": "t1",
             "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"}}


def _agent():
    agent = AccommodationPlanBrandingAgent(config={
        "kb_service": KbService(vector_store=FakeVectorStore()),
        "llm": FakeLLM(),
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 8},
        "max_revisions": 8,
        "max_retry": 3,
    })
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _ctx(session):
    return InvocationContext(session_id=session, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def test_pb7_interrupt_surfaces_as_awaiting_human_not_error():
    agent = _agent()
    out = agent.invoke(json.dumps(_GENERATE, ensure_ascii=False), ctx=_ctx("pb7-suspend"))
    assert out["status"] == AgentStatus.AWAITING_HUMAN.value
    assert not out.get("error_log")


def test_pb7_resume_reaches_success():
    """resume() must reach SUCCESS — guards the get_subgraph caching / lost-
    checkpoint reinterrupt failure mode."""
    agent = _agent()
    out = agent.invoke(json.dumps(_GENERATE, ensure_ascii=False), ctx=_ctx("pb7-resume"))
    assert out["status"] == AgentStatus.AWAITING_HUMAN.value
    final = agent.resume(thread_id=out["thread_id"], feedback="approve")
    assert final["status"] == AgentStatus.SUCCESS.value
    assert final["output"]["plans"]


def test_pb7_hitl_allowed_false_skips_interrupt():
    """hitl_allowed=False must skip interrupt() and auto-approve — no deadlock.

    NOTE: mode="generate" is required, otherwise execute() returns {} early
    (before the guard) and the assertion would pass vacuously (review TRV #4)."""
    from src.nodes.plan_review_node import PlanReviewNode

    node = PlanReviewNode()
    out = node.execute({"mode": "generate", "plan_drafts": [{"name": "x"}], "hitl_allowed": False})
    # The guard was actually reached: it returns an explicit approved decision.
    assert out.get("review_decision") == "approved"
    assert out.get("status") == AgentStatus.SUCCESS.value
    assert "__interrupt__" not in out
