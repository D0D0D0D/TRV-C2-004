"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
# object kinds). LangGraph checkpoints use msgpack serialization; non-primitive model objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or such model objects.

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """TRV-C2-004 Accommodation Plan Generation & Branding Agent state.

    Shared by the outer backbone graph and the inner domain workflow graph
    (docs/02_design.md §State Definition). Conditional-edge routing functions
    MUST annotate with this class, not AgentState — an AgentState annotation
    makes LangGraph project away the custom fields below and every branch
    falls through to the default.

    All values are JSON-serializable primitives/containers only.
    hitl_* fields are inherited from AgentState (schema 1.4) — never
    redeclared here; hitl_count / hitl_allowed are framework-owned.
    """

    # -- request routing -------------------------------------------------
    mode: str                    # "generate" (default) | "ingest" | "feedback"
    request_params: dict         # validated request payload (mode-specific)
    tenant_id: str               # facility tenant id — KB isolation key

    # -- generate mode ---------------------------------------------------
    facility_profile: dict       # structured facility profile (step ①)
    planning_context: dict       # season / trend / target context (step ②)
    reference_plans: list        # similar past plans from vector search (step ③)
    price_stats: dict            # price_band distribution from DB (step ③)
    plan_candidates: list        # raw generated plan drafts (step ④)
    plan_drafts: list            # refined plan drafts (step ⑤)
    revision_requests: list      # human revision instructions from HITL review
    revision_round: int          # template-owned loop counter (NOT hitl_count)
    review_decision: str         # "approved" | "revise" — inner routing flag

    # -- ingest mode -----------------------------------------------------
    parsed_rows: list            # validated CSV rows (step ①)
    skipped_rows: list           # rejected CSV rows with reasons (step ①)
    kb_records: list             # KB-schema mapped records (step ②)
    ingest_result: dict          # upsert counters / errors (step ③)

    # -- feedback mode ---------------------------------------------------
    feedback_result: dict        # appended plan key / metadata
