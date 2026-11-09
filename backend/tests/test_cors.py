"""Tests for the CORS configuration (Phase 8E Step 6 — Part A/B).

Proves the local Vite frontend origin is allowed (preflight and simple
requests), unrelated origins are not granted access, the configuration is
environment-overridable, no wildcard is used, and CORS changes nothing
about existing API behavior and triggers no Gemini/PayPal call.
"""

import pytest
from starlette.testclient import TestClient

from app.main import _cors_origins, app

client = TestClient(app, raise_server_exceptions=False)

FRONTEND = "http://localhost:5173"


@pytest.fixture(autouse=True)
def _no_ai_or_paypal(monkeypatch):
    """CORS handling is transport-level: no AI or PayPal call may happen
    in any test here (preflights never reach route handlers)."""
    import httpx

    from app.services.providers import gemini

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("CORS handling must not call Gemini or PayPal")

    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr(gemini, "GeminiProvider", _forbidden)


def test_preflight_from_frontend_origin_is_allowed():
    resp = client.options(
        "/chat",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == FRONTEND
    assert "POST" in resp.headers["access-control-allow-methods"]


def test_simple_response_carries_allow_origin_header():
    resp = client.get("/products", headers={"Origin": FRONTEND})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == FRONTEND


def test_unrelated_origin_is_not_granted_access():
    resp = client.get("/products", headers={"Origin": "https://evil.example.com"})
    # The API still answers (CORS is a browser gate), but no allow-origin
    # header is granted, so a browser would block the read.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers

    preflight = client.options(
        "/chat",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 400  # disallowed preflight


def test_no_wildcard_origin_in_configuration():
    assert "*" not in _cors_origins()


def test_origins_are_environment_configurable(monkeypatch):
    monkeypatch.setenv(
        "PAYPILOT_CORS_ORIGINS",
        "http://localhost:5173, https://paypilot.example.com",
    )
    assert _cors_origins() == [
        "http://localhost:5173",
        "https://paypilot.example.com",
    ]
    monkeypatch.delenv("PAYPILOT_CORS_ORIGINS")
    assert _cors_origins() == ["http://localhost:5173"]  # clean default


def test_existing_api_behavior_unchanged_without_origin():
    """Requests without an Origin header (curl, tests, server-to-server)
    behave exactly as before — same bodies, no CORS headers injected."""
    resp = client.get("/products")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert "access-control-allow-origin" not in resp.headers

    with_origin = client.get("/products", headers={"Origin": FRONTEND})
    assert with_origin.json() == resp.json()  # identical business payload
