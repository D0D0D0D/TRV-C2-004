"""AgentCore Platform v1.0"""

# TRV-C2-004 — main-slot GraphNode wrapping the inner domain workflow.
#
# HITL wiring (docs/02_design.md §Data Flow 実装注意, verified on 1.0.0 wheel):
#   - propagate_hitl=True surfaces the inner PlanReviewNode interrupt to the
#     outer caller; the framework stores subgraph_thread_id and drives the
#     resume path inside GraphNode.execute().
#   - get_subgraph() MUST return the SAME inner instance every call — a fresh
#     inner graph + fresh checkpointer on resume loses the suspend checkpoint
#     and re-interrupts forever (TourPlanningProcurementRoutingAgent verified failure mode).
#   - The inner graph gets its own explicit compile(checkpointer=...) — the
#     framework does not auto-create checkpointers from config.

from __future__ import annotations

import json
import threading
from typing import Any, ClassVar

from langgraph.checkpoint.memory import InMemorySaver

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class PlanBrandingWorkflowNode(GraphNode):
    """Wraps PlanBrandingWorkflowGraph; assigned to the outer `main` slot."""

    # S-1: orchestrates facility-proprietary data — standard business operation.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = True

    def __init__(self, kb_service: Any = None, llm: Any = None,
                 workflow_config: dict | None = None) -> None:
        super().__init__()
        self._kb_service = kb_service
        self._llm = llm
        self._workflow_config = workflow_config or {}
        self._subgraph = None  # cached — same instance across invoke and resume
        self._subgraph_lock = threading.Lock()

    def get_subgraph(self):
        """Return the cached compiled inner graph, built once under a lock.

        get_subgraph() is called by GraphNode.execute() on BOTH the suspend
        invoke AND the resume, so it MUST return the SAME instance+saver each
        time — a fresh graph+saver per call loses the suspend checkpoint and
        re-interrupts forever. The build is deferred to first use (the inner
        graph needs kb_service/llm from config, which a bare construction in
        boundary tests does not supply), and guarded by a lock so two concurrent
        cold-start invokes cannot each compile a distinct inner instance/saver.
        """
        if self._subgraph is None:
            with self._subgraph_lock:
                if self._subgraph is None:
                    from src.graph.domain_workflow_graph import PlanBrandingWorkflowGraph
                    inner = PlanBrandingWorkflowGraph(config={
                        "kb_service": self._kb_service,
                        "llm": self._llm,
                        "memory_enabled": True,  # required for suspend/resume thread config
                        **self._workflow_config,
                    })
                    inner.compile(checkpointer=InMemorySaver())  # explicit — never implicit
                    self._subgraph = inner
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        """Serialize the validated request — custom outer-state fields do not
        cross the subgraph boundary, so everything travels as JSON text."""
        validated = state.get("validated_input")
        if validated:
            return str(validated)
        return json.dumps({
            "mode": state.get("mode", "generate"),
            "tenant_id": state.get("tenant_id", ""),
            "request_params": state.get("request_params", {}),
        }, ensure_ascii=False, default=str)

    def execute(self, state: AgentState) -> dict:
        # Clean early error for callers that reached main without a validated
        # request (also keeps PB-6 minimal-state invocation non-raising).
        if not state.get("validated_input") and not state.get("user_input"):
            return {"status": AgentStatus.ERROR.value,
                    "error_log": ["PlanBrandingWorkflowNode: no input to process"]}
        if state.get("status") == AgentStatus.ERROR.value:
            return {}  # upstream error passthrough (pre→main is unconditional)
        return super().execute(state)

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Map inner get_output() fields into the outer state delta.

        hitl_draft / hitl_feedback / hitl_status are included so a HITL
        round-trip survives the subgraph boundary (TourPlanningProcurementRoutingAgent requirement).
        """
        delta: dict = {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
        }
        for key in ("hitl_draft", "hitl_feedback", "hitl_status"):
            if sub_result.get(key) is not None:
                delta[key] = sub_result[key]
        if sub_result.get("error_log"):
            delta["error_log"] = sub_result["error_log"]
        return delta
