"""Safe external capture (Phase 11F-D).

THE FIRST PHASE THAT MOVES MONEY FOR AN EXTERNAL PRODUCT, and these tests
exist to keep that single call deliberate, checked, and impossible to make
twice.

THE PROPERTIES DEFENDED HERE

 1. THREE INDEPENDENT ACTS ARE REQUIRED, AND NONE IMPLIES THE NEXT.
    An ORDER_CREATED execution cannot be captured. A BUYER_APPROVED one
    cannot be captured either, until capture is EXPLICITLY authorized. And
    the authorization expires. Each has its own failing test asserting that
    PayPal's capture endpoint was never reached.

 2. THE ORDER IS RE-READ ANYWAY. Even with all three acts in hand, the
    order must still be that id, still APPROVED, still exactly one purchase
    unit at exactly the recorded amount and currency.

 3. ONE CAPTURE CALL, EVER. A compare-and-set claims the slot before PayPal
    is contacted. Concurrent requests race the real endpoint and exactly one
    reaches capture. A crash after reserving leaves CAPTURING, which refuses
    rather than recapturing. No outcome is ever retried automatically.

 4. PAYPAL'S ANSWER IS READ STRICTLY. CAPTURED requires nested COMPLETED
    plus a capture id plus the recorded amount and currency. PENDING is a
    pending outcome with captured=false. DECLINED and anything unknown are
    refusals. ORDER_ALREADY_CAPTURED is never a success on PayPal's word
    alone — the existing capture is independently re-read and verified.

 5. NOTHING LEAKS INTO THE CATALOG. The catalog capture service is
    booby-trapped for every test here, the `transactions` ledger is asserted
    empty, the 11F-B0 gate still refuses the order on both catalog capture
    routes, and the external capture id is refused by the refund route.

 6. NOTHING IS ROLLED BACK OR INVENTED. When a call was made and the outcome
    was bad, the refusal says so (`paypal_capture_attempted: true`,
    `rolled_back: false`) and whatever PayPal reported is persisted.

PayPal HTTP is fully mocked throughout. No live order, approval, capture or
refund happens anywhere in this file.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.external_purchase import (
    ExternalCaptureFailure as Failure,
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

# The 11E/11F-C chain, reused rather than duplicated.
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

ORDER_ID = "EXTORDER123"
CAPTURE_ID = "EXTCAP456"
AMOUNT = "19.99"
CURRENCY = "USD"

#: The genuine clients, captured at import time — before any fixture
#: booby-traps the module attributes.
REAL_GET_ORDER = external_purchase_service.get_order
REAL_CAPTURE_ORDER = external_purchase_service.capture_order

APPROVED_ORDER = {
    "order_id": ORDER_ID,
    "order_status": "APPROVED",
    "intent": "CAPTURE",
    "purchase_units": [{"amount": {"currency_code": CURRENCY, "value": AMOUNT}}],
    "captures": [],
    "links": [],
    "approve_url": None,
}

COMPLETED_CAPTURE = {
    "order_id": ORDER_ID,
    "status": "COMPLETED",
    "capture_id": CAPTURE_ID,
    "capture_status": "COMPLETED",
    "amount": AMOUNT,
    "currency": CURRENCY,
}


def approved_order(**overrides) -> dict:
    order = json.loads(json.dumps(APPROVED_ORDER))
    order.update(overrides)
    return order


def capture_response(**overrides) -> dict:
    result = dict(COMPLETED_CAPTURE)
    result.update(overrides)
    return result


def authorize(approval_id: str, **kwargs):
    return client.post(
        f"/discovery/approvals/{approval_id}/authorize-capture", **kwargs
    )


def capture(approval_id: str, **kwargs):
    return client.post(f"/discovery/approvals/{approval_id}/capture", **kwargs)


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


def set_status(execution_id: str, status: str) -> None:
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET status = ? WHERE execution_id = ?",
            (status, execution_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def catalog_is_untouchable(monkeypatch):
    """The CATALOG capture/refund services are booby-trapped everywhere here.

    External capture is allowed to call paypal.capture_order. It is never
    allowed to route through the catalog capture service or write the
    catalog ledger, in any branch — success included.
    """
    hits: list[str] = []

    def bomb(name):
        def _bomb(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"external capture must never touch {name}")

        return _bomb

    monkeypatch.setattr(
        purchase_execution_service, "capture_purchase", bomb("the catalog capture service")
    )
    monkeypatch.setattr(
        purchase_execution_service, "capture_order", bomb("the catalog capture path")
    )
    monkeypatch.setattr(refund_service, "refund_capture", bomb("refunds"))
    monkeypatch.setattr(
        transaction_service, "create_transaction", bomb("the catalog ledger")
    )
    monkeypatch.setattr(
        transaction_service, "mark_captured", bomb("the catalog ledger")
    )
    monkeypatch.setattr(
        transaction_service, "update_status", bomb("the catalog ledger")
    )
    return hits


@pytest.fixture
def transport(monkeypatch):
    """Drive get_order and capture_order at the service boundary.

    Records every call so "PayPal's capture endpoint was never reached" is
    an assertion about facts, not about absence of evidence.
    """
    state = SimpleNamespace(
        order=None,
        order_raises=None,
        capture=None,
        capture_raises=None,
        get_calls=[],
        capture_calls=[],
    )

    def fake_get_order(order_id):
        state.get_calls.append(order_id)
        if state.order_raises is not None:
            raise state.order_raises
        return state.order if state.order is not None else approved_order()

    def fake_capture_order(order_id):
        state.capture_calls.append(order_id)
        if state.capture_raises is not None:
            raise state.capture_raises
        return state.capture if state.capture is not None else capture_response()

    monkeypatch.setattr(external_purchase_service, "get_order", fake_get_order)
    monkeypatch.setattr(
        external_purchase_service, "capture_order", fake_capture_order
    )
    return state


@pytest.fixture
def created(adopted, paypal_http, transport):
    """An ORDER_CREATED execution via the real 11E flow. Returns approval id."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    assert post_create_order(approval.approval_id).status_code == 200
    return approval.approval_id


