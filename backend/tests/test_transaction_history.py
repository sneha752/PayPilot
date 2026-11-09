"""Tests for the transaction-history API (Phase 8D — read-only, no PayPal).

Covers GET /transactions: newest-first deterministic ordering (including
the transaction_id tie-breaker for equal timestamps), status filtering,
limit validation (rejected, never clamped), empty results as success, the
response reusing the existing TransactionRecord fields, read-only
behavior, and that no PayPal/AI call is ever made. Runs entirely against
the isolated per-test SQLite database from conftest.py.
"""

import pytest
from starlette.testclient import TestClient

from app.main import app
from app.schemas.transaction import TransactionStatus
from app.services import transaction_service

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_network_or_ai(monkeypatch):
    """History must be served purely from SQLite: any PayPal/HTTP or AI
    call fails the test."""
    import httpx

    from app.policy import policy_engine
    from app.services.providers import gemini

    def _forbidden(*args, **kwargs):  # pragma: no cover - fails the test if hit
        raise AssertionError("Transaction history must not call PayPal or AI")

    monkeypatch.setattr(httpx, "post", _forbidden)
    monkeypatch.setattr(httpx, "get", _forbidden)
    monkeypatch.setattr(policy_engine, "evaluate_policy", _forbidden)
    monkeypatch.setattr(gemini, "GeminiProvider", _forbidden)


def make_transaction(order_id: str, status: TransactionStatus | None = None):
    txn = transaction_service.create_transaction(
        product_id="p1",
        product_name="Demo Product",
        catalog_price_inr=1000,
        paypal_amount="12.00",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )
    if status is not None and status != TransactionStatus.ORDER_CREATED:
        txn = transaction_service.update_status(
            txn.transaction_id, status, capture_id=f"CAP-{order_id}"
        )
    return txn


# --- 1. Empty history --------------------------------------------------------


def test_empty_history_is_a_successful_response():
    resp = client.get("/transactions")
    assert resp.status_code == 200
    assert resp.json() == {"transactions": [], "count": 0}


# --- 2. Newest-first ordering -----------------------------------------------


def test_multiple_transactions_newest_first(monkeypatch):
    # Distinct, strictly increasing timestamps — order of return must be
    # by created_at, not by insertion order.
    times = iter(
        [
            "2026-10-04T10:00:00+00:00",
            "2026-10-04T11:00:00+00:00",
            "2026-10-04T12:00:00+00:00",
        ]
    )
    monkeypatch.setattr(transaction_service, "_now", lambda: next(times))
    make_transaction("ORDER-OLD")
    make_transaction("ORDER-MID")
    make_transaction("ORDER-NEW")

    body = client.get("/transactions").json()

    assert body["count"] == 3
    assert [t["paypal_order_id"] for t in body["transactions"]] == [
        "ORDER-NEW",
        "ORDER-MID",
        "ORDER-OLD",
    ]


def test_equal_timestamps_break_ties_by_transaction_id(monkeypatch):
    monkeypatch.setattr(
        transaction_service, "_now", lambda: "2026-10-04T10:00:00+00:00"
    )
    a = make_transaction("ORDER-A")
    b = make_transaction("ORDER-B")

    body = client.get("/transactions").json()

    expected = sorted(
        [a.transaction_id, b.transaction_id], reverse=True
    )  # transaction_id DESC
    assert [t["transaction_id"] for t in body["transactions"]] == expected


# --- 3/4. Status filtering -----------------------------------------------------


def test_status_filter_returns_only_matching_transactions():
    make_transaction("ORDER-1")  # ORDER_CREATED
    make_transaction("ORDER-2", TransactionStatus.CAPTURED)
    make_transaction("ORDER-3", TransactionStatus.REFUNDED)
    make_transaction("ORDER-4", TransactionStatus.CAPTURED)

    body = client.get("/transactions?status=CAPTURED").json()

    assert body["count"] == 2
    assert {t["paypal_order_id"] for t in body["transactions"]} == {
        "ORDER-2",
        "ORDER-4",
    }
    assert all(t["status"] == "CAPTURED" for t in body["transactions"])


