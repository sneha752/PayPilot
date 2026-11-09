"""External checkout-link persistence, recovery, and read (11F-B, C + D).

WHAT THIS PHASE CHANGED
    Phase 11E created a PayPal order for an approved external product and
    returned its approve_url exactly once -- on the request that created it.
    A replay got the order id back with approve_url null, so the buyer had
    an order they could not reach a checkout page for.

    The execution record now persists PayPal's OWN approval href. A replay
    returns it. A record that predates the column recovers the real link
    with a READ-ONLY get_order. And a new GET exposes the record without
    touching anything.

THE PROPERTIES DEFENDED HERE
    1. THE STORED URL IS PAYPAL'S OWN. Every assertion compares against the
       href the fake transport returned. A URL assembled from the order id
       would fail: the fakes deliberately return a checkout host and token
       that cannot be derived from "EXTORDER123" alone.
    2. AT MOST ONE CREATE-ORDER ATTEMPT, STILL. Every replay and every
       recovery path asserts the create-order call count, and the recovery
       transport raises on a create-order URL outright.
    3. RECOVERY FAILS TRUTHFULLY. When no link can be obtained the answer is
       a structured APPROVAL_URL_UNAVAILABLE naming the order that exists --
       never a guessed URL, never a second order.
    4. THE GET MUTATES NOTHING. The record is captured before and compared
       byte-for-byte after, and PayPal is trapped for the duration.
    5. NO CAPTURE OR REFUND CAPABILITY IS ADDED.

The 11E fixtures are imported rather than duplicated, so these tests run
against the same adopted product, revalidation snapshot and approval chain
the existing phase is verified with. PayPal HTTP is fully mocked throughout.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.schemas.external_purchase import (
    ExternalOrderConflict,
    ExternalOrderStatus,
    ExternalPurchaseOrder,
)
from app.services import external_purchase_service

# The 11E chain: adopted product -> revalidation snapshot -> APPROVED
# approval -> create-order. Imported so these tests exercise the real flow.
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

#: What the 11E fake returns as PayPal's approval href. Deliberately NOT
#: derivable from the order id alone -- the host and the token shape are
#: both part of the value, so a fabricated URL cannot accidentally match.
REAL_APPROVE_URL = "https://www.sandbox.paypal.com/checkoutnow?token=EXTORDER123"
ORDER_ID = "EXTORDER123"


def get_order_view(approval_id: str):
    return client.get(f"/discovery/approvals/{approval_id}/order")


def execution_rows() -> list[ExternalPurchaseOrder]:
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(external_purchase_service._SCHEMA)
        external_purchase_service._migrate(conn)
        return [
            ExternalPurchaseOrder(**dict(row))
            for row in conn.execute("SELECT * FROM external_purchase_orders")
        ]


def clear_stored_url(execution_id: str) -> None:
    """Simulate a row written before the column existed."""
    with sqlite3.connect(external_purchase_service._db_path()) as conn:
        conn.execute(
            "UPDATE external_purchase_orders SET paypal_approve_url = NULL "
            "WHERE execution_id = ?",
            (execution_id,),
        )
        conn.commit()


def create_external_order(adopted_product) -> tuple[str, dict]:
    """Run the real 11E flow once. Returns (approval_id, response body)."""
    require_approval()
    snapshot(adopted_product.adopted_product_id)
    approval = approved_approval(adopted_product.adopted_product_id)
    response = post_create_order(approval.approval_id)
    assert response.status_code == 200, response.text
    return approval.approval_id, response.json()


@pytest.fixture
def recovery_transport(monkeypatch):
    """Replace get_order in the service, and trap create_order on arm().

    `state` drives what recovery sees. Patching the name the SERVICE
    imported (not paypal.get_order) is deliberate — it is the binding the
    recovery path actually calls.

    The create_order trap is armed explicitly rather than at setup, because
    these tests must first create a real order through the normal 11E flow
    and only then prove that nothing afterwards creates a second one.
    """
    state = SimpleNamespace(result=None, raises=None, calls=[])

    def fake_get_order(order_id):
        state.calls.append(order_id)
        if state.raises is not None:
            raise state.raises
        return state.result

    def no_create(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("recovery must never call create_order")

    monkeypatch.setattr(external_purchase_service, "get_order", fake_get_order)
    state.arm = lambda: monkeypatch.setattr(
        external_purchase_service, "create_order", no_create
    )
    return state


@pytest.fixture
def no_paypal_at_all(monkeypatch):
    """Trap both transports and both service bindings, on arm().

    Armed explicitly for the same reason as above: the order under test has
    to exist before "no PayPal call happens" is a meaningful claim.
    """
    hits: list[str] = []

    def forbidden(name):
        def _raise(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"PayPal must not be contacted ({name})")

        return _raise

    def arm():
        monkeypatch.setattr(paypal.httpx, "post", forbidden("httpx.post"))
        monkeypatch.setattr(paypal.httpx, "get", forbidden("httpx.get"))
        monkeypatch.setattr(
            external_purchase_service, "get_order", forbidden("get_order")
        )
        monkeypatch.setattr(
            external_purchase_service, "create_order", forbidden("create_order")
        )

    return SimpleNamespace(arm=arm, hits=hits)


# ===========================================================================
# C1. A successful create persists PayPal's own approval URL
# ===========================================================================


def test_create_persists_the_real_approval_url(adopted, paypal_http):
    approval_id, body = create_external_order(adopted)

    assert body["created"] is True
    assert body["approve_url"] == REAL_APPROVE_URL
    assert body["execution"]["paypal_approve_url"] == REAL_APPROVE_URL

    stored = external_purchase_service.get_by_approval(approval_id)
    assert stored.paypal_approve_url == REAL_APPROVE_URL
    assert stored.status is ExternalOrderStatus.ORDER_CREATED
    assert stored.paypal_order_id == ORDER_ID


def test_the_stored_url_is_paypals_href_not_one_built_from_the_order_id(
    adopted, paypal_http
):
    """The create-order response's links decide the URL, nothing else."""
    paypal_http.state.order_payload = {
        "id": ORDER_ID,
        "status": "CREATED",
        "links": [
            {
                "rel": "approve",
                "href": "https://www.sandbox.paypal.com/checkoutnow?token=TOTALLY-DIFFERENT",
            }
        ],
    }

    _, body = create_external_order(adopted)

    assert body["approve_url"].endswith("token=TOTALLY-DIFFERENT")
    assert ORDER_ID not in body["approve_url"]