@pytest.fixture
def verified(created, transport):
    """A BUYER_APPROVED execution (11F-C verification, PayPal mocked).

    The call counters are cleared afterwards so a test measures only the
    PayPal traffic IT caused, not the fixture's own verification read.
    """
    resp = client.post(f"/discovery/approvals/{created}/verify-approval")
    assert resp.status_code == 200, resp.text
    assert stored(created).status is ExternalOrderStatus.BUYER_APPROVED
    transport.get_calls.clear()
    transport.capture_calls.clear()
    return created


@pytest.fixture
def authorized(verified, transport):
    """A BUYER_APPROVED execution with an explicit capture authorization."""
    resp = authorize(verified)
    assert resp.status_code == 200, resp.text
    transport.get_calls.clear()
    return verified


@pytest.fixture
def captured(authorized, transport):
    """A CAPTURED execution."""
    resp = capture(authorized)
    assert resp.status_code == 200, resp.text
    transport.get_calls.clear()
    transport.capture_calls.clear()
    return authorized


# ===========================================================================
# 1. Authorization: the second deliberate act
# ===========================================================================


def test_authorizing_captures_nothing_and_calls_no_paypal(verified, transport):
    resp = authorize(verified)

    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is True
    assert body["captured"] is False
    assert body["capture_window_seconds"] == 900
    assert body["capture_authorized_at"]
    assert body["capture_expires_at"] > body["capture_authorized_at"]
    assert transport.capture_calls == []
    assert transport.get_calls == []
    record = stored(verified)
    assert record.status is ExternalOrderStatus.BUYER_APPROVED  # status unchanged
    assert record.capture_authorized_at == body["capture_authorized_at"]


def test_the_window_is_configurable_and_separate_from_the_revalidation_ttl(
    verified, monkeypatch
):
    from app.services import external_approval_service

    monkeypatch.setenv("EXTERNAL_CAPTURE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("EXTERNAL_REVALIDATION_TTL_SECONDS", "7")

    body = authorize(verified).json()

    assert body["capture_window_seconds"] == 60
    # Deliberately NOT the revalidation TTL: different question, different
    # evidence, different setting.
    assert external_approval_service.revalidation_ttl_seconds() == 7
    assert external_purchase_service.capture_window_seconds() == 60


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "9.5", ""])
def test_a_misconfigured_window_is_a_500_not_a_default(verified, monkeypatch, bad):
    monkeypatch.setenv("EXTERNAL_CAPTURE_WINDOW_SECONDS", bad)
    resp = authorize(verified)
    if bad == "":  # empty means "unset" -> the documented default
        assert resp.status_code == 200
        return
    assert resp.status_code == 500
    assert stored(verified).capture_authorized_at is None


def test_an_unverified_order_cannot_be_authorized(created, transport):
    resp = authorize(created)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_NOT_PERMITTED_IN_STATE.value
    assert stored(created).capture_authorized_at is None
    assert transport.capture_calls == []


def test_an_unknown_approval_cannot_be_authorized(transport):
    assert authorize("xapr_nope").status_code == 404
    assert transport.capture_calls == []


def test_a_blank_approval_id_is_422(transport):
    assert authorize("%20").status_code == 422
    assert capture("%20").status_code == 422
    assert transport.capture_calls == []


def test_reauthorizing_refreshes_the_window(authorized, transport):
    first = stored(authorized).capture_authorized_at
    second = authorize(authorized).json()["capture_authorized_at"]
    assert second >= first
    assert transport.capture_calls == []


