"""Operator-gated create-order REPLAY recovery (Phase 11F-K.22b).

The ambiguous orphan the read-first K.21 / K.17 paths cannot settle: a CREATING
reservation with NO persisted PayPal order id, whose create-order outcome was
never learned. K.22a persisted a stable PayPal-Request-Id, so an operator can
REPLAY create_order with that EXACT id, but ONLY while the reservation is
demonstrably inside a conservative idempotency window and under a durable
single-flight claim. The replay response is never trusted as proof: the
returned id is persisted via the guarded K.20/K.16 claim and then
INDEPENDENTLY re-read and verified before finalization.

Covered here, for BOTH the catalog and external paths:
  1.  Eligible recent attempt replays with the EXACT persisted request id.
  2.  Expired attempt is not replayed.
  3.  Missing request id / invalid timestamp is not replayed.
  4.  Future / timezone-ambiguous timestamp rejected safely.
  5.  Replay returns an existing order id -> persisted + independently verified.
  6.  Conflicting id and amount/currency mismatch fail closed.
  7.  Transport failure leaves the reservation unresolved, NOT FAILED.
  8.  Non-2xx / non-JSON 2xx / malformed / missing id -> no false success.
  9.  A subsequent retry reuses the SAME request id, still window-gated.
  10. Concurrent recovery -> at most one in-flight provider call.
  11. A stale claim cannot permit a replay outside the original window.
  12. Product-unavailable / read-first cases still correct.
  13. No automatic replay from confirmation / webhook / background execution.
  14. Money-operation traps prove no capture / authorize / refund / cancel.

PayPal is fully mocked; local SQLite only. Clocks are deterministic (the
service functions accept now / window_seconds overrides).
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.catalog_attempt import CatalogAttemptStatus
from app.schemas.external_purchase import ExternalOrderStatus
from app.services import (
    catalog_attempt_service as cas,
    external_purchase_service as eps,
    purchase_execution_service as pes,
    refund_service,
    transaction_service,
)
from app.services.catalog_reconciliation_service import reconcile_catalog_attempt_event
from app.services.currency_service import convert_inr_to_usd

# External 11E chain fixtures (adopted product -> snapshot -> approval -> clock).
from test_external_purchase_order import (  # noqa: F401 - pytest fixtures
    adopted,
    approved_approval,
    clock,
    no_ai,
    no_money_moves,
    require_approval,
    snapshot,
    FRESH_OBSERVED,
    NOW,
)

client = TestClient(app, raise_server_exceptions=False)

PRODUCT_ID = "HDP006"  # boAt Rockerz 450, 1499 INR
PRICE_INR = 1499
AMOUNT = convert_inr_to_usd(PRICE_INR)  # "16.66"
CURRENCY = "USD"
ORDER_ID = "REPLAYORDER-1"

WINDOW = paypal.DEFAULT_CREATE_ORDER_RECOVERY_WINDOW_SECONDS  # 10800s (3h)


# ===========================================================================
# Shared traps & builders
# ===========================================================================


@pytest.fixture
def no_money(monkeypatch):
    """Trap every capture / authorize / refund / cancel path. Returns hits.

    create_order is NOT trapped here (recovery is supposed to replay it); the
    individual tests mock it to the outcome under test and assert on the
    request id. get_order is NOT trapped (recovery re-reads through it).
    """
    hits: list[str] = []

    def bomb(name):
        def _b(*a, **k):
            hits.append(name)
            raise AssertionError(f"recovery must not call {name}")

        return _b

    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
    monkeypatch.setattr(pes, "capture_order", bomb("pes.capture_order"))
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
    return hits


def catalog_fake_order(order_id=ORDER_ID, status="CREATED", value=AMOUNT, currency=CURRENCY):
    return {
        "order_id": order_id,
        "order_status": status,
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": value}}],
        "approve_url": None,
    }


def reserve_catalog_no_id():
    """A real CREATING catalog attempt: request id persisted, NO order id."""
    rec = cas.reserve_attempt(PRODUCT_ID, PRICE_INR, AMOUNT, CURRENCY)
    assert rec.status is CatalogAttemptStatus.CREATING
    assert rec.paypal_order_id is None
    assert rec.paypal_request_id == rec.attempt_id  # the original, derived id
    return rec


def raw_insert_catalog(created_at, *, request_id, order_id=None, status=CatalogAttemptStatus.CREATING, recovery_claimed_at=None):
    """Raw-insert a catalog attempt with chosen created_at / request id / claim."""
    attempt_id = f"cpa_{uuid.uuid4().hex}"
    stamp = created_at if isinstance(created_at, str) else created_at.isoformat()
    cols = (
        "attempt_id", "product_id", "catalog_price_inr", "paypal_amount",
        "paypal_currency", "paypal_order_id", "claim_key", "paypal_request_id",
        "recovery_claimed_at", "status", "transaction_id", "created_at", "updated_at",
    )
    with sqlite3.connect(cas._db_path()) as conn:
        conn.executescript(cas._SCHEMA)
        conn.execute(
            f"INSERT INTO catalog_purchase_attempts ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            (
                attempt_id, PRODUCT_ID, PRICE_INR, AMOUNT, CURRENCY, order_id,
                f"cpa_claim_{uuid.uuid4().hex}",
                request_id if request_id is not None else None,
                recovery_claimed_at,
                status.value if isinstance(status, CatalogAttemptStatus) else status,
                None, stamp, stamp,
            ),
        )
        conn.commit()
    return cas.get(attempt_id)


def tamper_catalog(attempt_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    with sqlite3.connect(cas._db_path()) as conn:
        conn.execute(
            f"UPDATE catalog_purchase_attempts SET {sets} WHERE attempt_id = ?",
            (*cols.values(), attempt_id),
        )
        conn.commit()
    return cas.get(attempt_id)


def dt(rec):
    return datetime.fromisoformat(rec.created_at)


# ===========================================================================
# CATALOG — 1. Eligible recent attempt replays with the EXACT request id
# ===========================================================================


def test_catalog_eligible_replays_with_exact_request_id(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    seen = {}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        seen["amount"] = amount
        seen["currency"] = currency
        seen["request_id"] = request_id
        return {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None}

    reads = []

    def fake_get_order(oid):
        reads.append(oid)
        return catalog_fake_order(order_id=oid)

    monkeypatch.setattr(pes, "create_order", fake_create_order)
    monkeypatch.setattr(pes, "get_order", fake_get_order)

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    # The EXACT persisted request id was replayed, with the recorded amount.
    assert seen["request_id"] == attempt.paypal_request_id == attempt.attempt_id
    assert seen["amount"] == AMOUNT and seen["currency"] == CURRENCY
    # The order id was persisted and then INDEPENDENTLY re-read before finalize.
    assert reads == [ORDER_ID]
    assert outcome == "recovered_finalized"
    assert updated.status is CatalogAttemptStatus.FINALIZED
    txn = transaction_service.get_transaction_by_order(ORDER_ID)
    assert txn.status.value == "ORDER_CREATED" and txn.paypal_amount == AMOUNT
    assert no_money == []


# ===========================================================================
# CATALOG — 2. Expired attempt is NOT replayed
# ===========================================================================


def test_catalog_expired_not_replayed(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    monkeypatch.setattr(
        pes, "create_order", lambda **k: pytest.fail("expired must not replay")
    )
    later = dt(attempt) + timedelta(seconds=WINDOW + 60)

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=later)

    assert outcome == "manual_review_required"
    assert detail["reason"] == "WINDOW_EXPIRED"
    assert updated.status is CatalogAttemptStatus.CREATING
    assert updated.paypal_order_id is None
    assert cas.get(attempt.attempt_id).recovery_claimed_at is None  # never claimed
    assert no_money == []


# ===========================================================================
# CATALOG — 3. Missing request id / invalid timestamp not replayed
# ===========================================================================


def test_catalog_missing_request_id_not_replayed(monkeypatch, no_money):
    attempt = raw_insert_catalog(datetime.now(timezone.utc), request_id=None)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("must not replay"))

    outcome, updated, detail = pes.recover_creating_attempt(attempt)

    assert outcome == "manual_review_required"
    assert detail["reason"] == "NO_PERSISTED_REQUEST_ID"
    assert updated.status is CatalogAttemptStatus.CREATING
    assert no_money == []


def test_catalog_malformed_timestamp_not_replayed(monkeypatch, no_money):
    attempt = raw_insert_catalog("not-a-timestamp", request_id="cpa_" + "a" * 32)
    # Give the stored request id a real id to pass the id checks and reach ts.
    attempt = tamper_catalog(attempt.attempt_id, paypal_request_id=attempt.attempt_id)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("must not replay"))

    outcome, updated, detail = pes.recover_creating_attempt(attempt)

    assert outcome == "manual_review_required"
    assert detail["reason"] == "TIMESTAMP_MALFORMED"
    assert no_money == []


def test_catalog_non_original_request_id_not_replayed(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    # A request id that is valid but NOT this attempt's original derived id.
    tamper_catalog(attempt.attempt_id, paypal_request_id="cpa_" + "z" * 32)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("must not replay"))

    outcome, updated, detail = pes.recover_creating_attempt(cas.get(attempt.attempt_id))

    assert outcome == "manual_review_required"
    assert detail["reason"] == "REQUEST_ID_NOT_ORIGINAL"
    assert no_money == []


# ===========================================================================
# CATALOG — 4. Future / timezone-ambiguous timestamp rejected safely
# ===========================================================================


def test_catalog_future_timestamp_rejected(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("must not replay"))
    # Clock earlier than creation: the attempt is dated in the future.
    earlier = dt(attempt) - timedelta(hours=2)

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=earlier)

    assert outcome == "manual_review_required"
    assert detail["reason"] == "TIMESTAMP_IN_FUTURE"
    assert no_money == []


def test_catalog_tz_naive_timestamp_rejected(monkeypatch, no_money):
    attempt = raw_insert_catalog("2026-10-14T12:00:00", request_id="x")
    tamper_catalog(attempt.attempt_id, paypal_request_id=attempt.attempt_id)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("must not replay"))

    outcome, updated, detail = pes.recover_creating_attempt(cas.get(attempt.attempt_id))

    assert outcome == "manual_review_required"
    assert detail["reason"] == "TIMESTAMP_NOT_TZ_AWARE"
    assert no_money == []


# ===========================================================================
# CATALOG — 5. Replay returns an existing id -> persisted + independently
#               verified before finalization
# ===========================================================================


def test_catalog_replay_returns_existing_order_verified_before_finalize(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    order_state = {"persisted_when_read": None}

    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None},
    )

    def fake_get_order(oid):
        # By the time we re-read, the id is already persisted on the attempt.
        order_state["persisted_when_read"] = cas.get(attempt.attempt_id).paypal_order_id
        return catalog_fake_order(order_id=oid)

    monkeypatch.setattr(pes, "get_order", fake_get_order)

    outcome, updated, _ = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "recovered_finalized"
    # The returned id was persisted BEFORE the independent verification read.
    assert order_state["persisted_when_read"] == ORDER_ID
    assert no_money == []


# ===========================================================================
# CATALOG — 6. Conflicting id & amount/currency mismatch fail closed
# ===========================================================================


def test_catalog_conflicting_order_id_fails_closed(monkeypatch, no_money):
    # ORDER_ID already belongs to ANOTHER attempt.
    other = cas.reserve_attempt(PRODUCT_ID, PRICE_INR, AMOUNT, CURRENCY)
    claimed, _ = cas.claim_order_id(other.attempt_id, ORDER_ID)
    assert claimed

    attempt = reserve_catalog_no_id()
    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(pes, "get_order", lambda oid: pytest.fail("conflict must not re-read"))

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "review_required"
    assert detail["reason"] == "ORDER_ID_CONFLICT"
    # The attempt did NOT steal the id; it is still CREATING with no id.
    refreshed = cas.get(attempt.attempt_id)
    assert refreshed.status is CatalogAttemptStatus.CREATING
    assert refreshed.paypal_order_id is None
    assert no_money == []


@pytest.mark.parametrize(
    "order,reason",
    [
        (catalog_fake_order(value="0.01"), "AMOUNT_MISMATCH"),
        (catalog_fake_order(currency="EUR"), "CURRENCY_MISMATCH"),
        (catalog_fake_order(status="COMPLETED"), "ORDER_NOT_RECOVERABLE_STATUS"),
    ],
)
def test_catalog_replay_mismatch_fails_closed(monkeypatch, no_money, order, reason):
    attempt = reserve_catalog_no_id()
    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(pes, "get_order", lambda oid: order)

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "review_required"
    assert detail["reason"] == reason
    # The id WAS persisted (a real order exists), but nothing was finalized.
    refreshed = cas.get(attempt.attempt_id)
    assert refreshed.status is CatalogAttemptStatus.CREATING
    with pytest.raises(transaction_service.TransactionNotFoundError):
        transaction_service.get_transaction_by_order(ORDER_ID)
    assert no_money == []


# ===========================================================================
# CATALOG — 7. Transport failure leaves it unresolved, NOT FAILED
# ===========================================================================


def test_catalog_transport_failure_unresolved_not_failed(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()

    def boom(**k):
        raise paypal.PayPalOrderNetworkError("Could not reach PayPal")

    monkeypatch.setattr(pes, "create_order", boom)
    monkeypatch.setattr(pes, "get_order", lambda oid: pytest.fail("no read on transport fail"))

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "unresolved"
    assert detail["reason"] == "REPLAY_TRANSPORT_AMBIGUOUS"
    assert detail["retryable"] is True
    refreshed = cas.get(attempt.attempt_id)
    assert refreshed.status is CatalogAttemptStatus.CREATING  # NOT FAILED
    assert refreshed.paypal_order_id is None
    assert refreshed.recovery_claimed_at is None  # claim released for retry
    assert no_money == []


# ===========================================================================
# CATALOG — 8. Non-2xx / non-JSON 2xx / malformed / missing id -> no false
#               success (and the client no longer leaks a JSON decode error)
# ===========================================================================


def test_catalog_non_2xx_replay_unresolved(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()

    def boom(**k):
        raise paypal.PayPalOrderError("PayPal returned HTTP 500 for create-order")

    monkeypatch.setattr(pes, "create_order", boom)

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "unresolved"
    assert detail["reason"] == "REPLAY_UNRESOLVED"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert no_money == []


def test_catalog_replay_missing_order_id_unresolved(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    monkeypatch.setattr(
        pes, "create_order", lambda **k: {"order_id": None, "status": "CREATED"}
    )

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "unresolved"
    assert detail["reason"] == "REPLAY_NO_ORDER_ID"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert no_money == []


def test_create_order_2xx_non_json_raises_order_error_not_decode_error(monkeypatch):
    """The client hardening: a 2xx body that is not JSON surfaces as a
    PayPalOrderError, never a raw json-decoding exception."""
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret")

    class Resp:
        status_code = 200

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            class T:
                status_code = 200

                def json(self):
                    return {"access_token": "t", "token_type": "Bearer", "expires_in": 1}

            return T()
        return Resp()

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    with pytest.raises(paypal.PayPalOrderError) as exc:
        paypal.create_order(amount="16.66", currency="USD", request_id="cpa_x")
    assert "malformed" in str(exc.value).lower()


# ===========================================================================
# CATALOG — 9. A subsequent retry reuses the SAME request id, window-gated
# ===========================================================================


def test_catalog_retry_reuses_same_request_id_and_respects_window(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    calls = []

    def flaky(**k):
        calls.append(k["request_id"])
        raise paypal.PayPalOrderNetworkError("timeout")

    monkeypatch.setattr(pes, "create_order", flaky)

    # First retry (within window) -> unresolved, claim released.
    o1, _, _ = pes.recover_creating_attempt(attempt, now=dt(attempt))
    # Second retry (still within window) -> replays again with the SAME id.
    o2, _, _ = pes.recover_creating_attempt(cas.get(attempt.attempt_id), now=dt(attempt))

    assert o1 == o2 == "unresolved"
    assert calls == [attempt.paypal_request_id, attempt.paypal_request_id]

    # A later retry OUTSIDE the window is refused with no further call.
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("expired"))
    o3, _, d3 = pes.recover_creating_attempt(
        cas.get(attempt.attempt_id), now=dt(attempt) + timedelta(seconds=WINDOW + 1)
    )
    assert o3 == "manual_review_required" and d3["reason"] == "WINDOW_EXPIRED"
    assert no_money == []


# ===========================================================================
# CATALOG — 10. Concurrent recovery -> at most one in-flight provider call
# ===========================================================================


def test_catalog_concurrent_recovery_one_provider_call(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()

    call_count = {"n": 0}
    count_lock = threading.Lock()
    release = threading.Event()

    def blocking_create_order(**k):
        with count_lock:
            call_count["n"] += 1
        # Hold the claim "in flight" so concurrent claimers must back off.
        release.wait(timeout=5)
        return {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None}

    monkeypatch.setattr(pes, "create_order", blocking_create_order)
    monkeypatch.setattr(pes, "get_order", lambda oid: catalog_fake_order(order_id=oid))

    outcomes: list[str] = []
    out_lock = threading.Lock()
    start = threading.Barrier(6)

    def worker():
        start.wait(timeout=5)
        o, _, _ = pes.recover_creating_attempt(cas.get(attempt.attempt_id), now=dt(attempt))
        with out_lock:
            outcomes.append(o)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    # Give the losers time to observe the held claim, then let the winner finish.
    threading.Timer(0.5, release.set).start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert call_count["n"] == 1  # exactly one provider call
    assert outcomes.count("recovered_finalized") == 1
    assert all(
        o in ("recovered_finalized", "replay_in_progress", "already_finalized")
        for o in outcomes
    )
    assert len(transaction_service.list_transactions(limit=100)) == 1
    assert no_money == []


# ===========================================================================
# CATALOG — 11. A stale claim cannot permit a replay outside the window
# ===========================================================================


def test_catalog_stale_claim_still_window_gated(monkeypatch, no_money):
    # Expired window AND a claim already set: the window (from created_at) wins.
    old = datetime.now(timezone.utc) - timedelta(seconds=WINDOW + 3600)
    attempt = raw_insert_catalog(
        old, request_id="placeholder", recovery_claimed_at=old.isoformat()
    )
    tamper_catalog(attempt.attempt_id, paypal_request_id=attempt.attempt_id)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("expired must not replay"))

    outcome, updated, detail = pes.recover_creating_attempt(cas.get(attempt.attempt_id))

    assert outcome == "manual_review_required"
    assert detail["reason"] == "WINDOW_EXPIRED"
    assert no_money == []


def test_catalog_fresh_claim_blocks_second_worker(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    # Simulate a FRESH in-flight claim held by another worker.
    claimed, _, _token = cas.claim_recovery(attempt.attempt_id)
    assert claimed
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("claim held"))

    outcome, updated, detail = pes.recover_creating_attempt(
        cas.get(attempt.attempt_id), now=dt(attempt)
    )

    assert outcome == "replay_in_progress"
    assert detail["reason"] == "REPLAY_IN_PROGRESS"
    assert no_money == []


def test_catalog_stale_claim_reclaimable_within_window(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    # A claim older than the TTL, but the attempt is still within the window.
    stale = dt(attempt) - timedelta(seconds=cas.DEFAULT_RECOVERY_CLAIM_TTL_SECONDS + 60)
    tamper_catalog(attempt.attempt_id, recovery_claimed_at=stale.isoformat())
    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(pes, "get_order", lambda oid: catalog_fake_order(order_id=oid))

    outcome, updated, _ = pes.recover_creating_attempt(
        cas.get(attempt.attempt_id), now=dt(attempt)
    )

    assert outcome == "recovered_finalized"  # stale claim reclaimed
    assert no_money == []


# ===========================================================================
# CATALOG — 12. Product-unavailable & read-first cases still correct
# ===========================================================================


def test_catalog_product_unavailable_during_replay_is_review(monkeypatch, no_money):
    attempt = cas.reserve_attempt("GHOST-PRODUCT", PRICE_INR, AMOUNT, CURRENCY)
    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(pes, "get_order", lambda oid: catalog_fake_order(order_id=oid))

    outcome, updated, detail = pes.recover_creating_attempt(attempt, now=dt(attempt))

    assert outcome == "review_required"
    assert detail["reason"] == "PRODUCT_UNAVAILABLE"
    assert cas.get(attempt.attempt_id).status is CatalogAttemptStatus.CREATING
    assert transaction_service.list_transactions(limit=100) == []
    assert no_money == []


def test_catalog_recover_with_existing_order_id_uses_read_first_no_replay(monkeypatch, no_money):
    # A CREATING attempt that ALREADY has an order id -> read-first, no replay.
    attempt = cas.reserve_attempt(PRODUCT_ID, PRICE_INR, AMOUNT, CURRENCY)
    cas.claim_order_id(attempt.attempt_id, ORDER_ID)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("must not replay"))
    monkeypatch.setattr(pes, "get_order", lambda oid: catalog_fake_order(order_id=oid))

    outcome, updated, _ = pes.recover_creating_attempt(cas.get(attempt.attempt_id))

    assert outcome == "recovered_finalized"
    assert updated.status is CatalogAttemptStatus.FINALIZED
    assert no_money == []


# ===========================================================================
# CATALOG — 13. No automatic replay from webhook / read-first reconcile
# ===========================================================================


def test_webhook_fallback_never_replays_create_order(monkeypatch, no_money):
    reserve_catalog_no_id()  # a CREATING, no-id attempt exists
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("webhook must not replay"))
    monkeypatch.setattr(pes, "get_order", lambda oid: pytest.fail("webhook must not read it"))

    # A verified webhook for some order id resolves NO no-id attempt (there is
    # no stored order id to match), so nothing is replayed or read.
    event = {
        "event_type": "CHECKOUT.ORDER.APPROVED",
        "resource": {"id": "SOME-ORDER"},
    }
    result = reconcile_catalog_attempt_event(event)
    assert result["action"] == "catalog_attempt_not_found"
    assert no_money == []


def test_read_first_reconcile_never_replays_no_id_attempt(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("reconcile must not replay"))
    monkeypatch.setattr(pes, "get_order", lambda oid: pytest.fail("no id -> no read"))

    outcome, _, _ = pes.reconcile_creating_attempt(attempt)

    assert outcome == "manual_review_required"
    assert no_money == []


# ===========================================================================
# CATALOG — endpoint wiring + auth
# ===========================================================================


def test_catalog_recover_endpoint_requires_auth(no_auto_auth):
    attempt = reserve_catalog_no_id()
    resp = client.post(f"/purchase/attempts/{attempt.attempt_id}/recover-order")
    assert resp.status_code == 401


def test_catalog_recover_endpoint_unknown_is_404():
    assert client.post("/purchase/attempts/cpa_nope/recover-order").status_code == 404


def test_catalog_recover_endpoint_expired_is_manual_review(monkeypatch, no_money):
    # created_at older than the (default) window -> endpoint uses the real clock.
    old = datetime.now(timezone.utc) - timedelta(seconds=WINDOW + 3600)
    attempt = raw_insert_catalog(old, request_id="x")
    tamper_catalog(attempt.attempt_id, paypal_request_id=attempt.attempt_id)
    monkeypatch.setattr(pes, "create_order", lambda **k: pytest.fail("expired must not replay"))

    body = client.post(f"/purchase/attempts/{attempt.attempt_id}/recover-order").json()

    assert body["outcome"] == "manual_review_required"
    assert body["detail"]["reason"] == "WINDOW_EXPIRED"
    assert no_money == []


def test_catalog_recover_endpoint_replays_recent(monkeypatch, no_money):
    attempt = reserve_catalog_no_id()  # created ~now, within window
    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(pes, "get_order", lambda oid: catalog_fake_order(order_id=oid))

    body = client.post(f"/purchase/attempts/{attempt.attempt_id}/recover-order").json()

    assert body["outcome"] == "recovered_finalized"
    assert body["reconciled"] is True and body["status"] == "FINALIZED"
    assert body["transaction_id"]
    assert no_money == []


# ===========================================================================
# EXTERNAL — mirror the key cases
# ===========================================================================


def ext_fake_order(order_id, status="CREATED", value=None, currency="USD"):
    value = value if value is not None else f"{FRESH_OBSERVED:.2f}"  # "19.99"
    return {
        "order_id": order_id,
        "order_status": status,
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": currency, "value": value}}],
        "captures": [],
        "links": [],
        "approve_url": None,
    }


def ext_reserve_no_id(adopted, *, created_at=None):
    """A CREATING external reservation with NO order id (a crashed create).

    created_at defaults to the frozen NOW (service tests pass now=NOW); an
    endpoint test, which runs on the real wall clock, passes a real-now stamp
    so the reservation falls inside the default recovery window.
    """
    require_approval()
    snapshot(adopted.adopted_product_id, reval_id=f"reval_fresh_{uuid.uuid4().hex}")
    approval = approved_approval(adopted.adopted_product_id)
    stamp = created_at if created_at is not None else NOW.isoformat()
    reservation, existing = eps._reserve(approval, stamp)
    assert reservation is not None and existing is None
    assert reservation.status is ExternalOrderStatus.CREATING
    assert reservation.paypal_order_id is None
    assert reservation.paypal_request_id == reservation.execution_id
    return reservation


EXT_ORDER_ID = "EXTREPLAY-1"


def test_external_eligible_replays_with_exact_request_id(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    seen = {}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        seen["amount"] = amount
        seen["currency"] = currency
        seen["request_id"] = request_id
        return {"order_id": EXT_ORDER_ID, "status": "CREATED", "approve_url": None}

    monkeypatch.setattr(eps, "create_order", fake_create_order)
    monkeypatch.setattr(eps, "get_order", lambda oid: ext_fake_order(oid))

    outcome, updated, _ = eps.recover_creating_order(reservation, now=NOW)

    assert seen["request_id"] == reservation.paypal_request_id == reservation.execution_id
    # The amount sent is the bound snapshot's observed price, never a client value.
    assert seen["amount"] == f"{FRESH_OBSERVED:.2f}" and seen["currency"] == "USD"
    assert outcome == "recovered_order_created"
    assert updated.status is ExternalOrderStatus.ORDER_CREATED
    assert updated.paypal_order_id == EXT_ORDER_ID
    assert no_money == []


def test_external_expired_not_replayed(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    monkeypatch.setattr(eps, "create_order", lambda **k: pytest.fail("expired must not replay"))

    outcome, updated, detail = eps.recover_creating_order(
        reservation, now=NOW + timedelta(seconds=WINDOW + 60)
    )

    assert outcome == "manual_review_required"
    assert detail["reason"] == "WINDOW_EXPIRED"
    assert updated.status is ExternalOrderStatus.CREATING
    assert updated.paypal_order_id is None
    assert no_money == []


def test_external_missing_request_id_not_replayed(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    with sqlite3.connect(eps._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET paypal_request_id = NULL WHERE execution_id = ?",
            (reservation.execution_id,),
        )
        conn.commit()
    monkeypatch.setattr(eps, "create_order", lambda **k: pytest.fail("must not replay"))

    outcome, updated, detail = eps.recover_creating_order(
        eps.get(reservation.execution_id), now=NOW
    )

    assert outcome == "manual_review_required"
    assert detail["reason"] == "NO_PERSISTED_REQUEST_ID"
    assert no_money == []


def test_external_future_timestamp_rejected(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    monkeypatch.setattr(eps, "create_order", lambda **k: pytest.fail("must not replay"))

    outcome, updated, detail = eps.recover_creating_order(
        reservation, now=NOW - timedelta(hours=2)
    )

    assert outcome == "manual_review_required"
    assert detail["reason"] == "TIMESTAMP_IN_FUTURE"
    assert no_money == []


def test_external_transport_failure_unresolved_not_failed(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)

    def boom(**k):
        raise paypal.PayPalOrderNetworkError("timeout")

    monkeypatch.setattr(eps, "create_order", boom)

    outcome, updated, detail = eps.recover_creating_order(reservation, now=NOW)

    assert outcome == "unresolved"
    assert detail["reason"] == "REPLAY_TRANSPORT_AMBIGUOUS"
    refreshed = eps.get(reservation.execution_id)
    assert refreshed.status is ExternalOrderStatus.CREATING  # NOT FAILED
    assert refreshed.paypal_order_id is None
    assert refreshed.recovery_claimed_at is None
    assert no_money == []


def test_external_amount_mismatch_fails_closed(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    monkeypatch.setattr(
        eps, "create_order",
        lambda **k: {"order_id": EXT_ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    # Independent read shows a DIFFERENT amount than the approved one.
    monkeypatch.setattr(eps, "get_order", lambda oid: ext_fake_order(oid, value="0.01"))

    outcome, updated, detail = eps.recover_creating_order(reservation, now=NOW)

    assert outcome == "review_required"
    assert detail["reason"] == "AMOUNT_MISMATCH"
    assert eps.get(reservation.execution_id).status is ExternalOrderStatus.CREATING
    assert no_money == []


def raw_insert_external_owner(order_id):
    """A minimal ORDER_CREATED external row that already owns `order_id`."""
    with sqlite3.connect(eps._db_path()) as conn:
        conn.executescript(eps._SCHEMA)
        conn.execute(
            """INSERT INTO external_purchase_orders
               (execution_id, approval_id, adopted_product_id, revalidation_id,
                paypal_order_id, paypal_amount, paypal_currency, status,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                f"xord_{uuid.uuid4().hex}", f"xapr_{uuid.uuid4().hex}",
                "adopt_other", "reval_other", order_id, "19.99", "USD",
                ExternalOrderStatus.ORDER_CREATED.value,
                NOW.isoformat(), NOW.isoformat(),
            ),
        )
        conn.commit()