def test_create_succeeds_and_stores_null_when_paypal_sends_no_link(
    adopted, paypal_http
):
    """The order EXISTS; a missing link is not a failure, it is recoverable."""
    paypal_http.state.order_payload = {"id": ORDER_ID, "status": "CREATED"}

    approval_id, body = create_external_order(adopted)

    assert body["created"] is True
    assert body["approve_url"] is None
    stored = external_purchase_service.get_by_approval(approval_id)
    assert stored.status is ExternalOrderStatus.ORDER_CREATED
    assert stored.paypal_order_id == ORDER_ID
    assert stored.paypal_approve_url is None


@pytest.mark.parametrize(
    "links",
    [
        [{"rel": "approve", "href": "javascript:alert(1)"}],
        [{"rel": "approve", "href": "http://insecure.example/checkout"}],
        [{"rel": "approve"}],
        [{"rel": "self", "href": "https://api-m.paypal.com/v2/x"}],
        "not-a-list",
    ],
)
def test_an_unusable_create_link_is_stored_as_null_not_as_junk(
    adopted, paypal_http, links
):
    paypal_http.state.order_payload = {
        "id": ORDER_ID,
        "status": "CREATED",
        "links": links,
    }

    approval_id, body = create_external_order(adopted)

    assert body["approve_url"] is None
    assert external_purchase_service.get_by_approval(
        approval_id
    ).paypal_approve_url is None


# ===========================================================================
# C2. Replay returns the same order id AND the same URL, with no new order
# ===========================================================================


def test_replay_returns_the_stored_url_without_any_paypal_call(
    adopted, paypal_http, no_paypal_at_all
):
    approval_id, first = create_external_order(adopted)
    before = external_purchase_service.get_by_approval(approval_id).model_dump()
    no_paypal_at_all.arm()

    replay = post_create_order(approval_id)

    assert replay.status_code == 200
    body = replay.json()
    assert body["created"] is False
    assert body["captured"] is False
    assert body["execution"]["paypal_order_id"] == first["execution"][
        "paypal_order_id"
    ]
    assert body["approve_url"] == first["approve_url"] == REAL_APPROVE_URL
    # Not one PayPal call of any kind, and the row is untouched.
    assert no_paypal_at_all.hits == []
    assert external_purchase_service.get_by_approval(approval_id).model_dump() == before


