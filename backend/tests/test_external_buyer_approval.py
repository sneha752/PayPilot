"""Server-side buyer-approval verification (Phase 11F-C).

WHAT THIS PHASE ADDS
    POST /discovery/approvals/{approval_id}/verify-approval asks PayPal one
    question -- did the buyer approve THIS order, at THIS amount? -- and, if
    PayPal's own answer says yes, moves the execution ORDER_CREATED ->
    BUYER_APPROVED with a compare-and-set.

THE PROPERTIES DEFENDED HERE

    1. THE SERVER ASKS ABOUT ITS OWN ORDER ID. The request carries only an
       approval id. Tests post forged bodies and the exact `token` and
       `PayerID` values a browser returns from PayPal, and assert the id
       actually sent to get_order is the STORED one, every time.

    2. PAYPAL'S ANSWER IS THE ONLY EVIDENCE. Status must be exactly
       APPROVED; the order id must match; there must be exactly one purchase
       unit; its currency and amount must equal what the server recorded.
       Each check has its own failing test, and each asserts that the
       execution did NOT become BUYER_APPROVED.

    3. IT FAILS CLOSED. Auth failures, missing orders, 5xx, timeouts,
       network errors, malformed payloads, NaN amounts, two purchase units
       -- all refusals. Unknown is never approved.

    4. NO MONEY MOVES, AND NONE BECOMES MOVABLE. capture_order and
       refund_capture are booby-trapped for every test in this module, the
       catalog ledger is asserted empty, and a BUYER_APPROVED order is
       proved still uncapturable through BOTH capture routes and
       unrefundable through the refund route -- the 11F-B0 ownership gate is
       unaffected by this new state.

    5. CONCURRENCY CANNOT DOUBLE-TRANSITION OR REGRESS. Threads race the
       endpoint and exactly one reports `transitioned: true`.

PayPal HTTP is fully mocked throughout; nothing here contacts PayPal, and no
order is created, approved, captured or refunded for real.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.external_purchase import (
    ExternalApprovalVerificationFailure as Failure,
    ExternalOrderStatus,
    ExternalPurchaseOrder,
)
from app.services import (
    external_purchase_service,
    order_ownership_service,
    purchase_execution_service,
    refund_service,
    transaction_service,
)

# The 11E chain, reused rather than duplicated (see 11F-B's recovery tests).
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

#: The genuine paypal.get_order, captured at import time — BEFORE any
#: fixture replaces it. The 11E `no_money_moves` fixture booby-traps
#: paypal.get_order, so reading the attribute inside a test would capture
#: the trap rather than the real client.
REAL_GET_ORDER = external_purchase_service.get_order

ORDER_ID = "EXTORDER123"
AMOUNT = "19.99"
CURRENCY = "USD"

#: What PayPal returns for a genuinely buyer-approved order, in the shape
#: paypal.get_order produces.
APPROVED_ORDER = {
    "order_id": ORDER_ID,
    "order_status": "APPROVED",
    "intent": "CAPTURE",
    "purchase_units": [
        {"amount": {"currency_code": CURRENCY, "value": AMOUNT}}
    ],
    "captures": [],
    "links": [],
    "approve_url": None,
}


def approved_order(**overrides) -> dict:
    order = json.loads(json.dumps(APPROVED_ORDER))
    order.update(overrides)
    return order


def verify(approval_id: str, **kwargs):
    return client.post(
        f"/discovery/approvals/{approval_id}/verify-approval", **kwargs
    )


def detail(resp) -> dict:
    body = resp.json()["detail"]
    assert isinstance(body, dict), f"refusal must be structured, got {body!r}"
    return body


def stored(approval_id: str) -> ExternalPurchaseOrder:
    record = external_purchase_service.get_by_approval(approval_id)
    assert record is not None
    return record


def execution_rows() -> list[ExternalPurchaseOrder]:
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(external_purchase_service._SCHEMA)
        external_purchase_service._migrate(conn)
        return [
            ExternalPurchaseOrder(**dict(row))
            for row in conn.execute("SELECT * FROM external_purchase_orders")
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_capture_or_refund_anywhere(monkeypatch):
    """Capture and refund are booby-trapped for EVERY test in this module.

    Verification is allowed to read PayPal and write one status column. It
    is not allowed to capture, refund, or touch the catalog ledger, in any
    branch — including the success branch.
    """
    hits: list[str] = []

    def bomb(name):
        def _bomb(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"verification must never call {name}")

        return _bomb

    monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
    monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
    monkeypatch.setattr(
        purchase_execution_service, "capture_order", bomb("capture_order")
    )
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the catalog ledger")
    )
    monkeypatch.setattr(
        transaction_service, "mark_captured", bomb("the catalog ledger")
    )
    return hits


@pytest.fixture
def verify_transport(monkeypatch):
    """Control what get_order returns to the verification path.

    Patches the binding the SERVICE imported, records the order id actually
    requested, and arms a create_order trap so no test can quietly create a
    second order. Armed explicitly, after the order under test exists.
    """
    state = SimpleNamespace(order=None, raises=None, calls=[])

    def fake_get_order(order_id):
        state.calls.append(order_id)
        if state.raises is not None:
            raise state.raises
        return state.order

    def no_create(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("verification must never call create_order")

    monkeypatch.setattr(external_purchase_service, "get_order", fake_get_order)
    state.arm = lambda: monkeypatch.setattr(
        external_purchase_service, "create_order", no_create
    )
    return state


@pytest.fixture
def created_order(adopted, paypal_http, verify_transport):
    """One ORDER_CREATED execution, via the real 11E flow. Returns its id."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    response = post_create_order(approval.approval_id)
    assert response.status_code == 200, response.text
    assert stored(approval.approval_id).status is ExternalOrderStatus.ORDER_CREATED
    verify_transport.arm()
    return approval.approval_id


