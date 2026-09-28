"""AGENTIC STAR Marketplace entry-adapter contract tests for TRV-C2-004."""

from __future__ import annotations

import json
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import AccommodationPlanBrandingAgent
from src.nodes.pre_process_node import PreProcessNode
from tests.fakes import FakeLLM


class _Provider(SecretProvider):
    def get(self, key: str, default: str | None = None) -> str | None:
        del key
        return default


class _RecordingKb(cli._ReadOnlyKbService):
    def __init__(self) -> None:
        super().__init__()
        self.tenant_ids: list[str] = []

    def search_similar(self, query_text: str, tenant_id: str, **kwargs: Any) -> list[dict]:
        del query_text, kwargs
        self.tenant_ids.append(tenant_id)
        return []

    def price_stats(self, tenant_id: str, **kwargs: Any) -> dict:
        del kwargs
        self.tenant_ids.append(tenant_id)
        return {"sample_size": 0}


def _set_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://azure.example.test")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "test-deployment")
    monkeypatch.setenv("ASTAR_TENANT_ID", "tenant-marketplace")


def _invoke(monkeypatch: pytest.MonkeyPatch, payload: Any):
    _set_environment(monkeypatch)
    llm = FakeLLM()
    kb = _RecordingKb()
    agent = cli.MarketplaceGraph(
        config={
            "llm": llm,
            "kb_service": kb,
            "memory_enabled": True,
            "hitl": {"enabled": True, "max_hitl": 8},
            "max_revisions": 8,
        }
    )
    agent.compile()
    agent.provision_secrets(_Provider())
    user_input = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    result = agent.invoke(
        user_input,
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
    )
    return result, llm, kb


def test_input_context_is_discarded_and_hitl_is_disabled(monkeypatch):
    observed: dict[str, Any] = {}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, session_id
        observed.update(user_input=user_input, ctx=ctx, input_context=input_context)
        return {"status": "success", "output": "ok"}

    _set_environment(monkeypatch)
    monkeypatch.setattr(AccommodationPlanBrandingAgent, "invoke", fake_invoke)
    result = cli.MarketplaceGraph(config={"llm": FakeLLM()}).invoke(
        "海辺の宿",
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True),
        input_context={"conversation_history": ["CANARY"], "future_key": "CANARY"},
    )

    assert result["status"] == "success"
    assert observed["input_context"] == {}
    assert "CANARY" not in observed["user_input"]
    assert observed["ctx"].hitl_allowed is False


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
def test_provision_secrets_fails_closed_for_each_missing_value(monkeypatch, missing_key):
    _set_environment(monkeypatch)
    monkeypatch.delenv(missing_key)

    with pytest.raises(ValueError, match=missing_key):
        cli.MarketplaceGraph(config={"llm": FakeLLM()}).provision_secrets(_Provider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph(config={"llm": FakeLLM()}).provision_secrets(_Provider())


@pytest.mark.parametrize("bad_value", ["", " leading", "trailing ", "line\nbreak"])
def test_provision_secrets_rejects_unclean_values(monkeypatch, bad_value):
    _set_environment(monkeypatch)
    monkeypatch.setenv("ASTAR_TENANT_ID", bad_value)

    with pytest.raises(ValueError, match="ASTAR_TENANT_ID"):
        cli.MarketplaceGraph(config={"llm": FakeLLM()}).provision_secrets(_Provider())


def test_marketplace_graph_installs_default_llm():
    llm = cli.MarketplaceGraph(config={}).config["llm"]

    assert isinstance(llm, cli._AzureMarketplaceLLM)
    assert vars(llm) == {}


def test_marketplace_graph_preserves_basellm_instance():
    fake = FakeLLM()

    assert cli.MarketplaceGraph(config={"llm": fake}).config["llm"] is fake


def test_marketplace_graph_replaces_config_mapping_with_client():
    agent = cli.MarketplaceGraph(config={"llm": {"provider": "anthropic"}})

    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)


@pytest.mark.parametrize(
    "payload,forbidden",
    [
        (
            {
                "mode": "ingest",
                "tenant_id": "EVIL",
                "facility_text": "海辺の温泉旅館",
                "csv_payload": "TOP_LEVEL_CSV",
            },
            "TOP_LEVEL_CSV",
        ),
        (
            {
                "mode": "generate",
                "facility_text": "海辺の温泉旅館",
                "request_params": {
                    "mode": "ingest",
                    "tenant_id": "EVIL",
                    "csv_payload": "NESTED_CSV",
                },
            },
            "NESTED_CSV",
        ),
        (
            {
                "mode": "feedback",
                "tenant_id": "EVIL",
                "facility_text": "海辺の温泉旅館",
                "adopted_plan": {"title": "FORBIDDEN_ADOPTED_PLAN"},
            },
            "FORBIDDEN_ADOPTED_PLAN",
        ),
    ],
)
def test_pasted_json_cannot_select_write_mode_or_tenant(monkeypatch, payload, forbidden):
    observed_preprocess: dict[str, Any] = {}
    original_execute = PreProcessNode.execute

    def capture_preprocess(self, state):
        delta = original_execute(self, state)
        observed_preprocess.update(delta)
        return delta

    monkeypatch.setattr(PreProcessNode, "execute", capture_preprocess)
    result, llm, kb = _invoke(monkeypatch, payload)

    assert result["status"] == "success"
    assert result["output"]["mode"] == "generate"
    assert result["output"]["plans"]
    assert observed_preprocess["mode"] == "generate"
    assert observed_preprocess["tenant_id"] == "tenant-marketplace"
    assert kb.tenant_ids and set(kb.tenant_ids) == {"tenant-marketplace"}
    serialized = "\n".join(
        (
            json.dumps(observed_preprocess, ensure_ascii=False, default=str),
            json.dumps(result, ensure_ascii=False, default=str),
            *llm.calls,
        )
    )
    assert forbidden not in serialized
    assert "EVIL" not in serialized
    assert "CANARY" not in serialized
    assert len(llm.calls) >= 3


