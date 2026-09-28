"""AgentCore Platform v1.0"""

# TRV-C2-004 — LLM injection seam.
#
# Nodes receive a shared.services.llm BaseLLM instance via constructor
# injection (never via State — State is checkpointed to the DB).
# build_llm() resolves the provider from config; tests inject fakes directly.
#
# There is deliberately NO offline fallback provider (human decision
# 2026-07-02): missing/invalid LLM configuration must fail at startup rather
# than silently degrade the generated plans.

from __future__ import annotations

import json
import re
from typing import Any

from framework.errors import ConfigError
from shared.services.llm.base_llm import BaseLLM


def parse_llm_json(response: Any, fallback_key: str = "text") -> Any:
    """Extract JSON from a BaseLLM.complete() response dict (or raw string).

    Accepts ```json fenced``` or bare JSON; falls back to {fallback_key: raw}.
    """
    if isinstance(response, dict):
        raw = response.get("content") or response.get("text") or response.get("output") or ""
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
    else:
        raw = str(response)
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    candidate = (match.group(1) if match else raw).strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(candidate[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
        start = candidate.find("[")
        end = candidate.rfind("]")
        if 0 <= start < end:
            try:
                return json.loads(candidate[start:end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
        return {fallback_key: raw}


def build_llm(llm_config: dict | None) -> BaseLLM:
    """Resolve the LLM provider from config/config.yaml `llm`.

    Provider modules are imported lazily — anthropic/openai require optional
    langchain extras. Missing/unknown provider raises ConfigError at startup
    (fail fast — no silent degraded mode).
    """
    cfg = llm_config or {}
    provider = str(cfg.get("provider", "")).lower()
    if provider == "anthropic":
        from shared.services.llm.anthropic_client import AnthropicClient
        return AnthropicClient(**cfg.get("params", {}))
    if provider == "openai":
        from shared.services.llm.openai_client import OpenAIClient
        return OpenAIClient(**cfg.get("params", {}))
    raise ConfigError(
        f"[TRV-C2-004] config.llm.provider must be 'anthropic' or 'openai', got: {provider!r}. "
        "The agent does not start without a real LLM (no offline fallback — by design)."
    )