@pytest.fixture
def buyer_approved(created_order, verify_transport):
    """One execution already verified as BUYER_APPROVED."""
    verify_transport.order = approved_order()
    assert verify(created_order).status_code == 200
    verify_transport.calls.clear()
    return created_order


# ===========================================================================
# 1. Happy path
# ===========================================================================


def test_a_genuinely_approved_order_verifies(created_order, verify_transport):
    verify_transport.order = approved_order()

    resp = verify(created_order)

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["transitioned"] is True
    assert body["verification_source"] == "paypal"
    assert body["observed_order_status"] == "APPROVED"
    assert body["observed_amount"] == AMOUNT
    assert body["observed_currency"] == CURRENCY
    assert body["captured"] is False
    assert body["execution"]["status"] == "BUYER_APPROVED"
    # The id asked about is the STORED one.
    assert verify_transport.calls == [ORDER_ID]


def test_the_transition_is_persisted(created_order, verify_transport):
    verify_transport.order = approved_order()
    before = stored(created_order)

    verify(created_order)

    after = stored(created_order)
    assert after.status is ExternalOrderStatus.BUYER_APPROVED
    # Only the status and updated_at moved. Money fields are untouched.
    assert after.paypal_order_id == before.paypal_order_id == ORDER_ID
    assert after.paypal_amount == before.paypal_amount == AMOUNT
    assert after.paypal_currency == before.paypal_currency == CURRENCY
    assert after.paypal_approve_url == before.paypal_approve_url
    assert after.execution_id == before.execution_id
    assert after.created_at == before.created_at


@pytest.mark.parametrize("equivalent", ["19.99", "19.990", "0019.99"])
def test_an_equal_amount_in_another_spelling_still_matches(
    created_order, verify_transport, equivalent
):
    """The comparison is of money, not of text — 19.990 is 19.99."""
    verify_transport.order = approved_order(
        purchase_units=[
            {"amount": {"currency_code": CURRENCY, "value": equivalent}}
        ]
    )

    resp = verify(created_order)

    assert resp.status_code == 200
    assert resp.json()["observed_amount"] == equivalent
    assert stored(created_order).status is ExternalOrderStatus.BUYER_APPROVED