def test_replay_makes_no_second_create_order_call(adopted, paypal_http):
    approval_id, _ = create_external_order(adopted)
    assert len(order_calls(paypal_http)) == 1

    post_create_order(approval_id)
    post_create_order(approval_id)

    assert len(order_calls(paypal_http)) == 1  # still exactly one


def test_many_replays_stay_identical(adopted, paypal_http):
    approval_id, first = create_external_order(adopted)

    bodies = [post_create_order(approval_id).json() for _ in range(5)]

    for body in bodies:
        assert body["created"] is False
        assert body["approve_url"] == REAL_APPROVE_URL
        assert body["execution"]["paypal_order_id"] == ORDER_ID
    assert len(order_calls(paypal_http)) == 1


# ===========================================================================
# C3. Recovery by read-only get_order when the stored URL is missing
# ===========================================================================


def test_a_missing_url_is_recovered_from_paypals_own_links(
    adopted, paypal_http, recovery_transport
):
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()

    recovery_transport.result = {
        "order_id": ORDER_ID,
        "order_status": "CREATED",
        "intent": "CAPTURE",
        "purchase_units": [],
        "captures": [],
        "links": [{"rel": "approve", "href": REAL_APPROVE_URL, "method": "GET"}],
        "approve_url": REAL_APPROVE_URL,
    }

    replay = post_create_order(approval_id)

    assert replay.status_code == 200
    body = replay.json()
    assert body["created"] is False
    assert body["approve_url"] == REAL_APPROVE_URL
    # Recovery READ the order, exactly once, and created nothing.
    assert recovery_transport.calls == [ORDER_ID]
    assert len(order_calls(paypal_http)) == 1


def test_a_recovered_url_is_persisted_so_the_next_replay_needs_no_call(
    adopted, paypal_http, recovery_transport
):
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()
    recovery_transport.result = {"approve_url": REAL_APPROVE_URL}

    post_create_order(approval_id)

    stored = external_purchase_service.get_by_approval(approval_id)
    assert stored.paypal_approve_url == REAL_APPROVE_URL
    # Status, order id and amount are untouched by the recovery write.
    assert stored.status is ExternalOrderStatus.ORDER_CREATED
    assert stored.paypal_order_id == ORDER_ID
    assert stored.paypal_amount == "19.99"
    assert stored.paypal_currency == "USD"

    recovery_transport.calls.clear()
    second = post_create_order(approval_id)
    assert second.json()["approve_url"] == REAL_APPROVE_URL
    assert recovery_transport.calls == []  # served from storage


def test_recovery_reads_the_order_it_stored_not_a_client_value(
    adopted, paypal_http, recovery_transport
):
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()
    recovery_transport.result = {"approve_url": REAL_APPROVE_URL}

    post_create_order(
        approval_id,
        json={"paypal_order_id": "ATTACKER-ORDER", "approve_url": "https://evil.example"},
    )

    assert recovery_transport.calls == [ORDER_ID]
    assert external_purchase_service.get_by_approval(
        approval_id
    ).paypal_approve_url == REAL_APPROVE_URL


# ===========================================================================
# C4. Recovery failure is structured and truthful
# ===========================================================================


RECOVERY_FAILURES = [
    paypal.PayPalGetOrderError("HTTP 500 from PayPal"),
    paypal.PayPalAuthError("no token"),
    paypal.PayPalOrderNotFoundError("no such order"),
]


@pytest.mark.parametrize("failure", RECOVERY_FAILURES)
def test_recovery_failure_returns_a_structured_error(
    adopted, paypal_http, recovery_transport, failure
):
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()
    recovery_transport.raises = failure

    replay = post_create_order(approval_id)

    assert replay.status_code == 409
    detail = replay.json()["detail"]
    assert detail["code"] == ExternalOrderConflict.APPROVAL_URL_UNAVAILABLE.value
    # Truthful: the order EXISTS and is named, so a human can act on it.
    assert detail["execution"]["paypal_order_id"] == ORDER_ID
    assert detail["execution"]["status"] == "ORDER_CREATED"
    # No second create-order, and nothing was repaired.
    assert len(order_calls(paypal_http)) == 1
    assert external_purchase_service.get_by_approval(
        approval_id
    ).paypal_approve_url is None


