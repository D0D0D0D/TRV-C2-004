"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.
#
# TRV-C2-004 wiring:
#   - config/config.yaml feeds the graph config.
#   - Services (KbService / LLM) are constructed here and injected via config
#     — never through State (State is checkpointed to the DB).
#   - HITL: outer graph gets an explicit compile(checkpointer=...) — the
#     framework does not auto-create checkpointers. /resume continues a
#     suspended review session.

from __future__ import annotations

import pathlib
import threading
import weakref
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import AccommodationPlanBrandingAgent
from src.services.kb_service import KbService
from src.services.llm_client import build_llm

_RUNTIME_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _load_config() -> dict:
    config = yaml.safe_load(_RUNTIME_CONFIG.read_text(encoding="utf-8")) or {}
    return dict(config)


def build_agent(config: dict | None = None,
                kb_service: Any = None, llm: Any = None) -> AccommodationPlanBrandingAgent:
    """Assemble the agent with injected services (tests pass fakes here)."""
    cfg = dict(config if config is not None else _load_config())
    cfg["kb_service"] = kb_service if kb_service is not None else KbService(
        top_k=int((cfg.get("kb") or {}).get("top_k", 5)),
    )
    cfg["llm"] = llm if llm is not None else build_llm(cfg.get("llm"))
    agent = AccommodationPlanBrandingAgent(config=cfg)
    # HITL requires an explicit checkpointer on the OUTER graph too
    # (inner checkpointer lives in PlanBrandingWorkflowNode.get_subgraph()).
    agent.compile(checkpointer=InMemorySaver())
    return agent


app = FastAPI(title="TRV-C2-004 Accommodation Plan Generation & Branding Agent")

agent = build_agent()
agent.provision_secrets(secrets_factory(namespace="trv", agent_name="trv_c2_004"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


class ResumeRequest(BaseModel):
    thread_id: str
    feedback: Any
    session_id: str = ""


def _authenticated_ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication.

    Entry authentication is implemented in entry_adapter.auth:
    verified middleware trust is used as-is; INVOKE_AUTH_TOKEN Bearer auth
    promotes ANONYMOUS → VERIFIED_EXTERNAL (401 on mismatch). An unauthenticated
    caller (still ANONYMOUS, no token promotion) → 401, replacing the old
    fallback that let ANONYMOUS callers reach the graph. This template has no
    tenant/org request-scope model, so the entry-adapter scope layer is
    intentionally not wired (only the auth core).
    """
    trust, token_authenticated = _resolve_trust(request)
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


def _require_resume_ownership(thread_id: str, ctx: InvocationContext) -> None:
    """Fail-closed checkpoint ownership check for HITL resume.

    Verify the suspended checkpoint exists (404) and its recorded session_id
    matches (403); a checkpoint with no session_id is un-resumable. Require a
    pending HITL wait (snapshot.next non-empty AND subgraph_thread_id set) so a
    completed/replayed resume is rejected (409).

    Ownership model & limitation (review TRV #1): session_id is the primary owner
    key. Under a single global INVOKE_AUTH_TOKEN deployment, token-authenticated
    callers all share caller_id="", so this defends against a caller who does NOT
    know the victim's session_id — it does NOT stop a caller who shares the token
    AND already knows the victim's (session_id, thread_id). Binding ownership to a
    verified principal is the robust fix, and it engages automatically in a real
    deployment: the platform auth middleware stamps request.state.caller_id, which
    the caller_id check below then enforces. The token-only STG path is intended
    for single-tenant STG verification, not multi-tenant production.

    Atomicity: the check + agent.resume() are made mutually exclusive per thread
    by a keyed lock in the /resume handler (single-process). A durable, multi-
    process deployment must replace that lock with a per-thread lease/CAS.
    """
    compiled = getattr(agent, "_compiled", None)
    if compiled is None:
        raise HTTPException(status_code=409, detail="No suspended session for this thread.")
    snapshot = compiled.get_state({"configurable": {"thread_id": thread_id}})
    values = getattr(snapshot, "values", None) or {}
    if not values:
        raise HTTPException(status_code=404, detail="No suspended session for this thread.")
    owner_session = values.get("session_id")
    if not owner_session or owner_session != ctx.session_id:
        raise HTTPException(status_code=403, detail="Session does not own this thread.")
    owner_caller = values.get("caller_id") or ""
    if owner_caller and owner_caller != (ctx.caller_id or ""):
        raise HTTPException(status_code=403, detail="Caller does not own this thread.")
    if not getattr(snapshot, "next", None) or not values.get("subgraph_thread_id"):
        raise HTTPException(status_code=409, detail="Thread is not awaiting human input.")


# Per-thread locks serialize the ownership-check + resume for one thread within
# this process (review TRV #3). WeakValueDictionary so entries drop when no
# request holds the lock. A durable, multi-process deployment must replace this
# with a per-thread lease/CAS in the shared checkpoint store.
_RESUME_LOCKS: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()
_RESUME_LOCKS_GUARD = threading.Lock()


def _resume_lock(thread_id: str) -> threading.Lock:
    with _RESUME_LOCKS_GUARD:
        lock = _RESUME_LOCKS.get(thread_id)
        if lock is None:
            lock = threading.Lock()
            _RESUME_LOCKS[thread_id] = lock
        return lock


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _authenticated_ctx(request, req.session_id)
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(req.input, ctx=ctx)
    # Surface the session_id the caller must echo to /resume (review TRV #2): when
    # the request omits session_id one is generated here, and without returning it
    # the caller can never match ownership on resume. Non-destructive: add only if
    # the graph output does not already carry it.
    if isinstance(result, dict) and "session_id" not in result:
        result = {**result, "session_id": ctx.session_id}
    return result


@app.post("/resume")
async def resume(req: ResumeRequest, request: Request):
    """Continue a suspended HITL plan-review session.

    feedback: "approve" (or {"action": "approve"}) to accept the drafts,
    anything else (e.g. {"instructions": "もっと安く"}) loops a revision.

    The caller MUST send back the session_id returned by the awaiting /invoke
    response (ownership key).
    """
    ctx = _authenticated_ctx(request, req.session_id)
    # Serialize check + resume for this thread so a concurrent duplicate cannot
    # slip between the pending-state check and agent.resume() (review TRV #3).
    with _resume_lock(req.thread_id):
        _require_resume_ownership(req.thread_id, ctx)
        with bound_secrets(agent._secrets_provider):
            return agent.resume(thread_id=req.thread_id, feedback=req.feedback)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "trv_c2_004"}