@pytest.mark.parametrize("spelling", ["approved", "Approved", "APPROVED"])
def test_the_status_comparison_is_case_insensitive(
    created_order, verify_transport, spelling
):
    verify_transport.order = approved_order(order_status=spelling)
    assert verify(created_order).status_code == 200


# ===========================================================================
# 2. PayPal says something other than APPROVED
# ===========================================================================


NOT_APPROVED_STATUSES = [
    "CREATED",
    "SAVED",
    "VOIDED",
    "COMPLETED",
    "PAYER_ACTION_REQUIRED",
    "APPROVED_PENDING",
    "",
    "   ",
]


@pytest.mark.parametrize("status", NOT_APPROVED_STATUSES)
def test_a_non_approved_status_is_refused(created_order, verify_transport, status):
    verify_transport.order = approved_order(order_status=status)

    resp = verify(created_order)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.ORDER_NOT_APPROVED.value
    assert body["verified"] is False
    # Truthful: the observed status is reported, not reinterpreted.
    assert body["observed"]["observed_order_status"] == (status.strip() or None)
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_a_completed_order_is_not_treated_as_approval(
    created_order, verify_transport
):
    """COMPLETED means captured — which this system never does externally.

    Reporting it as approval would launder an unexplained state into one the
    rest of the system acts on.
    """
    verify_transport.order = approved_order(order_status="COMPLETED")

    resp = verify(created_order)

    assert resp.status_code == 409
    assert detail(resp)["observed"]["observed_order_status"] == "COMPLETED"
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


@pytest.mark.parametrize("status", [None, 42, [], {}, True])
def test_a_non_string_status_is_refused(created_order, verify_transport, status):
    verify_transport.order = approved_order(order_status=status)

    resp = verify(created_order)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.ORDER_NOT_APPROVED.value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


# ===========================================================================
# 3. Order id mismatch
# ===========================================================================


@pytest.mark.parametrize(
    "returned",
    ["OTHERORDER999", "extorder123", " EXTORDER124", "", None, 12345, ORDER_ID + "X"],
)
def test_an_order_id_mismatch_is_refused(created_order, verify_transport, returned):
    verify_transport.order = approved_order(order_id=returned)

    resp = verify(created_order)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.ORDER_ID_MISMATCH.value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_paypals_echo_never_overrides_the_stored_order_id(
    created_order, verify_transport
):
    """A response claiming another order cannot redirect the transition."""
    verify_transport.order = approved_order(order_id="ATTACKER-ORDER")

    verify(created_order)

    record = stored(created_order)
    assert record.paypal_order_id == ORDER_ID
    assert record.status is ExternalOrderStatus.ORDER_CREATED


# ===========================================================================
# 4. Amount and currency mismatches
# ===========================================================================


@pytest.mark.parametrize("value", ["0.01", "19.98", "19.9", "199.90", "1999.00", "0"])
def test_an_amount_mismatch_is_refused(created_order, verify_transport, value):
    verify_transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": CURRENCY, "value": value}}]
    )

    resp = verify(created_order)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.AMOUNT_MISMATCH.value
    assert body["observed"]["observed_amount"] == value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


@pytest.mark.parametrize("currency", ["EUR", "GBP", "INR", "usd ", "US"])
def test_a_currency_mismatch_is_refused(created_order, verify_transport, currency):
    verify_transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": currency, "value": AMOUNT}}]
    )

    resp = verify(created_order)

    if currency.strip().upper() == CURRENCY:
        # "usd " is the same currency, differently spelled — it must pass.
        assert resp.status_code == 200
        return
    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CURRENCY_MISMATCH.value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "abc", "1,999.00", "$19.99", ""])
def test_a_malformed_amount_is_refused_without_raising(
    created_order, verify_transport, value
):
    verify_transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": CURRENCY, "value": value}}]
    )

    resp = verify(created_order)

    assert resp.status_code == 409
    assert detail(resp)["code"] in (
        Failure.AMOUNT_MALFORMED.value,
        Failure.PURCHASE_UNIT_MISSING.value,
    )
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_a_negative_amount_is_refused(created_order, verify_transport):
    verify_transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": CURRENCY, "value": "-19.99"}}]
    )

    resp = verify(created_order)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.AMOUNT_MISMATCH.value