def test_a_captured_execution_cannot_be_reauthorized(captured):
    resp = authorize(captured)
    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_NOT_PERMITTED_IN_STATE.value


def test_the_authorize_route_ignores_body_and_query(verified, transport):
    """A body and a query string change nothing about what is authorized."""
    resp = client.post(
        f"/discovery/approvals/{verified}/authorize-capture"
        f"?token=X&PayerID=Y&capture_window_seconds=999999&captured=true",
        json={
            "authorized": True,
            "capture": True,
            "amount": "0.01",
            "capture_window_seconds": 999999,
            "capture_authorized_at": "2099-01-01T00:00:00+00:00",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["capture_window_seconds"] == 900          # not 999999
    assert not body["capture_authorized_at"].startswith("2099")  # server clock
    assert body["captured"] is False
    assert transport.capture_calls == []
    record = stored(verified)
    assert record.status is ExternalOrderStatus.BUYER_APPROVED
    assert record.capture_authorized_at == body["capture_authorized_at"]


# ===========================================================================
# 2. Capture requires all three acts
# ===========================================================================


def test_an_order_created_execution_cannot_be_captured(created, transport):
    resp = capture(created)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURE_NOT_PERMITTED_IN_STATE.value
    assert body["paypal_capture_attempted"] is False
    assert transport.capture_calls == []
    assert stored(created).status is ExternalOrderStatus.ORDER_CREATED


def test_a_verified_but_unauthorized_execution_cannot_be_captured(
    verified, transport
):
    """A verified buyer approval is NOT authorization to collect money."""
    resp = capture(verified)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURE_NOT_AUTHORIZED.value
    assert body["paypal_capture_attempted"] is False
    assert transport.capture_calls == []
    assert transport.get_calls == []  # refused before even re-reading
    assert stored(verified).status is ExternalOrderStatus.BUYER_APPROVED


def test_an_expired_authorization_cannot_capture(authorized, transport, monkeypatch):
    expired = external_purchase_service._now_dt() - timedelta(seconds=901)
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET capture_authorized_at = ?",
            (expired.isoformat(),),
        )
        conn.commit()

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURE_AUTHORIZATION_EXPIRED.value
    assert body["paypal_capture_attempted"] is False
    assert body["observed"]["capture_window_seconds"] == 900
    assert transport.capture_calls == []
    assert stored(authorized).status is ExternalOrderStatus.BUYER_APPROVED


def test_an_expired_authorization_can_be_renewed(authorized, transport):
    expired = external_purchase_service._now_dt() - timedelta(seconds=901)
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET capture_authorized_at = ?",
            (expired.isoformat(),),
        )
        conn.commit()
    assert capture(authorized).status_code == 409

    assert authorize(authorized).status_code == 200
    resp = capture(authorized)

    assert resp.status_code == 200
    assert resp.json()["captured"] is True


def test_an_unreadable_authorization_timestamp_is_treated_as_expired(
    authorized, transport
):
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET capture_authorized_at = ?",
            ("not-a-timestamp",),
        )
        conn.commit()

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_AUTHORIZATION_EXPIRED.value
    assert transport.capture_calls == []


def test_an_incomplete_record_cannot_be_captured(authorized, transport):
    execution = stored(authorized)
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET paypal_amount = NULL "
            "WHERE execution_id = ?",
            (execution.execution_id,),
        )
        conn.commit()

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.RECORDED_ORDER_INCOMPLETE.value
    assert transport.capture_calls == []


def test_an_unknown_approval_cannot_be_captured(transport):
    assert capture("xapr_nope").status_code == 404
    assert transport.capture_calls == []
    assert execution_rows() == []


# ===========================================================================
# 3. The pre-capture re-read must still match
# ===========================================================================


def test_a_non_approved_order_is_not_captured(authorized, transport):
    transport.order = approved_order(order_status="CREATED")

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.ORDER_NOT_APPROVED.value
    assert body["paypal_capture_attempted"] is False
    assert transport.capture_calls == []
    assert stored(authorized).status is ExternalOrderStatus.BUYER_APPROVED


@pytest.mark.parametrize("status", ["CREATED", "VOIDED", "COMPLETED", "", None])
def test_every_non_approved_status_refuses_before_capture(
    authorized, transport, status
):
    transport.order = approved_order(order_status=status)
    assert capture(authorized).status_code == 409
    assert transport.capture_calls == []


def test_an_order_id_mismatch_refuses_before_capture(authorized, transport):
    transport.order = approved_order(order_id="OTHER-ORDER")

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.ORDER_ID_MISMATCH.value
    assert transport.capture_calls == []


