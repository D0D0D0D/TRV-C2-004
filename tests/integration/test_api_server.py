"""API-adapter integration tests — entry auth + HITL /resume ownership.

Exercises src/api/server.py over HTTP (FastAPI TestClient). Guards the retrofit
invariants: unauthenticated → 401 (enum/string ANONYMOUS); /resume ownership
fail-closed (403 foreign / 404 unknown); replay-after-completion → 409; a valid
owner reaches success.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.trust_level import TrustLevel
from src.services.kb_service import KbService
from tests.fakes import FakeLLM, FakeVectorStore

_TOKEN = "test-invoke-token-trv004"
_GENERATE = {"mode": "generate", "tenant_id": "t1",
             "request_params": {"facility_text": "海辺の温泉旅館", "season": "夏"}}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    # Inject FakeLLM before the server module's import-time build_agent() reaches
    # for a real Anthropic client (langchain_anthropic is not installed here).
    import src.services.llm_client as llc

    monkeypatch.setattr(llc, "build_llm", lambda *a, **k: FakeLLM())

    import importlib

    import src.api.server as server

    importlib.reload(server)
    agent = server.build_agent(kb_service=KbService(vector_store=FakeVectorStore()), llm=FakeLLM())
    agent.compile(checkpointer=InMemorySaver())
    server.agent = agent
    return TestClient(server.app), server


def _auth(token: str = _TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(session_id="sess-owner"):
    return {"input": json.dumps(_GENERATE, ensure_ascii=False), "session_id": session_id}


def test_invoke_without_token_is_401(client):
    c, _ = client
    assert c.post("/invoke", json=_body()).status_code == 401


def test_invoke_wrong_token_is_401(client):
    c, _ = client
    assert c.post("/invoke", json=_body(), headers=_auth("nope")).status_code == 401


@pytest.mark.parametrize("stamp", [TrustLevel.ANONYMOUS, "ANONYMOUS"])
def test_invoke_explicit_anonymous_middleware_is_401(client, stamp):
    c, server = client

    @server.app.middleware("http")
    async def _stamp(request, call_next):
        request.state.trust_level = stamp
        return await call_next(request)

    fresh = TestClient(server.app)
    assert fresh.post("/invoke", json=_body()).status_code == 401


def test_invoke_good_token_suspends_at_hitl(client):
    c, _ = client
    r = c.post("/invoke", json=_body(), headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "awaiting_human"
    assert r.json()["thread_id"]


def test_resume_without_token_is_401(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    r = c.post("/resume", json={"thread_id": th, "feedback": "approve", "session_id": "sess-owner"})
    assert r.status_code == 401


def test_resume_foreign_session_is_403(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    r = c.post("/resume", json={"thread_id": th, "feedback": "approve", "session_id": "attacker"},
               headers=_auth())
    assert r.status_code == 403


def test_resume_unknown_thread_is_404(client):
    c, _ = client
    c.post("/invoke", json=_body(), headers=_auth())
    r = c.post("/resume", json={"thread_id": "00000000-0000-0000-0000-000000000000",
                                "feedback": "approve", "session_id": "sess-owner"}, headers=_auth())
    assert r.status_code == 404


def test_resume_correct_owner_reaches_success(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    r = c.post("/resume", json={"thread_id": th, "feedback": "approve", "session_id": "sess-owner"},
               headers=_auth())
    assert r.status_code == 200
    assert r.json()["status"] == "success"


def test_invoke_returns_session_id_for_resume(client):
    """review TRV #2: when the caller omits session_id, /invoke generates one but
    MUST return it so the caller can echo it to /resume (else resume can never
    match ownership and 403s forever)."""
    c, _ = client
    r = c.post("/invoke", json={"input": json.dumps(_GENERATE, ensure_ascii=False)}, headers=_auth())
    assert r.status_code == 200
    sid = r.json().get("session_id")
    assert sid, "awaiting response must carry session_id"
    th = r.json()["thread_id"]
    resumed = c.post("/resume", json={"thread_id": th, "feedback": "approve", "session_id": sid},
                     headers=_auth())
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "success"


def test_resume_replay_after_completion_is_409(client):
    c, _ = client
    th = c.post("/invoke", json=_body(), headers=_auth()).json()["thread_id"]
    first = c.post("/resume", json={"thread_id": th, "feedback": "approve", "session_id": "sess-owner"},
                   headers=_auth())
    assert first.status_code == 200 and first.json()["status"] == "success"
    replay = c.post("/resume", json={"thread_id": th, "feedback": "approve", "session_id": "sess-owner"},
                    headers=_auth())
    assert replay.status_code == 409
