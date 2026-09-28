from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

import entry_adapter


def _invoke_app(*, preset_trust: TrustLevel | None = None, verify_scope=None):
    app = FastAPI()
    app.state.preset_trust = preset_trust
    if verify_scope is not None:
        app.state.verify_scope = verify_scope
    captured: dict[str, object] = {}

    @app.middleware("http")
    async def set_test_trust(request: Request, call_next):
        if app.state.preset_trust is not None:
            request.state.trust_level = app.state.preset_trust
        return await call_next(request)

    @app.post("/invoke")
    async def invoke(request: Request):
        trust, tenant_id, org_id = entry_adapter.authenticate_scope(request)
        captured["trust"] = trust
        captured["scope"] = {"tenant_id": tenant_id, "org_id": org_id}
        return {"status": "ok"}

    return app, captured


def test_package_exports_only_composed_authentication_surface():
    assert entry_adapter.__all__ == [
        "authenticate_scope",
        "VERIFIED_TENANT_STATE_ATTR",
        "VERIFIED_ORG_STATE_ATTR",
        "STG_DEFAULT_TENANT_ENV",
        "STG_DEFAULT_ORG_ENV",
    ]
    assert not hasattr(entry_adapter, "resolve_trust")
    assert not hasattr(entry_adapter, "require_verified_trust")
    assert not hasattr(entry_adapter, "require_scope")


@pytest.mark.parametrize("entry_token", [None, "mock-entry-token"])
def test_client_headers_cannot_supply_or_override_scope(monkeypatch, entry_token):
    monkeypatch.delenv("STG_DEFAULT_COMPANY_ID", raising=False)
    monkeypatch.delenv("STG_DEFAULT_PROJECT_ID", raising=False)
    if entry_token is None:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        headers = {"x-production-company-id": "tenant-other", "x-project-id": "org-other"}
    else:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", entry_token)
        headers = {
            "authorization": f"Bearer {entry_token}",
            "x-production-company-id": "tenant-other",
            "x-project-id": "org-other",
        }
    app, captured = _invoke_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers=headers)

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.parametrize(
    ("expected_token", "authorization", "expected_status"),
    [
        (None, None, 403),
        ("mock-entry-token", None, 401),
        ("mock-entry-token", "Bearer wrong-token", 401),
    ],
)
def test_stg_default_scope_does_not_rescue_anonymous_caller(
    monkeypatch, expected_token, authorization, expected_status
):
    if expected_token is None:
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", expected_token)
    monkeypatch.setenv("STG_DEFAULT_COMPANY_ID", "stg-tenant")
    monkeypatch.setenv("STG_DEFAULT_PROJECT_ID", "stg-org")
    app, captured = _invoke_app()
    headers = {} if authorization is None else {"authorization": authorization}

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers=headers)

    assert response.status_code == expected_status
    assert captured == {}


def test_token_authenticated_request_uses_stg_default_scope(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_COMPANY_ID", "stg-tenant")
    monkeypatch.setenv("STG_DEFAULT_PROJECT_ID", "stg-org")
    app, captured = _invoke_app()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["trust"] is TrustLevel.VERIFIED_EXTERNAL
    assert captured["scope"] == {"tenant_id": "stg-tenant", "org_id": "stg-org"}


def test_verify_scope_seam_takes_precedence_over_stg_default(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_COMPANY_ID", "stg-tenant")
    monkeypatch.setenv("STG_DEFAULT_PROJECT_ID", "stg-org")
    app, captured = _invoke_app(verify_scope=lambda _request: ("verified-tenant", "verified-org"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})

    assert response.status_code == 200
    assert captured["scope"] == {"tenant_id": "verified-tenant", "org_id": "verified-org"}


def test_internal_request_state_passes_without_token(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _invoke_app(
        preset_trust=TrustLevel.INTERNAL,
        verify_scope=lambda _request: ("internal-tenant", "internal-org"),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 200
    assert captured["trust"] is TrustLevel.INTERNAL


@pytest.mark.parametrize("preset_trust", [TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL])
def test_stg_default_never_fires_for_preset_trust_without_token_auth(monkeypatch, preset_trust):
    """STG default scope is reachable ONLY via entry-token authentication.

    A caller whose trust was set upstream (middleware) but whose verified scope
    is missing must get 403 even when STG env vars are present — otherwise a
    production deployment with leftover STG env silently serves the placeholder
    tenant scope (cross-tenant pollution). Kills the mutation that drops the
    token gate from the STG fallback.
    """
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("STG_DEFAULT_COMPANY_ID", "stg-tenant")
    monkeypatch.setenv("STG_DEFAULT_PROJECT_ID", "stg-org")
    app, captured = _invoke_app(preset_trust=preset_trust)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 403
    assert captured == {}


def test_anonymous_request_without_configured_token_is_forbidden(monkeypatch):
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    app, captured = _invoke_app(verify_scope=lambda _request: ("verified-tenant", "verified-org"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke")

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.parametrize("preset_trust", [TrustLevel.ANONYMOUS, "ANONYMOUS"])
def test_string_anonymous_middleware_is_promotable_by_token(monkeypatch, preset_trust):
    """Middleware may stamp trust_level as the enum OR its raw string value.

    _coerce_trust normalizes both to the enum, so a string "ANONYMOUS" must NOT
    silently skip token promotion (which an identity check `is ANONYMOUS` would).
    With a valid token the caller is promoted to VERIFIED_EXTERNAL.
    """
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    app, captured = _invoke_app(
        preset_trust=preset_trust,
        verify_scope=lambda _request: ("t", "o"),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/invoke", headers={"authorization": "Bearer mock-entry-token"})
    assert response.status_code == 200
    assert captured["trust"] is TrustLevel.VERIFIED_EXTERNAL