@pytest.mark.parametrize("value", ["0.01", "19.98", "199.90"])
def test_an_amount_mismatch_refuses_before_capture(authorized, transport, value):
    transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": CURRENCY, "value": value}}]
    )

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.AMOUNT_MISMATCH.value
    assert transport.capture_calls == []
    assert stored(authorized).status is ExternalOrderStatus.BUYER_APPROVED


def test_a_currency_mismatch_refuses_before_capture(authorized, transport):
    transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": "EUR", "value": AMOUNT}}]
    )

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CURRENCY_MISMATCH.value
    assert transport.capture_calls == []


@pytest.mark.parametrize(
    "units",
    [None, [], "not-a-list", [{}], [{"amount": {}}], [{"amount": {"value": AMOUNT}}]],
)
def test_missing_purchase_units_refuse_before_capture(authorized, transport, units):
    transport.order = approved_order(purchase_units=units)

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.PURCHASE_UNIT_MISSING.value
    assert transport.capture_calls == []


def test_ambiguous_purchase_units_refuse_before_capture(authorized, transport):
    unit = {"amount": {"currency_code": CURRENCY, "value": AMOUNT}}
    transport.order = approved_order(purchase_units=[unit, unit])

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.PURCHASE_UNIT_AMBIGUOUS.value
    assert body["observed"]["purchase_unit_count"] == 2
    assert transport.capture_calls == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "abc", "$19.99"])
def test_a_malformed_order_amount_refuses_before_capture(
    authorized, transport, value
):
    transport.order = approved_order(
        purchase_units=[{"amount": {"currency_code": CURRENCY, "value": value}}]
    )
    resp = capture(authorized)
    assert resp.status_code == 409
    assert detail(resp)["code"] in (
        Failure.AMOUNT_MALFORMED.value,
        Failure.AMOUNT_MISMATCH.value,
    )
    assert transport.capture_calls == []


UPSTREAM_READ_FAILURES = [
    paypal.PayPalAuthError("invalid_client"),
    paypal.PayPalOrderNotFoundError("no such order"),
    paypal.PayPalGetOrderError("HTTP 500"),
]


@pytest.mark.parametrize("failure", UPSTREAM_READ_FAILURES)
def test_an_unreadable_order_refuses_before_capture(authorized, transport, failure):
    transport.order_raises = failure

    resp = capture(authorized)

    assert resp.status_code == 502
    body = detail(resp)
    assert body["code"] == Failure.ORDER_UNREADABLE.value
    assert body["paypal_capture_attempted"] is False
    assert transport.capture_calls == []
    assert stored(authorized).status is ExternalOrderStatus.BUYER_APPROVED


# ===========================================================================
# 4. The happy path
# ===========================================================================


def test_a_fully_authorized_order_captures(authorized, transport):
    resp = capture(authorized)

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "CAPTURED"
    assert body["captured"] is True
    assert body["replay"] is False
    assert body["refundable"] is False
    assert body["paypal_capture_id"] == CAPTURE_ID
    assert body["paypal_capture_status"] == "COMPLETED"
    assert body["captured_amount"] == AMOUNT
    assert body["captured_currency"] == CURRENCY
    assert body["execution"]["status"] == "CAPTURED"
    # The STORED order id was captured, exactly once.
    assert transport.capture_calls == [ORDER_ID]


def test_the_capture_is_persisted(authorized, transport):
    capture(authorized)

    record = stored(authorized)
    assert record.status is ExternalOrderStatus.CAPTURED
    assert record.paypal_capture_id == CAPTURE_ID
    assert record.paypal_capture_status == "COMPLETED"
    assert record.captured_amount == AMOUNT
    assert record.captured_currency == CURRENCY
    # Money provenance untouched.
    assert record.paypal_amount == AMOUNT
    assert record.paypal_currency == CURRENCY
    assert record.paypal_order_id == ORDER_ID


@pytest.mark.parametrize("equivalent", ["19.99", "19.990"])
def test_an_equal_captured_amount_in_another_spelling_is_accepted(
    authorized, transport, equivalent
):
    transport.capture = capture_response(amount=equivalent)
    resp = capture(authorized)
    assert resp.status_code == 200
    assert resp.json()["captured"] is True


# ===========================================================================
# 5. PayPal's capture response is read strictly
# ===========================================================================


def test_a_pending_capture_is_not_a_success(authorized, transport):
    transport.capture = capture_response(capture_status="PENDING")

    resp = capture(authorized)

    assert resp.status_code == 202  # Accepted, NOT 200
    body = resp.json()
    assert body["outcome"] == "PENDING"
    assert body["captured"] is False
    assert body["paypal_capture_status"] == "PENDING"
    record = stored(authorized)
    assert record.status is ExternalOrderStatus.CAPTURE_PENDING
    assert record.paypal_capture_id == CAPTURE_ID  # observed facts kept


