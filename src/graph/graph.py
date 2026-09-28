"""AgentCore Platform v1.0"""

# TRV-C2-004 — outer graph (Cat 2).
#
# AgentBaseGraph fixed 5-node backbone; the domain workflow (3 modes ×
# hybrid past-plan KB × HITL review loop) is encapsulated inside
# PlanBrandingWorkflowNode (`main` slot) → src/graph/domain_workflow_graph.py.
#
# add_edges() is NOT overridden — backbone wiring belongs to the framework.
#
# config keys (config/config.yaml, services injected by the
# entry point — see src/api/server.py):
#   kb_service      KbService instance (hybrid DB + vector store)
#   llm             shared.services.llm BaseLLM instance
#   memory_enabled  true — checkpointer required for HITL pause/resume
#   hitl            {enabled: true, max_hitl: 8}
#   max_revisions   template-owned HITL revision ceiling (default 8)

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.plan_branding_workflow_node import PlanBrandingWorkflowNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class AccommodationPlanBrandingAgent(AgentBaseGraph):
    """Accommodation Plan Generation & Branding Agent (TRV-C2-004)."""

    @property
    def name(self) -> str:
        return "trv_c2_004"

    @property
    def state_schema(self) -> type:
        return State

    def get_output(self, state: State) -> dict:
        """Surface `error_log` to the caller.

        `AgentBaseGraph.get_output()` returns output / status / trace_id /
        correlation_id / node_history only, so a node that reported *why* it failed
        still reached the caller as `status: error` with no reason attached.
        The inner workflow already carries the reason across the GraphNode
        boundary; this hands it on.

        Entries are de-duplicated: `error_log` is an additive channel, so one failure
        observed by several nodes on the way to `END` would otherwise be repeated.
        """
        output = super().get_output(state)
        errors: list[str] = []
        for entry in state.get("error_log") or []:
            # The framework appends the full traceback of an unhandled exception to
            # this channel. The caller needs the reason, not our file paths and
            # frames — keep the first line and leave the trace in State for debugging.
            reason = str(entry).split("\nTraceback", 1)[0].strip()
            if reason and reason not in errors:
                errors.append(reason)
        if errors:
            output["error_log"] = errors
        return output

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = PlanBrandingWorkflowNode(
            kb_service=self.config.get("kb_service"),
            llm=self.config.get("llm"),
            workflow_config={
                "hitl": self.config.get("hitl") or {},
                "max_revisions": self.config.get("max_revisions", 8),
            },
        )
        self._nodes["post_process"] = PostProcessNode()


# Backwards-compatible alias for scaffold-era references.
Graph = AccommodationPlanBrandingAgent