# ===========================================================================
# 5. Missing, malformed and ambiguous purchase units
# ===========================================================================


MISSING_UNITS = [
    None,
    [],
    "not-a-list",
    {},
    [None],
    ["a string"],
    [{}],
    [{"amount": None}],
    [{"amount": "19.99"}],
    [{"amount": {}}],
    [{"amount": {"value": AMOUNT}}],                      # no currency_code
    [{"amount": {"currency_code": CURRENCY}}],            # no value
    [{"amount": {"currency_code": CURRENCY, "value": None}}],
    [{"amount": {"currency_code": CURRENCY, "value": 19.99}}],  # not a string
    [{"amount": {"currency_code": None, "value": AMOUNT}}],
    [{"amount": {"currency_code": "   ", "value": AMOUNT}}],
]


@pytest.mark.parametrize("units", MISSING_UNITS)
def test_missing_or_malformed_purchase_units_are_refused(
    created_order, verify_transport, units
):
    verify_transport.order = approved_order(purchase_units=units)

    resp = verify(created_order)  # must not raise

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.PURCHASE_UNIT_MISSING.value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


@pytest.mark.parametrize("count", [2, 3, 10])
def test_multiple_purchase_units_are_ambiguous_and_refused(
    created_order, verify_transport, count
):
    """Even when one of them matches — picking is how a wrong sum passes."""
    units = [{"amount": {"currency_code": CURRENCY, "value": AMOUNT}}] * count
    verify_transport.order = approved_order(purchase_units=units)

    resp = verify(created_order)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.PURCHASE_UNIT_AMBIGUOUS.value
    assert body["observed"]["purchase_unit_count"] == count
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_a_second_unit_with_a_different_amount_is_also_refused(
    created_order, verify_transport
):
    verify_transport.order = approved_order(
        purchase_units=[
            {"amount": {"currency_code": CURRENCY, "value": AMOUNT}},
            {"amount": {"currency_code": CURRENCY, "value": "999.00"}},
        ]
    )

    assert verify(created_order).status_code == 409
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_an_entirely_empty_paypal_response_is_refused(
    created_order, verify_transport
):
    verify_transport.order = {}
    resp = verify(created_order)
    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.ORDER_ID_MISMATCH.value


# ===========================================================================
# 6. PayPal errors: 401 / 404 / 5xx / timeout / network
# ===========================================================================


UPSTREAM_FAILURES = [
    paypal.PayPalAuthError("invalid_client"),
    paypal.PayPalOrderNotFoundError("no such order"),
    paypal.PayPalGetOrderError("HTTP 500"),
    paypal.PayPalGetOrderError("Could not reach PayPal to retrieve the order"),
]


@pytest.mark.parametrize("failure", UPSTREAM_FAILURES)
def test_an_unreadable_order_is_502_and_not_approved(
    created_order, verify_transport, failure
):
    verify_transport.raises = failure

    resp = verify(created_order)

    assert resp.status_code == 502
    body = detail(resp)
    assert body["code"] == Failure.ORDER_UNREADABLE.value
    assert body["verified"] is False
    assert body["captured"] is False
    # Unknown is not approved: the record keeps its status.
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_an_upstream_failure_is_retryable(created_order, verify_transport):
    verify_transport.raises = paypal.PayPalGetOrderError("transient")
    assert verify(created_order).status_code == 502

    verify_transport.raises = None
    verify_transport.order = approved_order()
    retried = verify(created_order)

    assert retried.status_code == 200
    assert retried.json()["transitioned"] is True