def test_a_declined_capture_is_a_refusal(authorized, transport):
    transport.capture = capture_response(capture_status="DECLINED")

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURE_DECLINED.value
    assert body["captured"] is False
    assert body["paypal_capture_attempted"] is True
    assert body["rolled_back"] is False
    record = stored(authorized)
    assert record.status is ExternalOrderStatus.CAPTURE_DECLINED
    assert record.paypal_capture_status == "DECLINED"


@pytest.mark.parametrize(
    "status", ["FAILED", "REFUNDED", "PARTIALLY_REFUNDED", "", None, 42, "completed_"]
)
def test_an_unrecognised_capture_status_is_unconfirmed(authorized, transport, status):
    transport.capture = capture_response(capture_status=status)

    resp = capture(authorized)

    assert resp.status_code == 502
    assert detail(resp)["code"] == Failure.CAPTURE_UNCONFIRMED.value
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


@pytest.mark.parametrize("spelling", ["completed", "Completed", "COMPLETED"])
def test_the_capture_status_comparison_is_case_insensitive(
    authorized, transport, spelling
):
    transport.capture = capture_response(capture_status=spelling)
    assert capture(authorized).status_code == 200


def test_a_completed_capture_without_a_capture_id_is_rejected(authorized, transport):
    transport.capture = capture_response(capture_id=None)

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURED_RESULT_MISMATCH.value
    assert body["paypal_capture_attempted"] is True
    assert body["rolled_back"] is False
    assert "NOT ROLLED BACK" in body["message"]
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


@pytest.mark.parametrize("value", ["0.01", "199.90", "19.9", "NaN", None, "abc"])
def test_a_captured_amount_mismatch_is_rejected_but_recorded(
    authorized, transport, value
):
    """PayPal captured something else. Money moved and is NOT rolled back."""
    transport.capture = capture_response(amount=value)

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURED_RESULT_MISMATCH.value
    assert body["paypal_capture_attempted"] is True
    assert body["rolled_back"] is False
    record = stored(authorized)
    assert record.status is ExternalOrderStatus.CAPTURE_FAILED
    # What PayPal reported is kept, so the discrepancy stays visible.
    assert record.paypal_capture_id == CAPTURE_ID
    assert record.paypal_capture_status == "COMPLETED"


def test_a_captured_currency_mismatch_is_rejected(authorized, transport):
    transport.capture = capture_response(currency="EUR")

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURED_RESULT_MISMATCH.value
    record = stored(authorized)
    assert record.status is ExternalOrderStatus.CAPTURE_FAILED
    assert record.captured_currency == "EUR"


def test_a_malformed_capture_response_is_unconfirmed(authorized, transport):
    transport.capture = {}

    resp = capture(authorized)

    assert resp.status_code == 502
    assert detail(resp)["code"] == Failure.CAPTURE_UNCONFIRMED.value
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


# ===========================================================================
# 6. Upstream capture failures: ambiguous, never retried
# ===========================================================================


AMBIGUOUS_FAILURES = [
    paypal.PayPalAuthError("invalid_client"),
    paypal.PayPalOrderNotFoundError("gone"),
    paypal.PayPalCaptureError("HTTP 500"),
    paypal.PayPalCaptureError("Could not reach PayPal to capture the order"),
]


@pytest.mark.parametrize("failure", AMBIGUOUS_FAILURES)
def test_an_ambiguous_capture_failure_is_terminal(authorized, transport, failure):
    transport.capture_raises = failure

    resp = capture(authorized)

    assert resp.status_code == 502
    body = detail(resp)
    assert body["code"] == Failure.CAPTURE_UNCONFIRMED.value
    assert body["paypal_capture_attempted"] is True
    assert body["rolled_back"] is False
    assert body["retried"] is False
    assert "UNKNOWN" in body["message"]
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


@pytest.mark.parametrize("failure", AMBIGUOUS_FAILURES)
def test_an_ambiguous_failure_is_never_automatically_retried(
    authorized, transport, failure
):
    transport.capture_raises = failure
    capture(authorized)
    assert len(transport.capture_calls) == 1

    # Even with PayPal healthy again, a second request must not capture.
    transport.capture_raises = None
    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_ALREADY_ATTEMPTED.value
    assert len(transport.capture_calls) == 1  # STILL exactly one


def test_a_network_timeout_surfaces_through_the_real_client(
    authorized, monkeypatch
):
    """End to end through paypal.capture_order with httpx exploding."""
    monkeypatch.setattr(
        paypal.httpx,
        "post",
        lambda url, **kw: (_ for _ in ()).throw(paypal.httpx.ConnectTimeout("x"))
        if "/capture" in url
        else SimpleNamespace(
            status_code=200, json=lambda: {"access_token": "fake-token"}
        ),
    )
    monkeypatch.setattr(
        external_purchase_service, "capture_order", REAL_CAPTURE_ORDER
    )

    resp = capture(authorized)

    assert resp.status_code == 502
    assert detail(resp)["code"] == Failure.CAPTURE_UNCONFIRMED.value
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


