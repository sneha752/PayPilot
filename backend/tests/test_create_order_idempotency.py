"""Create-order idempotency keys persisted and sent (Phase 11F-K.22a).

Each logical catalog and external create-order operation now carries a stable
PayPal-Request-Id, derived from its own durable reservation id (attempt_id /
execution_id), persisted BEFORE the PayPal call and sent on it. This phase adds
NO retries or recovery re-calls — only the persistence and the header.

PayPal is mocked throughout (no network call). The header value sent is
asserted against the persisted reservation value.
"""

from __future__ import annotations

import sqlite3

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.catalog_attempt import CatalogAttemptStatus
from app.schemas.intent import Authorization
from app.services import (
    catalog_attempt_service as cas,
    external_purchase_service as eps,
    purchase_execution_service as pes,
)
from app.services.currency_service import convert_inr_to_usd

# External 11E chain fixtures (adopted product -> snapshot -> approval).
from test_external_purchase_order import (  # noqa: F401 - pytest fixtures
    adopted,
    approved_approval,
    clock,
    no_ai,
    no_money_moves,
    order_calls,
    paypal_http,
    post_create_order,
    require_approval,
    snapshot,
)

client = TestClient(app, raise_server_exceptions=False)

PRODUCT_ID = "HDP006"
PRICE_INR = 1499
AMOUNT = convert_inr_to_usd(PRICE_INR)


# ===========================================================================
# paypal.validate_paypal_request_id
# ===========================================================================


def test_validator_accepts_reservation_ids():
    assert paypal.validate_paypal_request_id("cpa_" + "a" * 32) == "cpa_" + "a" * 32
    assert paypal.validate_paypal_request_id("xord_" + "b" * 32) == "xord_" + "b" * 32


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "x" * (paypal.PAYPAL_REQUEST_ID_MAX_LENGTH + 1), "has space", "ctrl\x01"],
)
def test_validator_rejects_invalid(bad):
    with pytest.raises(ValueError):
        paypal.validate_paypal_request_id(bad)


# ===========================================================================
# create_order header behavior
# ===========================================================================


def test_create_order_sends_request_id_header_when_provided(paypal_http):
    paypal.create_order(amount="16.66", currency="USD", request_id="cpa_req_123")
    call = order_calls(paypal_http)[-1]
    assert call["headers"]["PayPal-Request-Id"] == "cpa_req_123"


def test_create_order_omits_header_when_not_provided(paypal_http):
    paypal.create_order(amount="16.66", currency="USD")
    call = order_calls(paypal_http)[-1]
    assert "PayPal-Request-Id" not in call["headers"]


def test_raw_create_order_endpoint_sends_no_request_id(paypal_http):
    resp = client.post(
        "/paypal/create-order", json={"amount": "16.66", "currency": "USD"}
    )
    assert resp.status_code == 200
    call = order_calls(paypal_http)[-1]
    assert "PayPal-Request-Id" not in call["headers"]


# ===========================================================================
# Catalog: persisted before the call, and sent
# ===========================================================================


def test_catalog_reservation_persists_request_id_before_create_order(monkeypatch):
    seen = {}

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        # The attempt must already be committed with this exact request id.
        creating = cas.list_by_status(CatalogAttemptStatus.CREATING, limit=100)
        seen["persisted"] = [a.paypal_request_id for a in creating]
        seen["header_arg"] = request_id
        return {"order_id": "ORDER-1", "status": "CREATED", "approve_url": "x"}

    monkeypatch.setattr(pes, "create_order", fake_create_order)
    pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))

    # Exactly one CREATING attempt existed at call time, carrying its id, and
    # that same value was passed as the request id.
    assert len(seen["persisted"]) == 1
    rid = seen["persisted"][0]
    assert rid and seen["header_arg"] == rid
    # Finalized attempt retains the same request id (never overwritten).
    fin = cas.list_by_status(CatalogAttemptStatus.FINALIZED, limit=100)
    assert len(fin) == 1 and fin[0].paypal_request_id == rid
    # Derived from the attempt's own id.
    assert rid == fin[0].attempt_id


def test_catalog_request_id_reaches_the_paypal_client_header(monkeypatch, paypal_http):
    # Drive the real client (paypal_http mock) and read the header it received.
    pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))
    attempt = cas.list_by_status(CatalogAttemptStatus.FINALIZED, limit=100)[0]
    call = order_calls(paypal_http)[-1]
    assert call["headers"]["PayPal-Request-Id"] == attempt.paypal_request_id