@pytest.mark.parametrize(
    "recovered",
    [
        {"approve_url": None},
        {"approve_url": ""},
        {"approve_url": "   "},
        {},
        {"links": [], "approve_url": None},
    ],
)
def test_no_usable_link_from_paypal_is_also_a_structured_refusal(
    adopted, paypal_http, recovery_transport, recovered
):
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()
    recovery_transport.result = recovered

    replay = post_create_order(approval_id)

    assert replay.status_code == 409
    assert (
        replay.json()["detail"]["code"]
        == ExternalOrderConflict.APPROVAL_URL_UNAVAILABLE.value
    )
    assert len(order_calls(paypal_http)) == 1


def test_a_failed_recovery_never_fabricates_a_checkout_url(
    adopted, paypal_http, recovery_transport
):
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()
    recovery_transport.raises = paypal.PayPalGetOrderError("down")

    body = post_create_order(approval_id).text

    # No checkout-looking URL anywhere in the refusal.
    for fragment in ("checkoutnow", "paypal.com/checkout", "approve_url\": \"http"):
        assert fragment not in body
    assert "https://www.sandbox.paypal.com" not in body


def test_recovery_failure_is_retryable_once_paypal_recovers(
    adopted, paypal_http, recovery_transport
):
    """The refusal is not terminal state — nothing was burned."""
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    recovery_transport.arm()

    recovery_transport.raises = paypal.PayPalGetOrderError("transient")
    assert post_create_order(approval_id).status_code == 409

    recovery_transport.raises = None
    recovery_transport.result = {"approve_url": REAL_APPROVE_URL}
    retried = post_create_order(approval_id)

    assert retried.status_code == 200
    assert retried.json()["approve_url"] == REAL_APPROVE_URL
    assert len(order_calls(paypal_http)) == 1  # still never a second order


# ===========================================================================
# C5. The at-most-once create guarantee and 11E's rules are intact
# ===========================================================================


def test_a_failed_execution_is_still_terminal_and_never_recovered(
    adopted, paypal_http
):
    """Recovery applies to ORDER_CREATED only; FAILED stays FAILED."""
    paypal_http.state.order_status = 500
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    assert post_create_order(approval.approval_id).status_code == 502
    rows = execution_rows()
    assert len(rows) == 1 and rows[0].status is ExternalOrderStatus.FAILED
    assert rows[0].paypal_approve_url is None

    paypal_http.state.order_status = 201  # PayPal is healthy again
    replay = post_create_order(approval.approval_id)

    assert replay.status_code == 409
    assert (
        replay.json()["detail"]["code"]
        == ExternalOrderConflict.EXECUTION_ALREADY_FAILED.value
    )
    assert len(order_calls(paypal_http)) == 1  # the failed attempt only


def test_a_broken_checkout_url_config_does_not_burn_the_approval(
    adopted, paypal_http, monkeypatch
):
    """A deployment typo must be fixable, not terminal.

    The config is validated BEFORE the reservation is written, so no row is
    left behind to block the approval once the variable is corrected.
    """
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)

    monkeypatch.setenv(paypal.RETURN_URL_ENV, "javascript:alert(1)")
    refused = post_create_order(approval.approval_id)

    assert refused.status_code == 500
    assert execution_rows() == []          # no reservation left behind
    assert order_calls(paypal_http) == []  # PayPal never contacted

    monkeypatch.delenv(paypal.RETURN_URL_ENV, raising=False)
    fixed = post_create_order(approval.approval_id)

    assert fixed.status_code == 200
    assert fixed.json()["created"] is True
    assert len(order_calls(paypal_http)) == 1


def test_money_provenance_is_unchanged_by_this_phase(adopted, paypal_http):
    approval_id, body = create_external_order(adopted)

    # The approved provider-observed price, exactly as Phase 11E derived it.
    assert body["execution"]["paypal_amount"] == "19.99"
    assert body["execution"]["paypal_currency"] == "USD"
    sent = order_calls(paypal_http)[0]["json"]["purchase_units"][0]["amount"]
    assert sent == {"currency_code": "USD", "value": "19.99"}
    assert "revalidation" in body["observed_price_source"]


def test_the_service_still_has_no_refund_capability():
    """11F-D gave this module capture_order. It must never gain a refund.

    capture_purchase is the CATALOG capture service: external orders must
    never route through it, whatever state they reach.
    """
    for forbidden in ("refund_capture", "refund_transaction", "capture_purchase"):
        assert not hasattr(external_purchase_service, forbidden), forbidden


# ===========================================================================
# C6. Migration: a database that predates the column
# ===========================================================================