def test_paypal_saying_not_approved_at_capture_is_a_contradiction(
    authorized, transport
):
    """The pre-capture read said APPROVED. Fail closed, terminally."""
    transport.capture_raises = paypal.PayPalOrderNotApprovedError("not approved")

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.ORDER_NOT_APPROVED.value
    assert body["paypal_capture_attempted"] is True
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


# ===========================================================================
# 7. ORDER_ALREADY_CAPTURED is verified independently
# ===========================================================================


def test_already_captured_is_verified_before_being_called_a_success(
    authorized, transport
):
    transport.capture_raises = paypal.PayPalAlreadyCapturedError("already")
    transport.order = approved_order(
        captures=[
            {
                "id": "PRIORCAP1",
                "status": "COMPLETED",
                "amount": {"currency_code": CURRENCY, "value": AMOUNT},
            }
        ]
    )

    resp = capture(authorized)

    assert resp.status_code == 200
    body = resp.json()
    assert body["captured"] is True
    assert body["replay"] is True  # not a NEW capture
    assert body["paypal_capture_id"] == "PRIORCAP1"
    record = stored(authorized)
    assert record.status is ExternalOrderStatus.CAPTURED
    assert record.paypal_capture_id == "PRIORCAP1"
    # The order was re-read to verify it — that is the whole point.
    assert len(transport.get_calls) >= 2


ALREADY_CAPTURED_UNVERIFIABLE = [
    pytest.param([], id="no-captures"),
    pytest.param(None, id="captures-missing"),
    pytest.param(
        [
            {"id": "C1", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}},
            {"id": "C2", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}},
        ],
        id="two-captures",
    ),
    pytest.param(
        [{"id": "C1", "status": "PENDING", "amount": {"currency_code": CURRENCY, "value": AMOUNT}}],
        id="not-completed",
    ),
    pytest.param(
        [{"id": None, "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": AMOUNT}}],
        id="no-capture-id",
    ),
    pytest.param(
        [{"id": "C1", "status": "COMPLETED", "amount": {"currency_code": "EUR", "value": AMOUNT}}],
        id="wrong-currency",
    ),
    pytest.param(
        [{"id": "C1", "status": "COMPLETED", "amount": {"currency_code": CURRENCY, "value": "0.01"}}],
        id="wrong-amount",
    ),
    pytest.param(
        [{"id": "C1", "status": "COMPLETED", "amount": {}}], id="no-amount"
    ),
]


@pytest.mark.parametrize("captures", ALREADY_CAPTURED_UNVERIFIABLE)
def test_an_unverifiable_existing_capture_is_not_a_success(
    authorized, transport, captures
):
    transport.capture_raises = paypal.PayPalAlreadyCapturedError("already")
    transport.order = approved_order(captures=captures)

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.EXISTING_CAPTURE_UNVERIFIABLE.value
    assert body["captured"] is False
    assert body["paypal_capture_attempted"] is True
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


def test_an_unreadable_order_after_already_captured_is_not_a_success(
    authorized, transport, monkeypatch
):
    """The pre-capture read succeeds; the verifying re-read then fails."""
    calls = {"n": 0}

    def flaky_get_order(order_id):
        calls["n"] += 1
        transport.get_calls.append(order_id)
        if calls["n"] == 1:
            return approved_order()
        raise paypal.PayPalGetOrderError("cannot re-read")

    transport.capture_raises = paypal.PayPalAlreadyCapturedError("already")
    monkeypatch.setattr(external_purchase_service, "get_order", flaky_get_order)

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.EXISTING_CAPTURE_UNVERIFIABLE.value
    assert calls["n"] == 2  # it really did try to verify independently
    assert stored(authorized).status is ExternalOrderStatus.CAPTURE_FAILED


# ===========================================================================
# 8. Idempotency, duplicates and concurrency
# ===========================================================================


def test_a_captured_execution_replays_without_calling_paypal(captured, transport):
    before = stored(captured).model_dump()

    resp = capture(captured)

    assert resp.status_code == 200
    body = resp.json()
    assert body["captured"] is True
    assert body["replay"] is True
    assert body["paypal_capture_id"] == CAPTURE_ID
    assert transport.capture_calls == []
    assert transport.get_calls == []
    assert stored(captured).model_dump() == before


def test_many_replays_are_stable(captured, transport):
    before = stored(captured).model_dump()
    for _ in range(5):
        assert capture(captured).json()["replay"] is True
    assert transport.capture_calls == []
    assert stored(captured).model_dump() == before