def test_a_network_timeout_surfaces_through_the_real_client(
    created_order, monkeypatch
):
    """End to end through paypal.get_order, with httpx exploding."""
    monkeypatch.setattr(
        paypal.httpx,
        "post",
        lambda url, **kw: SimpleNamespace(
            status_code=200, json=lambda: {"access_token": "fake-token"}
        ),
    )

    def timeout(*args, **kwargs):
        raise paypal.httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(paypal.httpx, "get", timeout)
    monkeypatch.setattr(external_purchase_service, "get_order", REAL_GET_ORDER)

    resp = verify(created_order)

    assert resp.status_code == 502
    assert detail(resp)["code"] == Failure.ORDER_UNREADABLE.value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_no_credentials_leak_in_any_refusal(created_order, verify_transport):
    verify_transport.raises = paypal.PayPalAuthError("invalid_client")
    bodies = [verify(created_order).text]
    verify_transport.raises = None
    verify_transport.order = approved_order(order_status="VOIDED")
    bodies.append(verify(created_order).text)

    for text in bodies:
        lowered = text.lower()
        for secret in ("client_secret", "access_token", "fake-token", "bearer"):
            assert secret not in lowered


# ===========================================================================
# 7. Unknown approval, missing execution, CREATING and FAILED
# ===========================================================================


def test_an_unknown_approval_is_404(verify_transport):
    resp = verify("xapr_does_not_exist")
    assert resp.status_code == 404
    assert verify_transport.calls == []


def test_an_approval_with_no_execution_is_404(adopted, paypal_http, verify_transport):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    resp = verify(approval.approval_id)

    assert resp.status_code == 404
    assert execution_rows() == []          # asking created nothing
    assert verify_transport.calls == []    # PayPal never consulted


@pytest.mark.parametrize("blank", ["%20", "%09"])
def test_a_blank_approval_id_is_422(blank, verify_transport):
    resp = client.post(f"/discovery/approvals/{blank}/verify-approval")
    assert resp.status_code == 422
    assert verify_transport.calls == []


def test_a_creating_reservation_is_not_verifiable(verify_transport):
    external_purchase_service._reserve(
        SimpleNamespace(
            approval_id="xapr_creating",
            adopted_product_id="adopt_x",
            revalidation_id="reval_x",
        ),
        "2026-10-09T12:00:00+00:00",
    )

    resp = verify("xapr_creating")

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.EXECUTION_NOT_VERIFIABLE.value
    assert body["observed"]["execution_status"] == "CREATING"
    assert verify_transport.calls == []  # no order id to even ask about
    assert stored("xapr_creating").status is ExternalOrderStatus.CREATING


def test_a_failed_execution_is_not_verifiable(adopted, paypal_http, verify_transport):
    paypal_http.state.order_status = 500
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    assert post_create_order(approval.approval_id).status_code == 502
    assert stored(approval.approval_id).status is ExternalOrderStatus.FAILED

    resp = verify(approval.approval_id)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.EXECUTION_NOT_VERIFIABLE.value
    assert verify_transport.calls == []
    assert stored(approval.approval_id).status is ExternalOrderStatus.FAILED


def test_an_incomplete_record_cannot_be_verified(created_order, verify_transport):
    """Nothing authoritative to check PayPal's answer against."""
    execution = stored(created_order)
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET paypal_amount = NULL "
            "WHERE execution_id = ?",
            (execution.execution_id,),
        )
        conn.commit()

    resp = verify(created_order)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.RECORDED_ORDER_INCOMPLETE.value
    assert verify_transport.calls == []
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


# ===========================================================================
# 8. Idempotent replay
# ===========================================================================


def test_a_replay_does_not_transition_again_or_call_paypal(
    buyer_approved, verify_transport
):
    before = stored(buyer_approved).model_dump()

    resp = verify(buyer_approved)

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["transitioned"] is False
    assert body["verification_source"] == "stored"
    # Truthful: nothing was observed on this request, so nothing is claimed.
    assert body["observed_order_status"] is None
    assert body["observed_amount"] is None
    assert body["observed_currency"] is None
    assert verify_transport.calls == []
    assert stored(buyer_approved).model_dump() == before  # updated_at too


def test_many_replays_are_stable(buyer_approved, verify_transport):
    before = stored(buyer_approved).model_dump()

    for _ in range(5):
        body = verify(buyer_approved).json()
        assert body["transitioned"] is False
        assert body["execution"]["status"] == "BUYER_APPROVED"

    assert verify_transport.calls == []
    assert stored(buyer_approved).model_dump() == before