def test_a_pre_phase_table_gains_the_column_and_reads_as_null(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(db))

    # Build the table exactly as it existed before Phase 11F-B.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE external_purchase_orders (
                   execution_id        TEXT PRIMARY KEY,
                   approval_id         TEXT NOT NULL UNIQUE,
                   adopted_product_id  TEXT NOT NULL,
                   revalidation_id     TEXT NOT NULL,
                   paypal_order_id     TEXT UNIQUE,
                   paypal_amount       TEXT,
                   paypal_currency     TEXT,
                   status              TEXT NOT NULL,
                   created_at          TEXT NOT NULL,
                   updated_at          TEXT NOT NULL
               )"""
        )
        conn.execute(
            "INSERT INTO external_purchase_orders VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (
                "xord_legacy",
                "xapr_legacy",
                "adopt_legacy",
                "reval_legacy",
                "LEGACYORDER",
                "19.99",
                "USD",
                "ORDER_CREATED",
                "2026-10-01T00:00:00+00:00",
                "2026-10-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    # Reading through the service migrates the table additively.
    record = external_purchase_service.get_by_approval("xapr_legacy")

    assert record is not None
    assert record.paypal_approve_url is None       # the recoverable state
    assert record.paypal_order_id == "LEGACYORDER"  # untouched
    assert record.paypal_amount == "19.99"
    assert record.status is ExternalOrderStatus.ORDER_CREATED

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(external_purchase_orders)")
        }
    assert "paypal_approve_url" in columns


def test_migration_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPILOT_DB_PATH", str(tmp_path / "repeat.db"))
    for _ in range(3):
        assert external_purchase_service.count() == 0


# ===========================================================================
# D. GET /discovery/approvals/{approval_id}/order
# ===========================================================================


def test_the_get_returns_the_execution_record(adopted, paypal_http):
    approval_id, created = create_external_order(adopted)

    resp = get_order_view(approval_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["execution"]["approval_id"] == approval_id
    assert body["execution"]["status"] == "ORDER_CREATED"
    assert body["execution"]["paypal_order_id"] == ORDER_ID
    assert body["execution"]["paypal_amount"] == "19.99"
    assert body["execution"]["paypal_currency"] == "USD"
    assert body["approve_url"] == REAL_APPROVE_URL
    assert body["captured"] is False


def test_the_get_is_read_only(adopted, paypal_http, no_paypal_at_all):
    approval_id, _ = create_external_order(adopted)
    before = external_purchase_service.get_by_approval(approval_id).model_dump()
    rows_before = len(execution_rows())
    no_paypal_at_all.arm()

    for _ in range(3):
        assert get_order_view(approval_id).status_code == 200

    assert external_purchase_service.get_by_approval(approval_id).model_dump() == before
    assert len(execution_rows()) == rows_before
    assert no_paypal_at_all.hits == []  # not one PayPal call


def test_the_get_does_not_recover_a_missing_url(
    adopted, paypal_http, no_paypal_at_all
):
    """Recovery needs a PayPal call and a write; a GET does neither."""
    approval_id, _ = create_external_order(adopted)
    execution = external_purchase_service.get_by_approval(approval_id)
    clear_stored_url(execution.execution_id)
    no_paypal_at_all.arm()

    resp = get_order_view(approval_id)

    assert resp.status_code == 200
    assert resp.json()["approve_url"] is None
    assert resp.json()["execution"]["paypal_order_id"] == ORDER_ID
    assert no_paypal_at_all.hits == []
    assert external_purchase_service.get_by_approval(
        approval_id
    ).paypal_approve_url is None


def test_unknown_approval_is_404(no_paypal_at_all):
    no_paypal_at_all.arm()
    resp = get_order_view("xapr_does_not_exist")
    assert resp.status_code == 404
    assert no_paypal_at_all.hits == []


def test_an_approval_with_no_execution_is_404(adopted, paypal_http, no_paypal_at_all):
    """The approval exists; no create-order has been attempted for it."""
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    no_paypal_at_all.arm()

    resp = get_order_view(approval.approval_id)

    assert resp.status_code == 404
    assert execution_rows() == []  # asking did not create one
    assert no_paypal_at_all.hits == []


def test_the_get_never_creates_an_execution(adopted, paypal_http, no_paypal_at_all):
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    no_paypal_at_all.arm()

    for _ in range(5):
        get_order_view(approval.approval_id)

    assert external_purchase_service.count() == 0


def test_a_creating_reservation_reads_back_with_no_url(no_paypal_at_all):
    no_paypal_at_all.arm()
    now = "2026-10-09T12:00:00+00:00"
    reservation, _ = external_purchase_service._reserve(
        SimpleNamespace(
            approval_id="xapr_creating",
            adopted_product_id="adopt_x",
            revalidation_id="reval_x",
        ),
        now,
    )

    body = get_order_view("xapr_creating").json()

    assert body["execution"]["status"] == "CREATING"
    assert body["execution"]["paypal_order_id"] is None
    assert body["approve_url"] is None
    assert body["captured"] is False
    assert reservation.paypal_approve_url is None


def test_a_failed_execution_reads_back_with_no_url(adopted, paypal_http):
    paypal_http.state.order_status = 500
    require_approval()
    snapshot(adopted.adopted_product_id)
    approval = approved_approval(adopted.adopted_product_id)
    post_create_order(approval.approval_id)

    body = get_order_view(approval.approval_id).json()

    assert body["execution"]["status"] == "FAILED"
    assert body["approve_url"] is None
    assert body["captured"] is False


@pytest.mark.parametrize(
    "bad_id",
    [
        "' OR '1'='1",
        "'; DROP TABLE external_purchase_orders;--",
        "' UNION SELECT * FROM transactions--",
        "%20",
        "A" * 4096,
        "{{7*7}}",
        "../../etc/passwd",
    ],
)
def test_malicious_approval_ids_cannot_read_or_damage_anything(
    bad_id, adopted, paypal_http, no_paypal_at_all
):
    approval_id, _ = create_external_order(adopted)
    before = external_purchase_service.get_by_approval(approval_id).model_dump()
    no_paypal_at_all.arm()

    resp = client.get(f"/discovery/approvals/{bad_id}/order")

    assert resp.status_code in (404, 422), resp.text
    # The table survived and the real record is untouched.
    assert external_purchase_service.get_by_approval(approval_id).model_dump() == before
    assert no_paypal_at_all.hits == []


def test_the_get_exposes_no_credentials_or_provider_payload(adopted, paypal_http):
    approval_id, _ = create_external_order(adopted)

    resp = get_order_view(approval_id)

    text = resp.text.lower()
    for secret in (
        "client_secret",
        "access_token",
        "fake-token",
        "bearer",
        "client_id",
        "authorization",
    ):
        assert secret not in text
    # Only the documented fields — no raw provider payload rode along.
    assert set(resp.json()) == {"execution", "approve_url", "captured"}
    assert set(resp.json()["execution"]) == {
        "execution_id",
        "approval_id",
        "adopted_product_id",
        "revalidation_id",
        "paypal_order_id",
        "paypal_amount",
        "paypal_currency",
        "paypal_approve_url",
        # Phase 11F-D capture columns. Audit values only; the read endpoint
        # still exposes no credential and no raw provider payload.
        "capture_authorized_at",
        "paypal_capture_id",
        "paypal_capture_status",
        "captured_amount",
        "captured_currency",
        "paypal_request_id",  # Phase 11F-K.22a idempotency key (not a secret)
        "recovery_claimed_at",  # Phase 11F-K.22b replay claim (not a secret)
        "recovery_claim_token",  # Phase 11F-K.22b ownership guard (not a secret)
        "status",
        "created_at",
        "updated_at",
    }


def test_the_stored_url_is_not_exposed_through_unrelated_endpoints(
    adopted, paypal_http
):
    """Only the create-order result and this GET may carry the link."""
    approval_id, _ = create_external_order(adopted)

    for path in (
        f"/discovery/approvals/{approval_id}/resolve",
        "/transactions",
        "/products",
    ):
        resp = (
            client.post(path, json={"approve": True})
            if path.endswith("/resolve")
            else client.get(path)
        )
        assert REAL_APPROVE_URL not in resp.text, path


def test_the_get_adds_no_refund_field_and_reports_no_capture(
    adopted, paypal_http
):
    """A freshly created order has no capture, and never a refund field."""
    approval_id, _ = create_external_order(adopted)
    body = get_order_view(approval_id).json()
    text = json.dumps(body).lower()

    for forbidden in ("refund_id", "refunded", "transaction_id"):
        assert forbidden not in text
    assert body["captured"] is False
    # The capture columns exist since 11F-D but are empty for an order that
    # was only created — creating captures nothing.
    assert body["execution"]["paypal_capture_id"] is None
    assert body["execution"]["paypal_capture_status"] is None
    assert body["execution"]["captured_amount"] is None
    assert body["execution"]["capture_authorized_at"] is None