@pytest.mark.parametrize(
    "state", ["CAPTURE_PENDING", "CAPTURE_DECLINED", "CAPTURE_FAILED"]
)
def test_a_terminal_capture_state_is_never_retried(authorized, transport, state):
    set_status(stored(authorized).execution_id, state)

    resp = capture(authorized)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_ALREADY_ATTEMPTED.value
    assert transport.capture_calls == []
    assert stored(authorized).status.value == state


def test_a_stuck_capturing_row_refuses_rather_than_recapturing(
    authorized, transport
):
    """The crash-after-reserve case: fail closed, visibly recoverable."""
    set_status(stored(authorized).execution_id, "CAPTURING")

    resp = capture(authorized)

    assert resp.status_code == 409
    body = detail(resp)
    assert body["code"] == Failure.CAPTURE_IN_PROGRESS.value
    assert body["paypal_capture_attempted"] is False
    assert "reconciliation" in body["message"].lower()
    assert transport.capture_calls == []
    assert stored(authorized).status is ExternalOrderStatus.CAPTURING


def test_concurrent_captures_call_paypal_exactly_once(authorized, transport):
    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(6)

    def run():
        barrier.wait()
        resp = capture(authorized)
        results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 6
    # EXACTLY ONE capture call reached PayPal.
    assert transport.capture_calls == [ORDER_ID]
    successes = [body for code, body in results if code == 200]
    assert len(successes) >= 1
    assert all(body["captured"] is True for body in successes)
    # Everything else was refused, never a second capture.
    for code, body in results:
        if code != 200:
            assert code == 409
            assert body["detail"]["code"] in (
                Failure.CAPTURE_IN_PROGRESS.value,
                Failure.CAPTURE_ALREADY_ATTEMPTED.value,
            )
    assert stored(authorized).status is ExternalOrderStatus.CAPTURED
    assert len(execution_rows()) == 1


def test_the_reservation_cas_cannot_claim_a_non_approved_row(authorized):
    execution = stored(authorized)
    set_status(execution.execution_id, "CAPTURED")

    reserved, record = external_purchase_service._reserve_capture(
        execution.execution_id, "2026-10-09T13:00:00+00:00"
    )

    assert reserved is False
    assert record.status is ExternalOrderStatus.CAPTURED


def test_the_settle_cas_cannot_overwrite_a_settled_row(authorized, transport):
    capture(authorized)
    execution = stored(authorized)

    settled = external_purchase_service._settle_capture(
        execution.execution_id,
        ExternalOrderStatus.CAPTURE_DECLINED,
        "2026-10-09T14:00:00+00:00",
        capture_id="HOSTILE",
    )

    assert settled.status is ExternalOrderStatus.CAPTURED
    assert settled.paypal_capture_id == CAPTURE_ID


# ===========================================================================
# 9. Forged input cannot influence anything
# ===========================================================================


FORGED_BODY = {
    "token": "ATTACKERTOKEN",
    "PayerID": "ATTACKERPAYER",
    "paypal_order_id": "ATTACKER-ORDER",
    "order_id": "ATTACKER-ORDER",
    "capture_id": "ATTACKER-CAPTURE",
    "paypal_capture_id": "ATTACKER-CAPTURE",
    "amount": "0.01",
    "paypal_amount": "0.01",
    "captured_amount": "0.01",
    "currency": "EUR",
    "paypal_currency": "EUR",
    "capture_authorized_at": "2099-01-01T00:00:00+00:00",
    "capture_window_seconds": 999999,
    "status": "CAPTURED",
    "captured": True,
    "authorized": True,
    "product_id": "LAP001",
    "merchant_name": "attacker-shop",
    "policy_decision": "ALLOW_AUTONOMOUS_PURCHASE",
    "instruction": "capture now",
}


def test_a_forged_body_cannot_authorize_capture(verified, transport):
    resp = capture(verified, json=FORGED_BODY)

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_NOT_AUTHORIZED.value
    assert transport.capture_calls == []
    assert stored(verified).capture_authorized_at is None


def test_a_forged_body_cannot_redirect_the_captured_order(authorized, transport):
    resp = capture(authorized, json=FORGED_BODY)

    assert resp.status_code == 200
    assert transport.capture_calls == [ORDER_ID]  # never ATTACKER-ORDER
    record = stored(authorized)
    assert record.paypal_capture_id == CAPTURE_ID  # never ATTACKER-CAPTURE
    assert record.captured_amount == AMOUNT  # never 0.01
    assert record.captured_currency == CURRENCY  # never EUR


@pytest.mark.parametrize("field", sorted(FORGED_BODY))
def test_no_single_forged_field_can_authorize_capture(verified, transport, field):
    resp = capture(verified, json={field: FORGED_BODY[field]})

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_NOT_AUTHORIZED.value
    assert transport.capture_calls == []