def test_a_replay_cannot_be_regressed_by_a_hostile_paypal_answer(
    buyer_approved, verify_transport
):
    """Even if PayPal would now say VOIDED, a replay reads from storage."""
    verify_transport.order = approved_order(order_status="VOIDED")

    resp = verify(buyer_approved)

    assert resp.status_code == 200
    assert resp.json()["verification_source"] == "stored"
    assert verify_transport.calls == []
    assert stored(buyer_approved).status is ExternalOrderStatus.BUYER_APPROVED


def test_create_order_replay_on_an_approved_execution_is_truthful(
    buyer_approved, paypal_http
):
    """A BUYER_APPROVED order still EXISTS — replay must not call it failed."""
    resp = post_create_order(buyer_approved)

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is False
    assert body["execution"]["status"] == "BUYER_APPROVED"
    assert body["execution"]["paypal_order_id"] == ORDER_ID
    assert len(order_calls(paypal_http)) == 1  # no second order


def test_the_read_endpoint_shows_the_approved_state(buyer_approved):
    resp = client.get(f"/discovery/approvals/{buyer_approved}/order")

    assert resp.status_code == 200
    body = resp.json()
    assert body["execution"]["status"] == "BUYER_APPROVED"
    assert body["captured"] is False


# ===========================================================================
# 9. Concurrency
# ===========================================================================


def test_concurrent_verification_transitions_exactly_once(
    created_order, verify_transport
):
    verify_transport.order = approved_order()
    results: list[dict] = []
    barrier = threading.Barrier(6)

    def run():
        barrier.wait()
        results.append(verify(created_order).json())

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 6
    assert all(r["verified"] is True for r in results)
    assert all(r["execution"]["status"] == "BUYER_APPROVED" for r in results)
    # EXACTLY ONE compare-and-set winner.
    assert sum(1 for r in results if r["transitioned"]) == 1
    assert stored(created_order).status is ExternalOrderStatus.BUYER_APPROVED
    assert len(execution_rows()) == 1


def test_a_concurrent_failed_verification_cannot_regress_an_approved_one(
    created_order, verify_transport
):
    verify_transport.order = approved_order()
    assert verify(created_order).json()["transitioned"] is True

    # A later request whose PayPal answer is hostile must change nothing:
    # BUYER_APPROVED replays from storage without consulting PayPal at all.
    verify_transport.order = approved_order(order_status="VOIDED")
    assert verify(created_order).status_code == 200
    assert stored(created_order).status is ExternalOrderStatus.BUYER_APPROVED


def test_the_compare_and_set_cannot_resurrect_a_failed_record():
    """Direct test of the guard: only ORDER_CREATED matches."""
    now = "2026-10-09T12:00:00+00:00"
    reservation, _ = external_purchase_service._reserve(
        SimpleNamespace(
            approval_id="xapr_cas",
            adopted_product_id="adopt_cas",
            revalidation_id="reval_cas",
        ),
        now,
    )
    external_purchase_service._settle(
        reservation.execution_id, ExternalOrderStatus.FAILED, now
    )

    transitioned, record = external_purchase_service._mark_buyer_approved(
        reservation.execution_id, now
    )

    assert transitioned is False
    assert record.status is ExternalOrderStatus.FAILED


def test_the_compare_and_set_is_a_no_op_on_an_already_approved_row(
    created_order, verify_transport
):
    verify_transport.order = approved_order()
    verify(created_order)
    execution = stored(created_order)

    transitioned, record = external_purchase_service._mark_buyer_approved(
        execution.execution_id, "2026-10-09T13:00:00+00:00"
    )

    assert transitioned is False  # no second transition
    assert record.status is ExternalOrderStatus.BUYER_APPROVED


# ===========================================================================
# 10. Forged bodies, and PayPal's own token / PayerID are not evidence
# ===========================================================================


