"""Single-operator authentication boundary (Phase 11F-H).

These tests exercise the REAL HTTP auth gate. They opt out of the conftest
token auto-injection (the `no_auto_auth` fixture) and drive the
Authorization header themselves, so a missing/invalid/expired token truly
reaches require_operator and is truly rejected with 401 before any route
runs. require_operator is never globally bypassed.

Everything PayPal/AI is mocked or trapped; no live call or money movement
occurs anywhere in this file.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import main
from app.main import app
from app.services import auth_service, purchase_execution_service
from tests.conftest import TEST_API_KEY

client = TestClient(app, raise_server_exceptions=False)


def login_token(api_key=TEST_API_KEY):
    resp = client.post("/auth/login", json={"api_key": api_key})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1 & 2 — login
# ---------------------------------------------------------------------------


def test_successful_login_returns_a_bearer_token(no_auto_auth):
    resp = client.post("/auth/login", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["token"].count(".") == 2
    assert body["expires_in"] == auth_service.DEFAULT_TOKEN_TTL_SECONDS
    # The secret/key must never be echoed back.
    assert TEST_API_KEY not in resp.text
    assert "secret" not in {k.lower() for k in body}


def test_incorrect_credential_is_401(no_auto_auth):
    resp = client.post("/auth/login", json={"api_key": "wrong-key"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3 — missing server configuration fails closed
# ---------------------------------------------------------------------------


def test_missing_config_fails_closed_for_login(no_auto_auth, monkeypatch):
    monkeypatch.delenv("PAYPILOT_AUTH_SECRET", raising=False)
    monkeypatch.delenv("PAYPILOT_API_KEY", raising=False)
    resp = client.post("/auth/login", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 503


def test_missing_config_fails_closed_for_protected_route(no_auto_auth, monkeypatch):
    # Even a well-formed token cannot be verified without the secret.
    token = login_token()
    monkeypatch.delenv("PAYPILOT_AUTH_SECRET", raising=False)
    resp = client.get("/auth/health", headers=bearer(token))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4-7 — token rejection cases
# ---------------------------------------------------------------------------


def test_missing_bearer_token_is_401(no_auto_auth):
    resp = client.get("/transactions")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "token-without-scheme",
        "Basic abc",
        "Bearer",
        "Bearer    ",
        "bearer ",
    ],
)
def test_malformed_authorization_header_is_401(no_auto_auth, header):
    resp = client.get("/transactions", headers={"Authorization": header})
    assert resp.status_code == 401


def test_invalid_signature_is_401(no_auto_auth):
    token = login_token()
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    resp = client.get("/transactions", headers=bearer(tampered))
    assert resp.status_code == 401


def test_malformed_token_is_401(no_auto_auth):
    for bad in ["a", "a.b", "a.b.c.d", "...", "notbase64!.x.y"]:
        resp = client.get("/transactions", headers=bearer(bad))
        assert resp.status_code == 401, bad


def test_expired_token_is_401(no_auto_auth):
    expired = auth_service.issue_token(ttl_seconds=-1)
    resp = client.get("/transactions", headers=bearer(expired))
    assert resp.status_code == 401


def test_a_token_signed_with_another_secret_is_401(no_auto_auth, monkeypatch):
    # Mint a token under a DIFFERENT secret, then verify under the real one.
    monkeypatch.setenv("PAYPILOT_AUTH_SECRET", "a-totally-different-secret")
    forged = auth_service.issue_token()
    monkeypatch.setenv("PAYPILOT_AUTH_SECRET", "test-auth-signing-secret-not-a-credential")
    resp = client.get("/transactions", headers=bearer(forged))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 8 & 9 — protection vs business logic; public routes
# ---------------------------------------------------------------------------

PROTECTED_GETS = [
    "/auth/health",
    "/transactions",
    "/transactions/txn_x",
    "/transactions/paypal/ORDER-X",
    "/discovery/approvals/appr_x/order",
    "/discovery/search",
    "/paypal/order/ORDER-X",
    "/sessions/sess_x",
]

PROTECTED_POSTS = [
    "/paypal/create-order",
    "/paypal/capture-order/ORDER-X",
    "/purchase/capture/ORDER-X",
    "/purchase/execute",
    "/purchase/approve",
    "/paypal/refund/CAP-X",
    "/transactions/txn_x/refund",
    "/discovery/compare",
    "/ai/plan",
    "/chat",
    "/sessions/sess_x/confirm",
    "/discovery/approvals/appr_x/create-order",
    "/discovery/approvals/appr_x/verify-approval",
    "/discovery/approvals/appr_x/authorize-capture",
    "/discovery/approvals/appr_x/capture",
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_get_rejected_without_token(no_auto_auth, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_POSTS)
def test_protected_post_rejected_without_token(no_auto_auth, path):
    assert client.post(path, json={}).status_code == 401


PUBLIC_GETS = ["/", "/products", "/products/search"]


@pytest.mark.parametrize("path", PUBLIC_GETS)
def test_public_get_reachable_without_token(no_auto_auth, path):
    # Reaches business logic (not 401). May be 200 or a validation error,
    # but never the auth gate.
    assert client.get(path).status_code != 401


def test_public_product_detail_reachable_without_token(no_auto_auth):
    resp = client.get("/products/does-not-exist")
    assert resp.status_code != 401  # 404 from business logic, not the gate


# ---------------------------------------------------------------------------
# 10 — webhooks: no bearer token, but still gated by PayPal signature
# ---------------------------------------------------------------------------


def test_webhook_needs_no_bearer_but_still_verifies_signature(no_auto_auth, monkeypatch):
    from app.services import webhook_service

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._p = payload

        def json(self):
            return self._p

    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "y")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "whid")

    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return FakeResponse(200, {"access_token": "t", "token_type": "Bearer"})
        if url.endswith("/v1/notifications/verify-webhook-signature"):
            return FakeResponse(200, {"verification_status": "FAILURE"})
        raise AssertionError(url)

    monkeypatch.setattr(webhook_service.httpx, "post", fake_post)

    headers = {
        "PAYPAL-TRANSMISSION-ID": "t",
        "PAYPAL-TRANSMISSION-TIME": "2026-10-11T10:00:00Z",
        "PAYPAL-TRANSMISSION-SIG": "s",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/c.pem",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    }
    # No Authorization header at all — the webhook is public to the operator
    # gate but its OWN signature check now runs and FAILS -> 400, not 401.
    resp = client.post(
        "/webhooks/paypal",
        json={"id": "WH", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}},
        headers=headers,
    )
    assert resp.status_code == 400  # signature FAILURE, never 401


# ---------------------------------------------------------------------------
# 11 — failed auth cannot trigger money movement
# ---------------------------------------------------------------------------


def test_unauthenticated_capture_never_reaches_capture_logic(no_auto_auth, monkeypatch):
    calls = []
    monkeypatch.setattr(
        purchase_execution_service,
        "capture_purchase",
        lambda *a, **k: calls.append("capture") or (_ for _ in ()).throw(AssertionError("must not run")),
    )
    # main.py imports capture_purchase by name; trap that reference too.
    monkeypatch.setattr(main, "capture_purchase", lambda *a, **k: calls.append("capture"))

    resp = client.post("/purchase/capture/ORDER-X")
    assert resp.status_code == 401
    assert calls == []  # the route body never executed


def test_unauthenticated_external_authorize_capture_never_runs(no_auto_auth, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "authorize_external_capture", lambda *a, **k: calls.append("authz"))
    resp = client.post("/discovery/approvals/appr_x/authorize-capture")
    assert resp.status_code == 401
    assert calls == []


# ---------------------------------------------------------------------------
# A valid token lets a protected route through to business logic
# ---------------------------------------------------------------------------


def test_valid_token_reaches_protected_business_logic(no_auto_auth):
    token = login_token()
    resp = client.get("/transactions", headers=bearer(token))
    assert resp.status_code == 200  # empty ledger, but past the gate
    assert "transactions" in resp.json()