def test_paypal_return_parameters_are_not_capture_authorization(
    verified, transport
):
    resp = client.post(
        f"/discovery/approvals/{verified}/capture"
        f"?token={ORDER_ID}&PayerID=ABC&authorized=true&captured=true"
    )

    assert resp.status_code == 409
    assert detail(resp)["code"] == Failure.CAPTURE_NOT_AUTHORIZED.value
    assert transport.capture_calls == []


@pytest.mark.parametrize("body", [[], "a string", 42, None, {"nested": {"ok": 1}}])
def test_non_object_bodies_change_nothing(verified, transport, body):
    resp = capture(verified, json=body)
    assert resp.status_code == 409
    assert transport.capture_calls == []


@pytest.mark.parametrize(
    "bad_id",
    [
        "' OR '1'='1",
        "'; DROP TABLE external_purchase_orders;--",
        "' UNION SELECT * FROM transactions--",
        "A" * 4096,
        "{{7*7}}",
    ],
)
def test_malicious_approval_ids_capture_nothing(authorized, transport, bad_id):
    before = stored(authorized).model_dump()

    assert client.post(
        f"/discovery/approvals/{bad_id}/capture"
    ).status_code in (404, 409, 422)
    assert client.post(
        f"/discovery/approvals/{bad_id}/authorize-capture"
    ).status_code in (404, 409, 422)

    assert transport.capture_calls == []
    assert stored(authorized).model_dump() == before
    assert len(execution_rows()) == 1


# ===========================================================================
# 10. Trust boundaries: the catalog stays isolated
# ===========================================================================


def test_capture_writes_no_catalog_ledger_row(captured, catalog_is_untouchable):
    assert transaction_service.list_transactions() == []
    assert catalog_is_untouchable == []


@pytest.mark.parametrize("route", ["/purchase/capture", "/paypal/capture-order"])
def test_a_captured_external_order_is_still_refused_by_catalog_routes(
    captured, route
):
    resp = client.post(f"{route}/{ORDER_ID}")

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EXTERNAL_ORDER_NOT_CAPTURABLE_HERE"
    assert transaction_service.list_transactions() == []
    assert stored(captured).status is ExternalOrderStatus.CAPTURED


def test_the_external_capture_id_cannot_be_refunded(captured):
    resp = client.post(f"/paypal/refund/{CAPTURE_ID}")

    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["code"] == "EXTERNAL_CAPTURE_NOT_REFUNDABLE_HERE"
    assert body["owner"] == "EXTERNAL"
    assert stored(captured).status is ExternalOrderStatus.CAPTURED


def test_the_ownership_gate_still_classifies_the_order_as_external(captured):
    owner, record = order_ownership_service.resolve_order_owner(ORDER_ID)
    assert owner is order_ownership_service.OrderOwner.EXTERNAL
    assert record.status is ExternalOrderStatus.CAPTURED


def test_the_service_has_no_refund_capability():
    for forbidden in ("refund_capture", "refund_transaction", "capture_purchase"):
        assert not hasattr(external_purchase_service, forbidden), forbidden
    for forbidden in ("refund_id", "paypal_refund_id", "refunded_amount"):
        assert forbidden not in ExternalPurchaseOrder.model_fields
    assert "REFUNDED" not in {s.value for s in ExternalOrderStatus}


def test_the_capture_result_never_claims_refundability(captured, transport):
    body = capture(captured).json()
    assert body["refundable"] is False
    assert "refund_id" not in json.dumps(body).lower()


def test_the_read_endpoint_shows_the_captured_state(captured):
    resp = client.get(f"/discovery/approvals/{captured}/order")

    assert resp.status_code == 200
    body = resp.json()
    assert body["execution"]["status"] == "CAPTURED"
    assert body["execution"]["paypal_capture_id"] == CAPTURE_ID
    assert body["execution"]["captured_amount"] == AMOUNT


def test_create_order_replay_on_a_captured_execution_is_truthful(
    captured, paypal_http
):
    """A CAPTURED order exists — a replay must not call it a failed one."""
    resp = post_create_order(captured)

    assert resp.status_code == 200
    assert resp.json()["created"] is False
    assert resp.json()["execution"]["status"] == "CAPTURED"
    assert len(order_calls(paypal_http)) == 1


def test_verification_replay_on_a_captured_execution_is_truthful(captured):
    """A captured order WAS buyer-approved; saying otherwise would be false."""
    resp = client.post(f"/discovery/approvals/{captured}/verify-approval")

    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["transitioned"] is False
    assert body["verification_source"] == "stored"


def test_no_credentials_leak_in_capture_responses(authorized, transport):
    bodies = [capture(authorized).text]
    transport.capture_raises = paypal.PayPalAuthError("invalid_client")
    bodies.append(capture(authorized).text)

    for text in bodies:
        lowered = text.lower()
        for secret in ("client_secret", "access_token", "fake-token", "bearer"):
            assert secret not in lowered