#: Everything a client might send hoping the server reads it instead of its
#: own stored state — including the exact parameters PayPal puts on the
#: browser return URL.
FORGED_BODY = {
    "token": "ATTACKERTOKEN",
    "PayerID": "ATTACKERPAYER",
    "payer_id": "ATTACKERPAYER",
    "paypal_order_id": "ATTACKER-ORDER",
    "order_id": "ATTACKER-ORDER",
    "status": "APPROVED",
    "order_status": "APPROVED",
    "execution_status": "BUYER_APPROVED",
    "verified": True,
    "transitioned": True,
    "captured": True,
    "paypal_amount": "0.01",
    "amount": "0.01",
    "paypal_currency": "EUR",
    "currency": "EUR",
    "product_id": "LAP001",
    "adopted_product_id": "adopt_forged",
    "revalidation_id": "reval_forged",
    "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
    "capture": True,
    "capture_id": "FORGEDCAPTURE",
    "merchant_name": "attacker-shop",
    "return_url": "https://attacker.example/return",
}


def test_a_forged_body_cannot_fake_approval(created_order, verify_transport):
    verify_transport.order = approved_order(order_status="CREATED")

    resp = verify(created_order, json=FORGED_BODY)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.ORDER_NOT_APPROVED.value
    # The id asked about is still the stored one.
    assert verify_transport.calls == [ORDER_ID]
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_a_forged_body_cannot_redirect_which_order_is_checked(
    created_order, verify_transport
):
    verify_transport.order = approved_order()

    resp = verify(created_order, json=FORGED_BODY)

    assert resp.status_code == 200
    assert verify_transport.calls == [ORDER_ID]
    record = stored(created_order)
    assert record.paypal_order_id == ORDER_ID
    assert record.paypal_amount == AMOUNT     # not 0.01
    assert record.paypal_currency == CURRENCY  # not EUR


@pytest.mark.parametrize("field", sorted(FORGED_BODY))
def test_no_single_forged_field_changes_the_outcome(
    created_order, verify_transport, field
):
    verify_transport.order = approved_order(order_status="VOIDED")

    resp = verify(created_order, json={field: FORGED_BODY[field]})

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.ORDER_NOT_APPROVED.value
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_paypal_return_url_parameters_are_not_proof_of_approval(
    created_order, verify_transport
):
    """A browser returning from PayPal carries ?token=&PayerID=. Neither is
    evidence, and neither selects the order that gets checked."""
    verify_transport.order = approved_order(order_status="CREATED")

    resp = client.post(
        f"/discovery/approvals/{created_order}/verify-approval"
        f"?token=ATTACKERTOKEN&PayerID=ATTACKERPAYER",
        json={"token": "ATTACKERTOKEN", "PayerID": "ATTACKERPAYER"},
    )

    assert resp.status_code == 409
    assert verify_transport.calls == [ORDER_ID]  # never ATTACKERTOKEN
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


def test_a_token_matching_a_real_other_order_is_still_ignored(
    created_order, verify_transport
):
    verify_transport.order = approved_order()

    client.post(
        f"/discovery/approvals/{created_order}/verify-approval"
        f"?token={ORDER_ID}&PayerID=SOMEPAYER"
    )

    assert verify_transport.calls == [ORDER_ID]


@pytest.mark.parametrize("body", [[], "a string", 42, None, {"nested": {"ok": True}}])
def test_non_object_bodies_change_nothing(created_order, verify_transport, body):
    verify_transport.order = approved_order(order_status="CREATED")
    resp = verify(created_order, json=body)
    assert resp.status_code == 409
    assert stored(created_order).status is ExternalOrderStatus.ORDER_CREATED


@pytest.mark.parametrize(
    "bad_id",
    [
        "' OR '1'='1",
        "'; DROP TABLE external_purchase_orders;--",
        "' UNION SELECT * FROM transactions--",
        "A" * 4096,
        "{{7*7}}",
        "xapr_%00",
    ],
)
def test_malicious_approval_ids_verify_nothing(
    bad_id, created_order, verify_transport
):
    before = stored(created_order).model_dump()

    resp = client.post(f"/discovery/approvals/{bad_id}/verify-approval")

    assert resp.status_code in (404, 409, 422), resp.text
    assert stored(created_order).model_dump() == before
    assert verify_transport.calls == []
    assert len(execution_rows()) == 1  # table intact