def test_different_catalog_purchases_get_different_request_ids(monkeypatch):
    monkeypatch.setattr(
        pes, "create_order",
        lambda **k: {"order_id": f"O-{k['request_id']}", "status": "CREATED", "approve_url": "x"},
    )
    pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))
    pes.approve_purchase(PRODUCT_ID, Authorization(purchase_requested=True))
    rids = {a.paypal_request_id for a in cas.list_by_status(CatalogAttemptStatus.FINALIZED, limit=100)}
    assert len(rids) == 2  # one per logical purchase


# ===========================================================================
# External: persisted before the call, and sent
# ===========================================================================


def test_external_reservation_persists_request_id_and_sends_it(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    resp = post_create_order(approval.approval_id)
    assert resp.status_code == 200

    execution = eps.get_by_approval(approval.approval_id)
    assert execution.paypal_request_id == execution.execution_id  # derived from id
    call = order_calls(paypal_http)[-1]
    assert call["headers"]["PayPal-Request-Id"] == execution.paypal_request_id


def test_external_replay_preserves_original_request_id(adopted, paypal_http):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    post_create_order(approval.approval_id)
    first = eps.get_by_approval(approval.approval_id)
    # A replay creates no second order and must not change the stored id.
    post_create_order(approval.approval_id)
    second = eps.get_by_approval(approval.approval_id)
    assert second.paypal_request_id == first.paypal_request_id
    assert len(order_calls(paypal_http)) == 1  # still exactly one create call


def test_different_external_purchases_get_different_request_ids(adopted, paypal_http):
    ids = []
    for i in range(2):
        require_approval()
        snapshot(adopted.adopted_product_id, reval_id=f"reval_fresh_{i}")
        approval = approved_approval(adopted.adopted_product_id)
        # Distinct PayPal order id per purchase (PayPal returns a unique id per
        # call; the shared fake default would otherwise collide on the unique
        # order-id column, unrelated to the request-id under test).
        paypal_http.state.order_payload = {
            "id": f"EXTORDER-{i}",
            "status": "CREATED",
            "links": [{"rel": "approve", "href": f"https://sandbox.paypal.com/c?t={i}"}],
        }
        post_create_order(approval.approval_id)
        ids.append(eps.get_by_approval(approval.approval_id).paypal_request_id)
    assert ids[0] and ids[1] and ids[0] != ids[1]


# ===========================================================================
# Migration: existing DBs gain the column; new DBs have it; repeatable
# ===========================================================================


def test_catalog_migration_adds_column_to_legacy_db(tmp_path, monkeypatch):
    db = tmp_path / "legacy_catalog.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(db))
    # A pre-K.22a table with no paypal_request_id column.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE catalog_purchase_attempts (
                   attempt_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
                   catalog_price_inr INTEGER NOT NULL, paypal_amount TEXT NOT NULL,
                   paypal_currency TEXT NOT NULL, paypal_order_id TEXT UNIQUE,
                   claim_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                   transaction_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO catalog_purchase_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("cpa_legacy", PRODUCT_ID, PRICE_INR, AMOUNT, "USD", None,
             "claim_legacy", "CREATING", None, "2026-10-01T00:00:00+00:00",
             "2026-10-01T00:00:00+00:00"),
        )
        conn.commit()

    # Reading through the service migrates additively; the legacy row reads NULL.
    rec = cas.get("cpa_legacy")
    assert rec is not None and rec.paypal_request_id is None
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(catalog_purchase_attempts)")}
    assert "paypal_request_id" in cols


def test_catalog_migration_is_repeatable(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(tmp_path / "repeat_catalog.db"))
    for _ in range(3):
        assert cas.count() == 0  # each _connect runs _migrate harmlessly


def test_external_migration_adds_column_to_legacy_db(tmp_path, monkeypatch):
    db = tmp_path / "legacy_external.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(db))
    # A pre-K.22a external table (post-11F-D columns, no paypal_request_id).
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE external_purchase_orders (
                   execution_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE,
                   adopted_product_id TEXT NOT NULL, revalidation_id TEXT NOT NULL,
                   paypal_order_id TEXT UNIQUE, paypal_amount TEXT, paypal_currency TEXT,
                   paypal_approve_url TEXT, capture_authorized_at TEXT,
                   paypal_capture_id TEXT, paypal_capture_status TEXT,
                   captured_amount TEXT, captured_currency TEXT,
                   status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO external_purchase_orders
               (execution_id, approval_id, adopted_product_id, revalidation_id,
                paypal_order_id, paypal_amount, paypal_currency, status,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("xord_legacy", "xapr_legacy", "adopt_legacy", "reval_legacy",
             "LEGACY-ORD", "19.99", "USD", "ORDER_CREATED",
             "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"),
        )
        conn.commit()

    rec = eps.get_by_approval("xapr_legacy")
    assert rec is not None and rec.paypal_request_id is None
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(external_purchase_orders)")}
    assert "paypal_request_id" in cols
