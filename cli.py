"""AGENTIC STAR Marketplace entry adapter for TRV-C2-004."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0/1.0.1 compatibility.
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0/1.0.1 has no Marketplace extra.
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0/1.0.1 compatibility.
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import AccommodationPlanBrandingAgent
from src.services.kb_service import KbService

_NAMESPACE = "trv"
_AGENT_NAME = "TRV-C2-004"
_PROVIDER_AGENT_NAME = "trv_c2_004"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, "ASTAR_TENANT_ID")
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only the four deployment values consumed by this adapter."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required value without silently repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureOpenAIClientCompat(BaseLLM):
    """Minimal Azure chat client for SDK versions without the Azure extra."""

    def __init__(self, config: dict[str, str], *, tools: tuple[Any, ...] = ()) -> None:
        self._config = dict(config)
        self._tools = tools

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21"
        body: dict[str, Any] = {"messages": messages}
        if self._tools:
            body["tools"] = list(self._tools)
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        return type(self)(self._config, tools=tuple(tools))


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Resolve Azure connection values per operation and retain none of them."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        # Resolve deployment values before importing the optional Azure client,
        # so a missing secret is never misreported as an optional-extra issue.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0/1.0.1 compatibility.
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": values["AZURE_OPENAI_API_KEY"],
                "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


class _ReadOnlyKbService(KbService):
    """Keep generate-mode reads available while making KB writes impossible."""

    def upsert_plans(self, records: list[dict], tenant_id: str) -> dict:
        del records, tenant_id
        raise RuntimeError("Marketplace KB is read-only: upsert_plans is disabled")

    def append_feedback(self, record: dict, tenant_id: str, operator_edited: bool = False) -> dict:
        del record, tenant_id, operator_edited
        raise RuntimeError("Marketplace KB is read-only: append_feedback is disabled")


def _marketplace_user_input(user_input: str) -> str:
    """Rebuild input as a tenant-bound generate packet with no write payload."""

    try:
        decoded = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        decoded = None
    packet = dict(decoded) if isinstance(decoded, dict) else {"facility_text": user_input}

    packet.pop("csv_payload", None)
    packet.pop("adopted_plan", None)
    nested = packet.get("request_params")
    if isinstance(nested, dict):
        nested = dict(nested)
        nested.pop("csv_payload", None)
        nested.pop("adopted_plan", None)
        packet["request_params"] = nested

    # These assignments are intentionally unconditional: caller JSON must not
    # select a write branch or a different tenant than the deployment scope.
    packet["mode"] = "generate"
    packet["tenant_id"] = os.environ["ASTAR_TENANT_ID"]
    return json.dumps(packet, ensure_ascii=False)


class MarketplaceGraph(AccommodationPlanBrandingAgent):
    """Marketplace-only tenant, read-only KB, Azure, and HITL boundary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        marketplace_config = dict(config or {})
        # config.yaml contains an LLM settings mapping, not a client. Nodes
        # capture the concrete client during compile(), before secrets arrive.
        if not isinstance(marketplace_config.get("llm"), BaseLLM):
            marketplace_config["llm"] = _AzureMarketplaceLLM()
        kb_config = marketplace_config.get("kb") or {}
        # Guard on the value, not on key presence: a config.yaml carrying an
        # explicit `kb_service: null` already HAS the key, so setdefault would
        # silently no-op and the inner graph would fail closed at first invoke.
        if marketplace_config.get("kb_service") is None:
            marketplace_config["kb_service"] = _ReadOnlyKbService(
                top_k=int(kb_config.get("top_k", 5))
            )
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input),
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context={},
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