# ===========================================================================
# 11. Approval is not payment: nothing becomes capturable or refundable
# ===========================================================================


def test_verification_writes_no_catalog_ledger_row(buyer_approved):
    assert transaction_service.list_transactions() == []


@pytest.mark.parametrize("route", ["/purchase/capture", "/paypal/capture-order"])
def test_a_buyer_approved_order_is_still_uncapturable(buyer_approved, route):
    """The 11F-B0 ownership gate is unaffected by the new state."""
    resp = client.post(f"{route}/{ORDER_ID}")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE"
    assert stored(buyer_approved).status is ExternalOrderStatus.BUYER_APPROVED
    assert transaction_service.list_transactions() == []


def test_a_buyer_approved_order_is_still_unrefundable(buyer_approved):
    for identifier in (ORDER_ID, "EXTCAPTURE123"):
        resp = client.post(f"/paypal/refund/{identifier}")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "UNKNOWN_CAPTURE"


def test_the_ownership_gate_still_classifies_it_as_external(buyer_approved):
    owner, record = order_ownership_service.resolve_order_owner(ORDER_ID)
    assert owner is order_ownership_service.OrderOwner.EXTERNAL
    assert record.status is ExternalOrderStatus.BUYER_APPROVED


def test_no_capture_or_refund_call_happened_anywhere(
    buyer_approved, no_capture_or_refund_anywhere
):
    assert no_capture_or_refund_anywhere == []


def test_verifying_approval_captures_nothing(buyer_approved):
    """Phase 11F-D made external capture possible — through its OWN path.

    Verifying buyer approval must not be that path: a BUYER_APPROVED
    execution has no capture id, no capture status and, critically, no
    capture authorization. Money still needs two further deliberate acts.
    """
    record = stored(buyer_approved)
    assert record.status is ExternalOrderStatus.BUYER_APPROVED
    assert record.paypal_capture_id is None
    assert record.paypal_capture_status is None
    assert record.captured_amount is None
    assert record.captured_currency is None
    assert record.capture_authorized_at is None
    # A refund has never existed and must not.
    for forbidden in ("refund_id", "paypal_refund_id", "transaction_id"):
        assert forbidden not in ExternalPurchaseOrder.model_fields
        assert not hasattr(record, forbidden)
    assert "REFUNDED" not in {s.value for s in ExternalOrderStatus}


def test_the_service_still_imports_no_refund_client():
    """capture_order arrived in 11F-D. A refund client must never arrive."""
    for forbidden in ("refund_capture", "refund_transaction", "capture_purchase"):
        assert not hasattr(external_purchase_service, forbidden), forbidden


def test_the_verification_response_reports_no_capture(
    buyer_approved, verify_transport
):
    """The capture KEYS exist on the record since 11F-D; their VALUES must
    all be null here, and `captured` false. No refund field at any depth."""
    body = verify(buyer_approved).json()

    assert body["captured"] is False
    for key in (
        "paypal_capture_id",
        "paypal_capture_status",
        "captured_amount",
        "captured_currency",
        "capture_authorized_at",
    ):
        assert body["execution"][key] is None, key
    for forbidden in ("refund_id", "refunded", "transaction_id"):
        assert forbidden not in json.dumps(body).lower()


def test_money_provenance_is_unchanged(created_order, verify_transport, paypal_http):
    """Verification reads the recorded amount; it never re-derives one."""
    verify_transport.order = approved_order()
    before = stored(created_order)

    verify(created_order)

    after = stored(created_order)
    assert (after.paypal_amount, after.paypal_currency) == (AMOUNT, CURRENCY)
    assert (before.paypal_amount, before.paypal_currency) == (AMOUNT, CURRENCY)
    sent = order_calls(paypal_http)[0]["json"]["purchase_units"][0]["amount"]
    assert sent == {"currency_code": CURRENCY, "value": AMOUNT}
