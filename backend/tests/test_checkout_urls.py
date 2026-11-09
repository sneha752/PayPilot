"""Checkout URL configuration and PayPal link exposure (Phase 11F-B, A + B).

WHAT THIS PHASE CHANGED
    The buyer's return and cancel destinations were literals inside
    create_order. They are now read from PAYPAL_RETURN_URL and
    PAYPAL_CANCEL_URL, validated on every read, and defaulted to the exact
    pre-11F-B literals so an unconfigured deployment is unchanged.

    get_order gained two ADDITIVE keys -- "links" and "approve_url" --
    parsed defensively from PayPal's own response.

THE PROPERTIES DEFENDED HERE
    1. DEFAULTS ARE BYTE-IDENTICAL to the old behavior when both variables
       are absent, empty or whitespace.
    2. ONLY AN ABSOLUTE https URL IS ACCEPTED. javascript:, data:, http:,
       relative paths, scheme-relative //host, host-less URLs and malformed
       input are each refused, and refusal happens BEFORE any PayPal call --
       both transports are trapped, so a leak fails the test.
    3. NO CLIENT CAN SUPPLY THESE URLS. There is no parameter to pass one
       to; the injection tests post them as bodies and query strings and
       assert the exact bytes PayPal receives are still the server's.
    4. A MALFORMED LINKS ARRAY NEVER RAISES. The pre-11F-B code indexed
       link["href"] and would KeyError on a link without one.
    5. NO URL IS EVER BUILT FROM AN ORDER ID. If PayPal sends no usable
       link, the answer is None.
    6. EVERY PRE-EXISTING get_order KEY IS UNCHANGED.

HTTP to PayPal is fully mocked throughout; nothing here contacts PayPal.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from app import paypal
from app.main import app
from app.services import purchase_execution_service

client = TestClient(app, raise_server_exceptions=False)

RETURN_ENV = paypal.RETURN_URL_ENV
CANCEL_ENV = paypal.CANCEL_URL_ENV

#: The literals create_order used before Phase 11F-B. The defaults must stay
#: exactly these, or an unconfigured deployment silently changes behavior.
LEGACY_RETURN = "https://example.com/paypilot/return"
LEGACY_CANCEL = "https://example.com/paypilot/cancel"


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


TOKEN_OK = FakeResponse(200, {"access_token": "fake-token", "token_type": "Bearer"})

ORDER_RESPONSE = {
    "id": "ORDER123",
    "status": "CREATED",
    "links": [
        {
            "rel": "self",
            "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER123",
            "method": "GET",
        },
        {
            "rel": "approve",
            "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123",
            "method": "GET",
        },
    ],
}


@pytest.fixture(autouse=True)
def unconfigured(monkeypatch):
    """No checkout URL configured unless a test sets one.

    Pinned per test rather than in conftest so the developer's own .env can
    never decide what these tests measure.
    """
    monkeypatch.delenv(RETURN_ENV, raising=False)
    monkeypatch.delenv(CANCEL_ENV, raising=False)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")


@pytest.fixture
def paypal_http(monkeypatch):
    """Record every PayPal request; route token + create-order to fakes."""
    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        if url.endswith("/v2/checkout/orders"):
            return FakeResponse(201, ORDER_RESPONSE)
        raise AssertionError(f"Unexpected PayPal URL called: {url}")

    monkeypatch.setattr(paypal.httpx, "post", fake_post)
    return calls


@pytest.fixture
def no_paypal(monkeypatch):
    """Trap BOTH PayPal transports: a refusal must reach neither."""
    hits: list[str] = []

    def forbidden(name):
        def _raise(*args, **kwargs):
            hits.append(name)
            raise AssertionError(f"PayPal must not be contacted ({name})")

        return _raise

    monkeypatch.setattr(paypal.httpx, "post", forbidden("post"))
    monkeypatch.setattr(paypal.httpx, "get", forbidden("get"))
    return hits


def order_context(calls) -> dict:
    body = next(c for c in calls if c["url"].endswith("/v2/checkout/orders"))["json"]
    return body["application_context"]


# ===========================================================================
# A1. Defaults when both variables are absent
# ===========================================================================


def test_defaults_match_the_pre_phase_literals():
    assert paypal.DEFAULT_RETURN_URL == LEGACY_RETURN
    assert paypal.DEFAULT_CANCEL_URL == LEGACY_CANCEL
    assert paypal.checkout_urls() == (LEGACY_RETURN, LEGACY_CANCEL)


def test_create_order_sends_the_defaults_when_unconfigured(paypal_http):
    paypal.create_order(amount="10.00", currency="USD")

    ctx = order_context(paypal_http)
    assert ctx["return_url"] == LEGACY_RETURN
    assert ctx["cancel_url"] == LEGACY_CANCEL
    # And the rest of the checkout context is untouched.
    assert ctx["brand_name"] == "PayPilot"
    assert ctx["shipping_preference"] == "NO_SHIPPING"
    assert ctx["user_action"] == "PAY_NOW"


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_blank_configuration_falls_back_to_the_defaults(monkeypatch, blank):
    monkeypatch.setenv(RETURN_ENV, blank)
    monkeypatch.setenv(CANCEL_ENV, blank)
    assert paypal.checkout_urls() == (LEGACY_RETURN, LEGACY_CANCEL)


# ===========================================================================
# A2. Valid configured HTTPS URLs
# ===========================================================================


VALID_URLS = [
    "https://paypilot.example.com/return",
    "https://paypilot.example.com/paypal/return?flow=catalog",
    "https://localhost:5173/return",
    "https://sub.domain.example.co.uk/a/b/c#frag",
    "https://example.com",
    "HTTPS://Example.COM/Return",  # scheme comparison is case-insensitive
]


@pytest.mark.parametrize("url", VALID_URLS)
def test_valid_https_urls_are_accepted(monkeypatch, url):
    monkeypatch.setenv(RETURN_ENV, url)
    monkeypatch.setenv(CANCEL_ENV, url)
    assert paypal.checkout_urls() == (url, url)


def test_configured_urls_reach_paypal_verbatim(monkeypatch, paypal_http):
    monkeypatch.setenv(RETURN_ENV, "https://app.example.com/paypal/return")
    monkeypatch.setenv(CANCEL_ENV, "https://app.example.com/paypal/cancel")

    paypal.create_order(amount="10.00", currency="USD")

    ctx = order_context(paypal_http)
    assert ctx["return_url"] == "https://app.example.com/paypal/return"
    assert ctx["cancel_url"] == "https://app.example.com/paypal/cancel"


def test_each_variable_is_independent(monkeypatch, paypal_http):
    monkeypatch.setenv(RETURN_ENV, "https://app.example.com/only-return")

    paypal.create_order(amount="10.00", currency="USD")

    ctx = order_context(paypal_http)
    assert ctx["return_url"] == "https://app.example.com/only-return"
    assert ctx["cancel_url"] == LEGACY_CANCEL  # still the default


def test_configuration_is_read_per_call_not_cached(monkeypatch, paypal_http):
    monkeypatch.setenv(RETURN_ENV, "https://first.example.com/return")
    paypal.create_order(amount="10.00", currency="USD")
    monkeypatch.setenv(RETURN_ENV, "https://second.example.com/return")
    paypal.create_order(amount="10.00", currency="USD")

    contexts = [
        c["json"]["application_context"]
        for c in paypal_http
        if c["url"].endswith("/v2/checkout/orders")
    ]
    assert contexts[0]["return_url"] == "https://first.example.com/return"
    assert contexts[1]["return_url"] == "https://second.example.com/return"


# ===========================================================================
# A3. Rejection: malformed, relative, non-HTTPS, javascript:
# ===========================================================================


REJECTED_URLS = [
    # --- scheme is a redirect primitive, not a destination ---
    "javascript:alert(1)",
    "JavaScript:alert(document.domain)",
    "javascript:void(fetch('https://attacker.example'))",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "intent://scan/#Intent;scheme=zxing;end",
    # --- not HTTPS ---
    "http://example.com/return",
    "HTTP://example.com/return",
    "ftp://example.com/return",
    "ws://example.com/return",
    # --- relative / scheme-less ---
    "/paypilot/return",
    "paypilot/return",
    "./return",
    "../return",
    "example.com/return",
    "//evil.example.com/return",  # scheme-relative: no scheme at all
    # --- absolute https but no host ---
    "https:///no-host",
    "https://",
    # --- junk and embedded whitespace ---
    "not a url at all",
    "https:// space in host",
    "https://example.com/a b",
]

#: Control characters embedded INSIDE a URL. Tested against the validator
#: directly rather than through the environment, because a Windows
#: environment variable is a null-terminated string: setting one to a value
#: containing chr(0) silently truncates it before os.getenv ever sees it.
#: Built with chr() so no literal control byte lands in this file's source.
CONTROL_CHARACTER_URLS = [
    "https://example.com/" + chr(13) + chr(10) + "X-Injected: 1",
    "https://example.com/" + chr(10) + "X-Injected: 1",
    "https://example.com/" + chr(0),
    "https://example.com/" + chr(7) + "bell",
    "https://example.com/" + chr(9) + "tab",
    "https://exam" + chr(32) + "ple.com/",
    "https://example.com/" + chr(127),
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_invalid_return_url_is_rejected(monkeypatch, url, no_paypal):
    monkeypatch.setenv(RETURN_ENV, url)
    with pytest.raises(paypal.PayPalCheckoutConfigError):
        paypal.checkout_urls()
    assert no_paypal == []


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_invalid_cancel_url_is_rejected(monkeypatch, url, no_paypal):
    monkeypatch.setenv(CANCEL_ENV, url)
    with pytest.raises(paypal.PayPalCheckoutConfigError):
        paypal.checkout_urls()
    assert no_paypal == []


@pytest.mark.parametrize("url", CONTROL_CHARACTER_URLS)
def test_embedded_control_characters_are_rejected(url, no_paypal):
    """A raw CR/LF in an outbound value is a header-injection shape.

    Checked against the validator directly: see CONTROL_CHARACTER_URLS for
    why the environment cannot carry these faithfully on Windows.
    """
    with pytest.raises(paypal.PayPalCheckoutConfigError):
        paypal._validate_checkout_url(RETURN_ENV, url)
    assert no_paypal == []


@pytest.mark.parametrize(
    "configured,expected",
    [
        ("  https://example.com/return  ", "https://example.com/return"),
        (chr(9) + "https://example.com/return", "https://example.com/return"),
        ("https://example.com/return" + chr(10), "https://example.com/return"),
    ],
)
def test_surrounding_whitespace_is_trimmed_not_rejected(
    monkeypatch, configured, expected
):
    """Trimming the edges is deliberate — a trailing newline in a .env file
    is a formatting artefact, not a different destination. Only whitespace
    INSIDE the URL is a refusal (see the test above)."""
    monkeypatch.setenv(RETURN_ENV, configured)
    assert paypal.checkout_urls()[0] == expected


@pytest.mark.parametrize("url", REJECTED_URLS[:8])
def test_create_order_refuses_before_contacting_paypal(monkeypatch, url, no_paypal):
    """The refusal must precede the TOKEN call, not just the order call."""
    monkeypatch.setenv(RETURN_ENV, url)
    with pytest.raises(paypal.PayPalCheckoutConfigError):
        paypal.create_order(amount="10.00", currency="USD")
    assert no_paypal == []


def test_a_checkout_config_error_is_not_a_paypal_order_error():
    """Deliberate: a config fault must not look like an upstream failure.

    If it subclassed PayPalOrderError the external purchase service would
    mark an approval's single execution slot permanently FAILED over a
    fixable typo, and the API would report a 502 "PayPal is down".
    """
    assert not issubclass(
        paypal.PayPalCheckoutConfigError, paypal.PayPalOrderError
    )
    assert not issubclass(paypal.PayPalCheckoutConfigError, paypal.PayPalAuthError)


def test_the_error_names_the_variable_but_never_echoes_the_value(monkeypatch):
    """A mis-pasted secret is the likeliest way a credential lands here."""
    secret = "AcTuAlSeCrEtVaLuE-not-a-url"
    monkeypatch.setenv(RETURN_ENV, secret)
    with pytest.raises(paypal.PayPalCheckoutConfigError) as exc:
        paypal.checkout_urls()
    message = str(exc.value)
    assert RETURN_ENV in message
    assert secret not in message
    for leak in ("test-client-secret", "test-client-id", "fake-token"):
        assert leak not in message


def test_misconfiguration_surfaces_as_500_not_502(monkeypatch, no_paypal):
    monkeypatch.setenv(RETURN_ENV, "javascript:alert(1)")
    resp = client.post(
        "/paypal/create-order", json={"amount": "10.00", "currency": "USD"}
    )
    assert resp.status_code == 500
    assert no_paypal == []
    body = resp.text.lower()
    for leak in ("test-client-secret", "test-client-id", "fake-token", "bearer"):
        assert leak not in body


# ===========================================================================
# A4. No client can supply a return or cancel URL
# ===========================================================================


#: Every spelling a client might try, in a body or a query string.
INJECTED_URLS = {
    "return_url": "https://attacker.example/steal",
    "returnUrl": "https://attacker.example/steal",
    "cancel_url": "https://attacker.example/cancel",
    "cancelUrl": "https://attacker.example/cancel",
    "redirect_uri": "https://attacker.example/redirect",
    "application_context": {
        "return_url": "https://attacker.example/ctx",
        "cancel_url": "https://attacker.example/ctx-cancel",
        "brand_name": "Attacker",
    },
    "PAYPAL_RETURN_URL": "https://attacker.example/env",
    "PAYPAL_CANCEL_URL": "https://attacker.example/env",
}


def test_create_order_endpoint_ignores_injected_urls(paypal_http):
    resp = client.post(
        "/paypal/create-order",
        json={"amount": "10.00", "currency": "USD", **INJECTED_URLS},
    )
    assert resp.status_code == 200

    ctx = order_context(paypal_http)
    assert ctx["return_url"] == LEGACY_RETURN
    assert ctx["cancel_url"] == LEGACY_CANCEL
    assert ctx["brand_name"] == "PayPilot"
    assert "attacker.example" not in json.dumps(paypal_http)


def test_purchase_execute_ignores_injected_urls(paypal_http):
    resp = client.post(
        "/purchase/execute",
        json={
            "product_id": "TAB003",
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 50000,
            },
            **INJECTED_URLS,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "order_created"

    ctx = order_context(paypal_http)
    assert ctx["return_url"] == LEGACY_RETURN
    assert ctx["cancel_url"] == LEGACY_CANCEL
    assert "attacker.example" not in json.dumps(paypal_http)


@pytest.mark.parametrize("field", sorted(INJECTED_URLS))
def test_no_single_injected_field_changes_the_checkout_urls(field, paypal_http):
    client.post(
        "/paypal/create-order",
        json={"amount": "10.00", "currency": "USD", field: INJECTED_URLS[field]},
    )
    ctx = order_context(paypal_http)
    assert ctx["return_url"] == LEGACY_RETURN
    assert ctx["cancel_url"] == LEGACY_CANCEL


def test_query_string_urls_are_ignored(paypal_http):
    client.post(
        "/paypal/create-order?return_url=https://attacker.example/q"
        "&cancel_url=https://attacker.example/q2",
        json={"amount": "10.00", "currency": "USD"},
    )
    ctx = order_context(paypal_http)
    assert ctx["return_url"] == LEGACY_RETURN
    assert ctx["cancel_url"] == LEGACY_CANCEL


def test_create_order_has_no_url_parameter_to_pass_one_to():
    """Structural: there is nowhere for a URL to land in the signature.

    Phase 11F-K.22a added `request_id` (a stable PayPal-Request-Id idempotency
    key) — never a URL, amount, currency, or any money/checkout value.
    """
    import inspect

    params = set(inspect.signature(paypal.create_order).parameters)
    assert params == {"amount", "currency", "description", "request_id"}
    # No URL / return / cancel / redirect parameter may ever land here.
    for forbidden in ("url", "return_url", "cancel_url", "redirect"):
        assert forbidden not in params


# ===========================================================================
# A5. The existing catalog flow is unchanged
# ===========================================================================


def test_catalog_order_creation_is_byte_identical_when_unconfigured(paypal_http):
    result = paypal.create_order(
        amount="766.56", currency="USD", description="HP Pavilion 14 (LAP002)"
    )
    body = next(
        c for c in paypal_http if c["url"].endswith("/v2/checkout/orders")
    )["json"]

    assert body == {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "amount": {"currency_code": "USD", "value": "766.56"},
                "description": "HP Pavilion 14 (LAP002)",
            }
        ],
        "application_context": {
            "brand_name": "PayPilot",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
            "return_url": LEGACY_RETURN,
            "cancel_url": LEGACY_CANCEL,
        },
    }
    assert result == {
        "order_id": "ORDER123",
        "status": "CREATED",
        "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123",
    }


def test_purchase_execute_still_creates_an_order(monkeypatch):
    calls: list[dict] = []

    def fake_create_order(amount, currency="USD", description=None, request_id=None):
        calls.append({"amount": amount, "currency": currency})
        return {
            "order_id": "ORDER-COMPAT",
            "status": "CREATED",
            "approve_url": "https://www.sandbox.paypal.com/checkoutnow?token=X",
        }

    monkeypatch.setattr(purchase_execution_service, "create_order", fake_create_order)

    resp = client.post(
        "/purchase/execute",
        json={
            "product_id": "TAB003",
            "authorization": {
                "purchase_requested": True,
                "autonomous_purchase_allowed": True,
                "auto_approve_below_inr": 50000,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "order_created"
    assert body["paypal_amount"] == "498.89"  # unchanged derivation
    assert calls == [{"amount": "498.89", "currency": "USD"}]


# ===========================================================================
# B. get_order exposes links safely and keeps every existing field
# ===========================================================================


GET_ORDER_PAYLOAD = {
    "id": "ORDER123",
    "status": "CREATED",
    "intent": "CAPTURE",
    "purchase_units": [
        {
            "amount": {"currency_code": "USD", "value": "16.66"},
            "payments": {
                "captures": [
                    {
                        "id": "CAP123",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "16.66"},
                    }
                ]
            },
        }
    ],
    "links": [
        {
            "rel": "self",
            "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER123",
            "method": "GET",
        },
        {
            "rel": "approve",
            "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123",
            "method": "GET",
        },
    ],
}


def mock_get_order(monkeypatch, payload, status_code: int = 200):
    monkeypatch.setattr(paypal.httpx, "post", lambda url, **kw: TOKEN_OK)
    monkeypatch.setattr(
        paypal.httpx, "get", lambda url, **kw: FakeResponse(status_code, payload)
    )


def test_get_order_preserves_every_previous_field(monkeypatch):
    mock_get_order(monkeypatch, GET_ORDER_PAYLOAD)
    result = paypal.get_order("ORDER123")

    assert result["order_id"] == "ORDER123"
    assert result["order_status"] == "CREATED"
    assert result["intent"] == "CAPTURE"
    assert result["purchase_units"] == GET_ORDER_PAYLOAD["purchase_units"]
    assert result["captures"] == [
        {
            "id": "CAP123",
            "status": "COMPLETED",
            "amount": {"currency_code": "USD", "value": "16.66"},
        }
    ]


def test_get_order_adds_links_additively(monkeypatch):
    mock_get_order(monkeypatch, GET_ORDER_PAYLOAD)
    result = paypal.get_order("ORDER123")

    assert result["approve_url"] == (
        "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123"
    )
    assert result["links"] == [
        {
            "rel": "self",
            "href": "https://api-m.sandbox.paypal.com/v2/checkout/orders/ORDER123",
            "method": "GET",
        },
        {
            "rel": "approve",
            "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER123",
            "method": "GET",
        },
    ]
    # Additive only: the previous key set is a strict subset of the new one.
    assert {
        "order_id",
        "order_status",
        "intent",
        "purchase_units",
        "captures",
    } < set(result)


def test_payer_action_is_recognised_as_an_approval_link(monkeypatch):
    payload = dict(GET_ORDER_PAYLOAD)
    payload["links"] = [
        {"rel": "payer-action", "href": "https://www.paypal.com/payer/act?t=1"}
    ]
    mock_get_order(monkeypatch, payload)
    assert paypal.get_order("ORDER123")["approve_url"] == (
        "https://www.paypal.com/payer/act?t=1"
    )


#: Every shape that used to risk a KeyError or a bad URL escaping.
MALFORMED_LINKS = [
    None,
    [],
    "not-a-list",
    {"rel": "approve"},                                  # dict, not a list
    [{"rel": "approve"}],                                # no href at all
    [{"rel": "approve", "href": None}],
    [{"rel": "approve", "href": ""}],
    [{"rel": "approve", "href": "   "}],
    [{"rel": "approve", "href": 12345}],
    [{"href": "https://www.paypal.com/x"}],              # no rel
    [{"rel": None, "href": "https://www.paypal.com/x"}],
    ["a string", 42, None],
    [[], {}],
    [{"rel": "approve", "href": "javascript:alert(1)"}],
    [{"rel": "approve", "href": "http://insecure.example/checkout"}],
    [{"rel": "approve", "href": "/relative/checkout"}],
    [{"rel": "approve", "href": "https:///no-host"}],
    [{"rel": "self", "href": "https://api-m.paypal.com/x"}],  # no approval rel
]


@pytest.mark.parametrize("links", MALFORMED_LINKS)
def test_malformed_links_never_raise_and_never_yield_a_url(monkeypatch, links):
    payload = dict(GET_ORDER_PAYLOAD)
    payload["links"] = links
    mock_get_order(monkeypatch, payload)

    result = paypal.get_order("ORDER123")  # must not raise

    assert result["approve_url"] is None
    assert isinstance(result["links"], list)
    # Every previous field still present and correct.
    assert result["order_id"] == "ORDER123"
    assert result["captures"][0]["id"] == "CAP123"


@pytest.mark.parametrize("links", MALFORMED_LINKS)
def test_create_order_survives_malformed_links_too(monkeypatch, links):
    """The old code indexed link["href"] and would KeyError here."""
    payload = {"id": "ORDER123", "status": "CREATED", "links": links}

    def fake_post(url, **kwargs):
        if url.endswith("/v1/oauth2/token"):
            return TOKEN_OK
        return FakeResponse(201, payload)

    monkeypatch.setattr(paypal.httpx, "post", fake_post)

    result = paypal.create_order(amount="10.00", currency="USD")
    assert result["order_id"] == "ORDER123"
    assert result["approve_url"] is None


def test_an_order_id_is_never_turned_into_a_url(monkeypatch):
    """No link in, no URL out — not even a plausible-looking one."""
    payload = {"id": "ORDER-SECRET-123", "status": "CREATED"}  # no links key
    mock_get_order(monkeypatch, payload)

    result = paypal.get_order("ORDER-SECRET-123")

    assert result["approve_url"] is None
    assert result["links"] == []
    assert "ORDER-SECRET-123" not in json.dumps(
        {"links": result["links"], "approve_url": result["approve_url"]}
    )


def test_links_are_reduced_to_three_fields(monkeypatch):
    """Unreviewed provider payload must not ride along inside a link."""
    payload = dict(GET_ORDER_PAYLOAD)
    payload["links"] = [
        {
            "rel": "approve",
            "href": "https://www.paypal.com/checkoutnow?token=X",
            "method": "GET",
            "encType": "application/json",
            "internal_debug": "do-not-forward",
            "authorization": "Bearer leaked",
        }
    ]
    mock_get_order(monkeypatch, payload)

    links = paypal.get_order("ORDER123")["links"]
    assert links == [
        {
            "rel": "approve",
            "href": "https://www.paypal.com/checkoutnow?token=X",
            "method": "GET",
        }
    ]
    assert "leaked" not in json.dumps(links)


def test_paypal_order_endpoint_exposes_links_without_credentials(monkeypatch):
    mock_get_order(monkeypatch, GET_ORDER_PAYLOAD)
    resp = client.get("/paypal/order/ORDER123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["order_id"] == "ORDER123"
    assert body["captures"][0]["id"] == "CAP123"
    assert body["approve_url"].startswith("https://")
    text = resp.text.lower()
    for secret in ("client_secret", "access_token", "fake-token", "bearer", "client_id"):
        assert secret not in text