def test_external_conflicting_order_id_fails_closed(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    # The returned id already belongs to ANOTHER execution row.
    raw_insert_external_owner(EXT_ORDER_ID)

    monkeypatch.setattr(
        eps, "create_order",
        lambda **k: {"order_id": EXT_ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(eps, "get_order", lambda oid: pytest.fail("conflict must not read"))

    outcome, updated, detail = eps.recover_creating_order(reservation, now=NOW)

    assert outcome == "review_required"
    assert detail["reason"] == "ORDER_ID_CONFLICT"
    refreshed = eps.get(reservation.execution_id)
    assert refreshed.status is ExternalOrderStatus.CREATING and refreshed.paypal_order_id is None
    assert no_money == []


def test_external_concurrent_recovery_one_provider_call(monkeypatch, adopted, no_money):
    reservation = ext_reserve_no_id(adopted)
    call_count = {"n": 0}
    count_lock = threading.Lock()
    release = threading.Event()

    def blocking(**k):
        with count_lock:
            call_count["n"] += 1
        release.wait(timeout=5)
        return {"order_id": EXT_ORDER_ID, "status": "CREATED", "approve_url": None}

    monkeypatch.setattr(eps, "create_order", blocking)
    monkeypatch.setattr(eps, "get_order", lambda oid: ext_fake_order(oid))

    outcomes: list[str] = []
    out_lock = threading.Lock()
    start = threading.Barrier(6)

    def worker():
        start.wait(timeout=5)
        o, _, _ = eps.recover_creating_order(eps.get(reservation.execution_id), now=NOW)
        with out_lock:
            outcomes.append(o)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    threading.Timer(0.5, release.set).start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert call_count["n"] == 1
    assert outcomes.count("recovered_order_created") == 1
    assert all(
        o in ("recovered_order_created", "replay_in_progress", "already_applied")
        for o in outcomes
    )
    assert no_money == []


def test_external_recover_endpoint_replays_recent(monkeypatch, adopted, no_money):
    # The endpoint uses the real wall clock, so stamp the reservation now.
    reservation = ext_reserve_no_id(
        adopted, created_at=datetime.now(timezone.utc).isoformat()
    )
    monkeypatch.setattr(
        eps, "create_order",
        lambda **k: {"order_id": EXT_ORDER_ID, "status": "CREATED", "approve_url": None},
    )
    monkeypatch.setattr(eps, "get_order", lambda oid: ext_fake_order(oid))

    body = client.post(
        f"/discovery/approvals/{reservation.approval_id}/recover-order"
    ).json()

    assert body["outcome"] == "recovered_order_created"
    assert body["status"] == "ORDER_CREATED"
    assert no_money == []
