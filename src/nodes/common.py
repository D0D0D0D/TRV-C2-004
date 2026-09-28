"""AgentCore Platform v1.0"""

# TRV-C2-004 — shared node helpers.
#
# Inner-graph nodes on linear edges run even when an upstream node failed
# (custom BaseGraph topology has no framework routing between them), so every
# node opens with an upstream-error passthrough guard.

from __future__ import annotations

from framework.schemas.agent_status import AgentStatus


def upstream_error(state: dict) -> bool:
    """True when an upstream node already put the run into a terminal error."""
    return state.get("status") == AgentStatus.ERROR.value or state.get("status") == AgentStatus.ERROR
