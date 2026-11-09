"""Shared test fixtures.

Two kinds of isolation, both autouse, both for the same reason: a test must
assert on the code, never on whatever happens to be in the developer's
backend/.env.

1. DATABASE. Every test runs against its own temporary SQLite file, so unit
   tests never pollute the real development database (backend/data/
   paypilot.db) and never collide with each other. One env var covers every
   table, because the ledger, the session store, the discovery evidence
   cache and the adopted-product store all resolve their path through
   PAYPILOT_DB_PATH.

2. EXTERNAL DISCOVERY CONFIG. The discovery provider and its API keys are
   pinned to inert test values. Without this, setting DISCOVERY_PROVIDER in
   a local .env silently redirects provider-agnostic tests at a real paid
   API: they stop exercising their own fakes, start making live billed
   requests, and fail for a reason that has nothing to do with the code
   under test.

   A test that wants a specific provider sets it with monkeypatch.setenv,
   which runs after this fixture and therefore wins. So pinning here
   constrains nothing a test asks for explicitly — it only removes the
   ambient default as a hidden input.
"""

import pytest
import starlette.testclient as _testclient_mod

# ---------------------------------------------------------------------------
# Single-operator authentication (Phase 11F-H) — test infrastructure.
#
# Dedicated, obviously-fake credentials, kept entirely separate from the
# developer's real .env (which conftest never reads). Every route except the
# public allowlist now requires `Authorization: Bearer <token>`, so ordinary
# business-logic tests must send one. Rather than editing ~2,100 call sites,
# a TestClient patch attaches a VALID token when a request has none — which
# means the REAL require_operator gate runs and really validates a real
# token on every request. The gate is never globally bypassed; the auth
# boundary tests opt OUT via the `no_auto_auth` fixture and then drive the
# header themselves (missing/invalid/expired).
# ---------------------------------------------------------------------------

TEST_API_KEY = "test-operator-key-not-a-credential"
TEST_AUTH_SECRET = "test-auth-signing-secret-not-a-credential"

#: Toggled off per-test by `no_auto_auth` so auth-boundary tests control the
#: Authorization header (including sending none).
_AUTOINJECT = {"enabled": True}


@pytest.fixture(autouse=True)
def _isolated_transactions_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(tmp_path / "test_transactions.db"))


@pytest.fixture(autouse=True)
def _auth_config(monkeypatch):
    """Pin test-only auth config and re-enable token auto-injection.

    These are not real credentials and never touch the developer's .env.
    """
    monkeypatch.setenv("PAYPILOT_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("PAYPILOT_AUTH_SECRET", TEST_AUTH_SECRET)
    _AUTOINJECT["enabled"] = True
    yield


@pytest.fixture(autouse=True)
def _authenticated_testclient(monkeypatch):
    """Attach a valid operator bearer token to TestClient requests that lack
    one, so the real auth gate runs and passes for ordinary tests.

    It injects ONLY when no Authorization header is present and injection is
    enabled, so a test that sets its own header (a bad/expired token) is left
    untouched, and a test using `no_auto_auth` can send no header at all.
    """
    original_request = _testclient_mod.TestClient.request

    def request(self, method, url, *args, **kwargs):
        if _AUTOINJECT["enabled"]:
            raw = kwargs.get("headers")
            headers = {} if raw is None else dict(raw)
            if not any(k.lower() == "authorization" for k in headers):
                from app.services.auth_service import issue_token

                headers["Authorization"] = f"Bearer {issue_token()}"
                kwargs["headers"] = headers
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(_testclient_mod.TestClient, "request", request)
    yield


@pytest.fixture
def no_auto_auth():
    """Disable auto-injection for a test that drives the Authorization header
    itself (used by the auth-boundary tests)."""
    _AUTOINJECT["enabled"] = False
    try:
        yield
    finally:
        _AUTOINJECT["enabled"] = True


@pytest.fixture(autouse=True)
def _pinned_discovery_config(monkeypatch):
    """Pin discovery config so no test inherits it from the real .env.

    channel3 is the documented Phase 10C/10D default, and the provider the
    provider-agnostic tests substitute a fake client for. The keys are
    obvious non-credentials: any test that reaches a real HTTP call with one
    of these fails loudly instead of quietly spending credits.
    """
    monkeypatch.setenv("DISCOVERY_PROVIDER", "channel3")
    monkeypatch.setenv("CHANNEL3_API_KEY", "test-channel3-key-not-a-credential")
    monkeypatch.setenv("SERPER_API_KEY", "test-serper-key-not-a-credential")
