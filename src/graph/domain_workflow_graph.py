"""AgentCore Platform v1.0"""

# TRV-C2-004 — inner domain workflow graph (Cat 2, docs/02_design.md).
#
# Topology (mode branch right after the entry unpack node):
#
#   START → unpack ─┬─ generate → facility_profile → context_enrichment
#                   │             → past_plan_retrieval → plan_generation
#                   │             → refinement → plan_review ─┬─(revise)→ plan_generation
#                   │                                         └─(approved)→ END
#                   ├─ ingest   → csv_parse → schema_map → kb_bulk_upsert → END
#                   └─ feedback → plan_normalize → kb_append → END
#
# Routing functions are annotated with the template State class — an
# AgentState annotation would project away `mode`/`review_decision` and every
# branch would fall through to the default (Business Travel Hotel & Plan Recommendation Agent verified behaviour).

from __future__ import annotations

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.feedback_nodes import KbAppendNode, PlanNormalizeNode
from src.nodes.generate_nodes import (
    ContextEnrichmentNode,
    FacilityProfileNode,
    PastPlanRetrievalNode,
    PlanGenerationNode,
    RefinementNode,
)
from src.nodes.ingest_nodes import CsvParseNode, KbBulkUpsertNode, SchemaMapNode
from src.nodes.plan_review_node import PlanReviewNode
from src.nodes.unpack_node import UnpackNode
from src.schemas.state import State


class PlanBrandingWorkflowGraph(BaseGraph):
    """Inner multi-step workflow: 3 modes over the hybrid past-plan KB."""

    @property
    def name(self) -> str:
        return "plan_branding_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        if self.config.get("kb_service") is None:
            raise ConfigError("[PlanBrandingWorkflowGraph] config 'kb_service' is required")
        if self.config.get("llm") is None:
            raise ConfigError("[PlanBrandingWorkflowGraph] config 'llm' is required")

    def register_nodes(self) -> None:
        kb = self.config.get("kb_service")
        llm = self.config.get("llm")
        hitl = self.config.get("hitl") or {}
        self._nodes["unpack"] = UnpackNode()
        # generate mode
        self._nodes["facility_profile"] = FacilityProfileNode(llm=llm)
        self._nodes["context_enrichment"] = ContextEnrichmentNode()
        self._nodes["past_plan_retrieval"] = PastPlanRetrievalNode(kb_service=kb)
        self._nodes["plan_generation"] = PlanGenerationNode(llm=llm)
        self._nodes["refinement"] = RefinementNode(llm=llm)
        self._nodes["plan_review"] = PlanReviewNode(
            hitl_enabled=bool(hitl.get("enabled", True)),
            max_revisions=int(self.config.get("max_revisions", 8)),
            llm=llm,  # stage-2 intent classification for natural-language replies
        )
        # ingest mode
        self._nodes["csv_parse"] = CsvParseNode()
        self._nodes["schema_map"] = SchemaMapNode()
        self._nodes["kb_bulk_upsert"] = KbBulkUpsertNode(kb_service=kb)
        # feedback mode
        self._nodes["plan_normalize"] = PlanNormalizeNode()
        self._nodes["kb_append"] = KbAppendNode(kb_service=kb)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "unpack")
        self._sg.add_conditional_edges("unpack", self.route, {
            "generate": "facility_profile",
            "ingest": "csv_parse",
            "feedback": "plan_normalize",
            END: END,
        })
        # generate chain + HITL revision loop
        self._sg.add_edge("facility_profile", "context_enrichment")
        self._sg.add_edge("context_enrichment", "past_plan_retrieval")
        self._sg.add_edge("past_plan_retrieval", "plan_generation")
        self._sg.add_edge("plan_generation", "refinement")
        self._sg.add_edge("refinement", "plan_review")
        self._sg.add_conditional_edges("plan_review", self.route_review, {
            "plan_generation": "plan_generation",
            END: END,
        })
        # ingest chain
        self._sg.add_edge("csv_parse", "schema_map")
        self._sg.add_edge("schema_map", "kb_bulk_upsert")
        self._sg.add_edge("kb_bulk_upsert", END)
        # feedback chain
        self._sg.add_edge("plan_normalize", "kb_append")
        self._sg.add_edge("kb_append", END)

    def route(self, state: State) -> str:
        """Mode branch — State annotation is load-bearing (see module docstring)."""
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        mode = state.get("mode", "generate")
        if mode in ("generate", "ingest", "feedback"):
            return mode
        return END

    def route_review(self, state: State) -> str:
        """HITL review outcome branch."""
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        if state.get("review_decision") == "revise":
            return "plan_generation"
        return END

    def get_output(self, state: State) -> dict:
        """Shape sub_result for the outer PlanBrandingWorkflowNode.merge_output()."""
        mode = state.get("mode", "generate")
        output: dict = {"mode": mode}
        if mode == "generate":
            output["plans"] = state.get("plan_drafts") or []
            output["price_stats"] = state.get("price_stats") or {}
            output["revision_rounds"] = state.get("revision_round", 0)
        elif mode == "ingest":
            output["ingest_result"] = state.get("ingest_result") or {}
        elif mode == "feedback":
            output["feedback_result"] = state.get("feedback_result") or {}
        return {
            "output": output,
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
            "error_log": state.get("error_log", []),
            "hitl_draft": state.get("hitl_draft"),
            "hitl_feedback": state.get("hitl_feedback"),
            "hitl_status": state.get("hitl_status"),
        }