def test_status_filter_with_zero_matches_is_success():
    make_transaction("ORDER-1")  # ORDER_CREATED only

    resp = client.get("/transactions?status=REFUNDED")

    assert resp.status_code == 200
    assert resp.json() == {"transactions": [], "count": 0}


# --- 5-8. Limits -------------------------------------------------------------------


def test_default_limit_is_20():
    for i in range(25):
        make_transaction(f"ORDER-{i:03d}")
    body = client.get("/transactions").json()
    assert body["count"] == 20
    assert len(body["transactions"]) == 20


def test_custom_valid_limit():
    for i in range(5):
        make_transaction(f"ORDER-{i}")
    body = client.get("/transactions?limit=3").json()
    assert body["count"] == 3


def test_limit_of_one_returns_single_newest(monkeypatch):
    times = iter(["2026-10-04T10:00:00+00:00", "2026-10-04T11:00:00+00:00"])
    monkeypatch.setattr(transaction_service, "_now", lambda: next(times))
    make_transaction("ORDER-OLD")
    make_transaction("ORDER-NEW")

    body = client.get("/transactions?limit=1").json()

    assert body["count"] == 1
    assert body["transactions"][0]["paypal_order_id"] == "ORDER-NEW"


def test_limit_of_100_accepted():
    make_transaction("ORDER-1")
    resp = client.get("/transactions?limit=100")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


# --- 9-11. Validation (rejected, never clamped) ------------------------------------


@pytest.mark.parametrize("bad_limit", [0, -1, 101, 1000])
def test_out_of_range_limit_rejected(bad_limit):
    assert client.get(f"/transactions?limit={bad_limit}").status_code == 422


def test_invalid_status_rejected():
    assert client.get("/transactions?status=BANANA").status_code == 422
    assert client.get("/transactions?status=captured").status_code == 422  # exact enum


# --- 12. Fields match the existing ledger schema -----------------------------------


def test_returned_fields_match_existing_transaction_record():
    txn = make_transaction("ORDER-1", TransactionStatus.CAPTURED)

    body = client.get("/transactions").json()

    listed = body["transactions"][0]
    # Identical to the single-record endpoint's representation.
    single = client.get(f"/transactions/{txn.transaction_id}").json()
    assert listed == single
    assert listed["transaction_id"] == txn.transaction_id
    assert listed["paypal_order_id"] == "ORDER-1"
    assert listed["paypal_capture_id"] == "CAP-ORDER-1"
    assert listed["status"] == "CAPTURED"
    assert listed["catalog_price_inr"] == 1000


# --- 13. Read-only ------------------------------------------------------------------


def test_history_endpoint_is_read_only():
    txn = make_transaction("ORDER-1", TransactionStatus.CAPTURED)
    before = client.get(f"/transactions/{txn.transaction_id}").json()

    for _ in range(3):
        assert client.get("/transactions").status_code == 200
        assert client.get("/transactions?status=CAPTURED&limit=5").status_code == 200

    after = client.get(f"/transactions/{txn.transaction_id}").json()
    assert after == before  # nothing created, modified, or re-timestamped

    # Writing to the collection URL is not possible.
    assert client.post("/transactions").status_code == 405


# --- 14/15 (no PayPal / no Gemini) are enforced by the autouse fixture --------------


# --- 16. Existing lookups unchanged --------------------------------------------------


def test_existing_single_transaction_lookups_still_work():
    txn = make_transaction("ORDER-1", TransactionStatus.CAPTURED)

    by_id = client.get(f"/transactions/{txn.transaction_id}")
    by_order = client.get("/transactions/paypal/ORDER-1")

    assert by_id.status_code == 200
    assert by_order.status_code == 200
    assert by_id.json() == by_order.json()
    assert client.get("/transactions/txn_missing").status_code == 404
