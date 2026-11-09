"""Order-id persistence before final settlement (Phase 11F-K.16).

WHAT THIS PHASE CHANGED
    Phase 11E/11F-B created a PayPal order for an approved external product in
    two writes: a CREATING reservation committed BEFORE PayPal, then a single
    final settlement to ORDER_CREATED that recorded the PayPal order id. A
    crash (or any failure) after create_order returned but before that
    settlement committed left the reservation CREATING with a NULL order id --
    a real PayPal order existed that NO local row named, and the existing
    lookup/reconciliation paths (which match a SERVER-STORED order id) could
    never find it.

    Now the order id is persisted in its OWN committed write, guarded on
    (CREATING AND order id IS NULL), the instant create_order returns a valid
    id and BEFORE the final settlement. A crash after that point leaves a
    CREATING record that still names its order, so the id is recoverable.

THE PROPERTIES DEFENDED HERE
    1. ORDERING. The id is committed while the record is still CREATING, and
       the final ORDER_CREATED settlement happens only afterwards.
    2. DURABILITY. If the final settlement fails after the id commit, the
       CREATING record keeps the order id (it is a separate transaction).
    3. RECOVERABILITY. The stored id is found by the existing lookup /
       reconciliation resolution, which never treats CREATING as captured or
       approved and moves no money.
    4. FAIL CLOSED. A guarded update that moves no row, or a UNIQUE conflict
       with a different execution's id, is reported not-claimed and never
       overwrites a stored id; the create path then refuses rather than
       settling an inconsistent row or marking FAILED (which would hide the
       order).
    5. AT MOST ONE CREATE-ORDER CALL, STILL. Every scenario asserts exactly
       one create-order call and traps capture / refund / second create.

PayPal HTTP is fully mocked (the 11E fixtures). Nothing here contacts PayPal,
and no money-moving method may run in the recovery-related tests -- explicit
traps prove it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import paypal
from app.schemas.external_purchase import ExternalOrderStatus
from app.services import (
    external_purchase_service as eps,
    external_reconciliation_service,
    refund_service,
    transaction_service,
)

# The real 11E chain + mocked PayPal transport, imported rather than
# duplicated so these tests run against the exact production flow.
from test_external_purchase_order import (  # noqa: F401 - pytest fixtures
    NOW,
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

#: The order id the 11E fake returns (ORDER_RESPONSE["id"]).
ORDER_ID = "EXTORDER123"
NOW_ISO = NOW.isoformat()


def _boom(*args, **kwargs):
    raise RuntimeError("final settlement crashed")


def seed_approval(adopted_product):
    """Settings + fresh revalidation + an APPROVED approval, no order yet."""
    require_approval()
    snapshot(adopted_product.adopted_product_id)
    return approved_approval(adopted_product.adopted_product_id)


def _reserve_creating(approval_id: str) -> str:
    """A bare CREATING reservation with a NULL order id. Returns execution id."""
    reservation, _ = eps._reserve(
        SimpleNamespace(
            approval_id=approval_id,
            adopted_product_id="adopt_x",
            revalidation_id="reval_x",
        ),
        NOW_ISO,
    )
    return reservation.execution_id


@pytest.fixture
def money_trap(monkeypatch):
    """Arm-on-demand traps over every money-moving method and a second create.

    Armed explicitly (not at setup) because the orphan under test has to be
    created through the normal flow first; only then is "nothing afterwards
    moves money or creates a second order" a meaningful claim.
    """
    hits: list[str] = []

    def bomb(name):
        def _b(*a, **k):
            hits.append(name)
            raise AssertionError(f"recovery must never call {name}")

        return _b

    def arm():
        monkeypatch.setattr(eps, "capture_order", bomb("eps.capture_order"))
        monkeypatch.setattr(eps, "create_order", bomb("eps.create_order"))
        monkeypatch.setattr(eps, "get_order", bomb("eps.get_order"))
        monkeypatch.setattr(paypal, "capture_order", bomb("paypal.capture_order"))
        monkeypatch.setattr(paypal, "refund_capture", bomb("paypal.refund_capture"))
        monkeypatch.setattr(refund_service, "refund_capture", bomb("refund_capture"))
        monkeypatch.setattr(
            transaction_service, "create_transaction", bomb("catalog ledger")
        )
        monkeypatch.setattr(
            transaction_service, "mark_captured", bomb("catalog ledger")
        )

    return SimpleNamespace(arm=arm, hits=hits)


# ===========================================================================
# 1. The order id is committed while CREATING, BEFORE final settlement
# ===========================================================================


def test_order_id_is_committed_while_creating_before_settlement(
    adopted, paypal_http, monkeypatch
):
    real_settle = eps._settle
    seen: dict = {}

    def spy_settle(execution_id, status, now, **kwargs):
        # Capture the stored row state at the instant settlement BEGINS.
        row = eps.get(execution_id)
        seen["status_at_settle"] = row.status
        seen["order_id_at_settle"] = row.paypal_order_id
        return real_settle(execution_id, status, now, **kwargs)

    monkeypatch.setattr(eps, "_settle", spy_settle)

    approval = seed_approval(adopted)
    result = eps.create_order_for_approval(approval.approval_id)

    # The id was already persisted, and the row was still CREATING, when the
    # final settlement started.
    assert seen["status_at_settle"] is ExternalOrderStatus.CREATING
    assert seen["order_id_at_settle"] == ORDER_ID

    # And the end state is the usual ORDER_CREATED carrying the id.
    assert result.created is True
    stored = eps.get_by_approval(approval.approval_id)
    assert stored.status is ExternalOrderStatus.ORDER_CREATED
    assert stored.paypal_order_id == ORDER_ID
    assert len(order_calls(paypal_http)) == 1


# ===========================================================================
# 2. Settlement failure AFTER the id commit leaves CREATING + the order id
# ===========================================================================


def test_settlement_failure_after_id_commit_retains_order_id(
    adopted, paypal_http, monkeypatch
):
    monkeypatch.setattr(eps, "_settle", _boom)
    approval = seed_approval(adopted)

    # The crash in the final settlement propagates (never a false success).
    with pytest.raises(RuntimeError):
        eps.create_order_for_approval(approval.approval_id)

    # The reservation is left CREATING but NAMES the order it created: the
    # committed id write survived the failed settlement (separate transaction).
    stored = eps.get_by_approval(approval.approval_id)
    assert stored.status is ExternalOrderStatus.CREATING
    assert stored.paypal_order_id == ORDER_ID
    # The order was created exactly once; nothing retried.
    assert len(order_calls(paypal_http)) == 1


# ===========================================================================
# 3. The stored id is recoverable by existing lookup/reconciliation, and no
#    money moves and no CREATING row is treated as captured/approved
# ===========================================================================


def test_creating_orphan_is_resolvable_by_order_id_without_moving_money(
    adopted, paypal_http, monkeypatch, money_trap
):
    # Build a CREATING-with-id orphan via a failed final settlement.
    approval = seed_approval(adopted)
    with monkeypatch.context() as m:
        m.setattr(eps, "_settle", _boom)
        with pytest.raises(RuntimeError):
            eps.create_order_for_approval(approval.approval_id)

    orphan = eps.get_by_approval(approval.approval_id)
    assert orphan.status is ExternalOrderStatus.CREATING
    assert orphan.paypal_order_id == ORDER_ID

    # From here on, nothing may move money or create a second order.
    money_trap.arm()

    # (a) The existing by-order-id lookup now finds the CREATING row.
    found = eps.get_by_paypal_order(ORDER_ID)
    assert found is not None
    assert found.execution_id == orphan.execution_id
    assert found.status is ExternalOrderStatus.CREATING

    # (b) The buyer-approval reconciliation primitive sees CREATING as
    #     not-applicable: no PayPal read, no state change, no money.
    outcome, rec, detail = eps.reconcile_buyer_approval(orphan)
    assert outcome == "not_applicable"
    assert rec.status is ExternalOrderStatus.CREATING

    # (c) The webhook reconciliation service RESOLVES the event to this row by
    #     the stored id, then declines to act on CREATING -- found, but never
    #     captured or approved.
    event = {"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": ORDER_ID}}
    result = external_reconciliation_service.reconcile_external_event(event)
    assert result["action"] == "not_applicable"
    assert result["execution_id"] == orphan.execution_id
    assert result["status"] == "CREATING"

    # Still CREATING, still only one create-order call, and no trap fired.
    assert eps.get(orphan.execution_id).status is ExternalOrderStatus.CREATING
    assert len(order_calls(paypal_http)) == 1
    assert money_trap.hits == []


# ===========================================================================
# 4. The guarded claim fails closed and never overwrites a stored id
# ===========================================================================


def test_claim_guard_refuses_to_overwrite_a_stored_id():
    execution_id = _reserve_creating("xapr_overwrite")

    ok, rec = eps._claim_order_id(execution_id, "ORDER-A", NOW_ISO)
    assert ok is True
    assert rec.paypal_order_id == "ORDER-A"
    assert rec.status is ExternalOrderStatus.CREATING

    # A second claim with a DIFFERENT id matches no row (order id no longer
    # NULL) and must not overwrite the stored one.
    ok2, rec2 = eps._claim_order_id(execution_id, "ORDER-B", NOW_ISO)
    assert ok2 is False
    assert rec2.paypal_order_id == "ORDER-A"  # unchanged
    assert rec2.status is ExternalOrderStatus.CREATING


def test_claim_fails_closed_on_unique_conflict_with_another_execution():
    a = _reserve_creating("xapr_a")
    b = _reserve_creating("xapr_b")

    # B legitimately claims the id first.
    okb, _ = eps._claim_order_id(b, "DUP-ORDER", NOW_ISO)
    assert okb is True

    # A tries to claim the SAME id: the UNIQUE index rejects it, and A is left
    # with a NULL id -- never given a stolen one.
    oka, reca = eps._claim_order_id(a, "DUP-ORDER", NOW_ISO)
    assert oka is False
    assert reca.paypal_order_id is None
    assert reca.status is ExternalOrderStatus.CREATING


def test_create_fails_closed_when_claim_reports_not_claimed(
    adopted, paypal_http, monkeypatch
):
    approval = seed_approval(adopted)

    # Simulate a claim that neither updated a row nor matches a benign
    # re-entry (no record / a different stored id).
    monkeypatch.setattr(eps, "_claim_order_id", lambda ex, oid, now: (False, None))

    with pytest.raises(eps.ExternalOrderPersistenceError) as exc:
        eps.create_order_for_approval(approval.approval_id)
    assert exc.value.paypal_order_id == ORDER_ID

    # Left CREATING (NOT settled, NOT marked FAILED), id not forced in by the
    # failed claim, and only the single real create-order call occurred.
    stored = eps.get_by_approval(approval.approval_id)
    assert stored.status is ExternalOrderStatus.CREATING
    assert stored.paypal_order_id is None
    assert len(order_calls(paypal_http)) == 1


def test_create_proceeds_on_benign_idempotent_reentry(
    adopted, paypal_http, monkeypatch
):
    approval = seed_approval(adopted)
    real_claim = eps._claim_order_id

    def claim_writes_but_reports_not_claimed(execution_id, order_id, now):
        # Actually persist the id (as the real claim would) but report
        # not-claimed, mimicking a retried/duplicated claim where the id is
        # already present on this CREATING row.
        real_claim(execution_id, order_id, now)
        return False, eps.get(execution_id)

    monkeypatch.setattr(eps, "_claim_order_id", claim_writes_but_reports_not_claimed)

    result = eps.create_order_for_approval(approval.approval_id)

    # The benign re-entry is recognised and settlement proceeds normally.
    assert result.created is True
    stored = eps.get_by_approval(approval.approval_id)
    assert stored.status is ExternalOrderStatus.ORDER_CREATED
    assert stored.paypal_order_id == ORDER_ID
    assert len(order_calls(paypal_http)) == 1