def test_read_only_kb_rejects_both_write_methods():
    kb = cli._ReadOnlyKbService()

    with pytest.raises(RuntimeError, match="read-only"):
        kb.upsert_plans([], "tenant")
    with pytest.raises(RuntimeError, match="read-only"):
        kb.append_feedback({}, "tenant")


def test_main_passes_loaded_config_and_marketplace_identity(monkeypatch):
    observed: dict[str, Any] = {}
    config = {"llm": {"provider": "anthropic"}}
    monkeypatch.setattr(cli, "load_agent_config", lambda path: config)

    def fake_runner(agent_cls, **kwargs):
        observed.update(agent_cls=agent_cls, **kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert observed == {
        "agent_cls": cli.MarketplaceGraph,
        "agent_name": "TRV-C2-004",
        "namespace": "trv",
        "config": config,
    }
    assert issubclass(FakeLLM, BaseLLM)


# ── production construction form ────────────────────────────────────────────────
# The tests above build the graph from a hand-written config dict. The runner does
# NOT: main() passes load_agent_config(<repo>) — the real config/config.yaml, which
# carries `llm: {provider: anthropic}` (a settings mapping, not a client) plus the
# real hitl/memory/kb keys. The tenant/mode forcing must be re-proved in that shape.
def _production_config() -> dict:
    from pathlib import Path

    return cli.load_agent_config(Path(cli.__file__).resolve().parent)


def test_production_config_llm_mapping_is_replaced_by_a_client():
    config = _production_config()
    assert config, "config/config.yaml did not load — the runner would build an unwired agent"
    assert isinstance(config.get("llm"), dict), "fixture drift: config.yaml no longer holds a mapping"
    agent = cli.MarketplaceGraph(config=config)
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert isinstance(agent.config["kb_service"], cli._ReadOnlyKbService)


def test_production_config_invoke_forces_generate_mode_and_deployment_tenant(monkeypatch):
    _set_environment(monkeypatch)
    observed: dict[str, Any] = {}
    original_execute = PreProcessNode.execute

    def capture(self, state):
        delta = original_execute(self, state)
        observed.update(delta)
        return delta

    monkeypatch.setattr(PreProcessNode, "execute", capture)

    llm = FakeLLM()
    kb = _RecordingKb()
    config = _production_config()
    config["llm"] = llm          # only the LLM/KB are faked; hitl/memory/kb keys are real
    config["kb_service"] = kb
    agent = cli.MarketplaceGraph(config=config)
    agent.compile()              # no checkpointer — exactly what the runner does

    result = agent.invoke(
        json.dumps(
            {
                "mode": "ingest",
                "tenant_id": "EVIL",
                "facility_text": "海辺の温泉旅館",
                "csv_payload": "PROD_FORM_CSV",
                "request_params": {"mode": "feedback", "tenant_id": "EVIL2", "csv_payload": "PROD_NESTED"},
            },
            ensure_ascii=False,
        ),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
    )

    assert result["status"] == "success"
    assert result["output"]["mode"] == "generate"
    assert observed["mode"] == "generate"
    assert observed["tenant_id"] == "tenant-marketplace"
    assert set(kb.tenant_ids) == {"tenant-marketplace"}
    assert len(llm.calls) >= 3
    serialized = "\n".join(
        (json.dumps(observed, ensure_ascii=False, default=str),
         json.dumps(result, ensure_ascii=False, default=str),
         *llm.calls)
    )
    for forbidden in ("EVIL", "EVIL2", "PROD_FORM_CSV", "PROD_NESTED", "CANARY"):
        assert forbidden not in serialized


def test_explicit_null_llm_and_kb_service_are_still_injected():
    """M5: a config.yaml with `llm: null` / `kb_service: null` HAS the key, so
    `setdefault` would be a silent no-op and the Pod would run unwired with every
    test green. The adapter must guard on the value, not on key presence."""
    agent = cli.MarketplaceGraph(config={"llm": None, "kb_service": None})
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert isinstance(agent.config["kb_service"], cli._ReadOnlyKbService)
