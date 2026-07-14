from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import broker
import config
import state


@pytest.fixture
def env(monkeypatch):
    values = {
        "GCP_PROJECT_ID": "project-1",
        "STRATEGY_ID": "STRATEGY",
        "SYMBOL": "smr",
        "FIX_C": "1500",
        "P0": "9",
        "DIFF": "30",
        "DNA_CODE": "bypass:10",
        "START_TIMESTAMP": "0",
        "WEBULL_APP_KEY": "app-key-value",
        "WEBULL_APP_SECRET": "app-secret-value",
        "WEBULL_ACCOUNT_ID": "account-id-value",
        "WEBULL_ENV": "uat",
    }
    for key in list(values):
        monkeypatch.setenv(key, values[key])
    return values


def test_app_config_defaults_and_normalization(env):
    loaded = config.load_app_config(use_cache=False)

    assert loaded.project_id == "project-1"
    assert loaded.strategy_id == "STRATEGY"
    assert loaded.symbol == "SMR"
    assert loaded.fix_c == 1500.0
    assert loaded.p0 == 9.0
    assert loaded.diff == 30.0
    assert loaded.dna_code == "bypass:10"
    assert loaded.firestore_state_document == "STRATEGY_SMR"


def test_broker_config_resolves_thailand_uat_and_redacts_secrets(env):
    loaded = config.load_broker_config("project-1", use_cache=False)

    assert loaded.region == "th"
    assert loaded.endpoint == "th-api.uat.webullbroker.com"
    # v3 is the default order API: the SDK's v2 order endpoints support only
    # Webull HK/US, while v3 explicitly supports Webull TH (this region).
    assert loaded.api_version == "v3"
    assert loaded.preview_orders is True
    assert loaded.safe_dict()["app_key"] == "***alue"
    assert loaded.safe_dict()["app_secret"] == "***"
    assert "app-secret-value" not in repr(loaded)


def test_startup_validation_preserves_check_names(env):
    checks = config.validate_startup()
    assert set(checks) == {
        "app_config",
        "webull_app_key",
        "webull_app_secret",
        "webull_account_id",
        "webull_endpoint",
        "webull_api_version",
        "webull_region",
        "webull_trading_session",
        "webull_preview",
    }
    assert all(value.startswith("ok") for value in checks.values())


def test_broker_config_requires_explicit_environment(env, monkeypatch):
    monkeypatch.delenv("WEBULL_ENV")

    with pytest.raises(ValueError, match="WEBULL_ENV"):
        config.load_broker_config("project-1", use_cache=False)


def test_broker_config_rejects_non_official_endpoint(env, monkeypatch):
    monkeypatch.setenv("WEBULL_TRADING_ENDPOINT", "example.invalid")

    with pytest.raises(ValueError, match="official endpoint"):
        config.load_broker_config("project-1", use_cache=False)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("WEBULL_REGION", "us", "WEBULL_REGION"),
        ("WEBULL_API_VERSION", "v2", "WEBULL_API_VERSION"),
        ("WEBULL_SUPPORT_TRADING_SESSION", "INVALID", "TRADING_SESSION"),
        ("WEBULL_PREVIEW_ORDERS", "false", "PREVIEW_ORDERS"),
    ],
)
def test_broker_config_fails_closed_on_non_manual_routing(
    env, monkeypatch, name, value, message
):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        config.load_broker_config("project-1", use_cache=False)


@pytest.mark.parametrize("session", ["CORE", "NIGHT", "ALL", "ALL_DAY"])
def test_broker_config_accepts_exact_official_sessions(env, monkeypatch, session):
    monkeypatch.setenv("WEBULL_SUPPORT_TRADING_SESSION", session)
    loaded = config.load_broker_config("project-1", use_cache=False)
    assert loaded.support_trading_session == session


@pytest.mark.parametrize("session", ["PRE", "AFTER", "OVERNIGHT"])
def test_broker_config_rejects_legacy_sessions(env, monkeypatch, session):
    monkeypatch.setenv("WEBULL_SUPPORT_TRADING_SESSION", session)
    with pytest.raises(ValueError, match="TRADING_SESSION"):
        config.load_broker_config("project-1", use_cache=False)


def test_get_config_never_returns_raw_credentials(env, monkeypatch):
    monkeypatch.setattr(config, "_cached_app_config", None)
    monkeypatch.setattr(config, "_cached_broker_config", None)

    loaded = config.get_config()

    serialized = repr(loaded)
    assert env["WEBULL_APP_KEY"] not in serialized
    assert env["WEBULL_APP_SECRET"] not in serialized
    assert env["WEBULL_ACCOUNT_ID"] not in serialized


def test_order_payload_contract_and_quantity_formatting():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.config = SimpleNamespace(support_trading_session="CORE")

    payload = instance._build_market_order_payload("SMR", "BUY", 1.23000, "coid")

    assert payload == [{
        "combo_type": "NORMAL",
        "client_order_id": "coid",
        "symbol": "SMR",
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": "1.23",
        "support_trading_session": "CORE",
        "side": "BUY",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }]


@pytest.mark.parametrize("quantity", [0, -1, float("inf"), float("nan")])
def test_invalid_order_quantities_are_rejected(quantity):
    with pytest.raises(broker.BrokerValidationError):
        broker._format_order_quantity(quantity)


def test_nested_webull_responses_are_parsed_without_changing_shape_assumptions():
    response = {"data": {"positions": [{"ticker": "SMR", "positionQty": "4.5"}]}}
    quote = {"data": [{"symbol": "SMR", "price": "11.25"}]}

    assert broker.WebullBroker._extract_quantity(response, "SMR") == 4.5
    assert broker.WebullBroker._extract_last_price(quote, "SMR") == 11.25
    assert broker.WebullBroker._extract_quantity(response, "AAPL") == 0.0


def test_core_quote_never_falls_back_to_stale_close():
    quote = [{"symbol": "SMR", "close": "11.25"}]
    assert broker.WebullBroker._extract_last_price(quote, "SMR", "CORE") == 0.0


def test_regional_nested_position_response_preserves_symbol_quantity_pair():
    response = {
        "data": [{
            "instrument": {"symbol": "SMR.US"},
            "position": {"positionQuantity": "10"},
        }]
    }

    assert broker.WebullBroker._extract_quantity(response, "SMR") == 10.0


def test_matching_position_without_quantity_fails_closed_instead_of_buying_from_zero():
    response = {"positions": [{"symbol": "SMR", "cost_price": "100"}]}

    with pytest.raises(broker.BrokerValidationError, match="position quantity"):
        broker.WebullBroker._extract_quantity(response, "SMR")


def test_comma_formatted_position_quantity_is_supported():
    response = [{"symbol": "SMR", "quantity": "1,234.5"}]
    assert broker.WebullBroker._extract_quantity(response, "SMR") == 1234.5


def test_broker_cache_reuses_same_config_and_rebuilds_for_changed_config():
    first_config = SimpleNamespace(name="first")
    second_config = SimpleNamespace(name="second")
    created = []

    def build(current_config):
        instance = SimpleNamespace(config=current_config)
        created.append(instance)
        return instance

    with patch.object(broker, "_cached_broker", None), patch.object(
        broker,
        "WebullBroker",
        side_effect=build,
    ):
        first = broker.get_broker(first_config)
        cached = broker.get_broker(first_config)
        second = broker.get_broker(second_config)

    assert first is cached
    assert second is not first
    assert len(created) == 2


def test_broker_initialization_authenticates_and_binds_configured_account():
    gateway = SimpleNamespace(
        get_account_list=Mock(return_value=[{"account_id": "acct-1"}])
    )
    broker_config = SimpleNamespace(account_id="acct-1")

    with patch("webull_api.WebullApiGateway", return_value=gateway) as factory:
        instance = broker.WebullBroker(broker_config)

    factory.assert_called_once_with(broker_config)
    gateway.get_account_list.assert_called_once_with()
    assert instance.gateway is gateway


def test_broker_initialization_rejects_account_not_owned_by_credentials():
    gateway = SimpleNamespace(
        get_account_list=Mock(return_value=[{"account_id": "different"}])
    )

    with patch("webull_api.WebullApiGateway", return_value=gateway):
        with pytest.raises(broker.BrokerValidationError, match="not present"):
            broker.WebullBroker(SimpleNamespace(account_id="acct-1"))


def test_retry_retries_5xx_but_not_4xx():
    transient_calls = 0

    def transient():
        nonlocal transient_calls
        transient_calls += 1
        if transient_calls == 1:
            raise broker.BrokerHTTPError(503, "down")
        return "ok"

    def permanent():
        raise broker.BrokerHTTPError(400, "bad")

    with patch("broker.time.sleep"):
        assert broker._retry()(transient)() == "ok"
    with pytest.raises(broker.BrokerHTTPError):
        broker._retry()(permanent)()

    assert transient_calls == 2


REPEAT_REQUEST_BODY = (
    "HTTP Status: 417, Code: OPENAPI_REPEAT_REQUEST, "
    "Msg: Please don't tap repeatedly., RequestID: abc"
)


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (504, "gateway timeout", True),        # 5xx upstream failure
        (500, "", True),
        (417, REPEAT_REQUEST_BODY, True),      # Webull repeat-request throttle
        (417, "some other 417 reason", False),  # unrelated 417 = permanent
        (429, "too many requests", True),       # idempotent read rate limit
        (400, "bad request", False),
    ],
)
def test_is_retryable_http_classifies_status_and_repeat_throttle(status_code, body, expected):
    assert broker._is_retryable_http(broker.BrokerHTTPError(status_code, body)) is expected


def test_retry_recovers_from_repeat_request_throttle():
    """A 417 OPENAPI_REPEAT_REQUEST on a read is backed off and retried."""
    calls = 0

    def throttled():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise broker.BrokerHTTPError(417, REPEAT_REQUEST_BODY)
        return "ok"

    with patch("broker.time.sleep"):
        assert broker._retry()(throttled)() == "ok"
    assert calls == 2


def test_retry_uses_longer_backoff_for_rate_limit():
    calls = 0

    def rate_limited():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise broker.BrokerHTTPError(429, "too many requests")
        return "ok"

    with (
        patch("broker.random.uniform", return_value=0.0),
        patch("broker.time.sleep") as sleep,
    ):
        assert broker._retry()(rate_limited)() == "ok"

    sleep.assert_called_once_with(broker.RATE_LIMIT_BASE_DELAY_SECONDS)
    assert calls == 2


def test_failed_position_read_retries_only_position_not_successful_quote():
    position = Mock(side_effect=[
        broker.BrokerHTTPError(504, "gateway timeout"),
        [{"symbol": "SMR", "quantity": "10"}],
    ])
    snapshot = Mock(return_value={"symbol": "SMR", "price": "150"})
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "account"
    instance.config = SimpleNamespace(support_trading_session="CORE")
    instance.gateway = SimpleNamespace(
        get_positions=position,
        get_quote=snapshot,
    )

    with patch("broker.time.sleep"):
        result = instance.get_position_and_price("SMR")

    assert result == broker.MarketState(quantity=10.0, last_price=150.0)
    assert position.call_count == 2
    assert snapshot.call_count == 1


def test_open_order_guard_uses_configured_th_order_api():
    open_orders = Mock(return_value=[{
        "client_order_id": "group-1",
        "orders": [{
            "symbol": "SMR",
            "instrument_type": "EQUITY",
            "status": "SUBMITTED",
        }],
    }])
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "account"
    instance.gateway = SimpleNamespace(get_open_orders=open_orders)

    assert instance.has_open_order("smr") is True
    assert instance.has_open_order("AAPL") is False
    assert open_orders.call_args.kwargs == {
        "page_size": 100,
        "last_client_order_id": None,
    }


def test_open_order_guard_paginates_beyond_first_100_groups():
    first_page = [
        {"client_order_id": f"group-{index}", "orders": []}
        for index in range(100)
    ]
    second_page = [{
        "client_order_id": "group-final",
        "orders": [{"symbol": "SMR", "instrument_type": "EQUITY"}],
    }]
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(
        get_open_orders=Mock(side_effect=[first_page, second_page])
    )

    assert instance.has_open_order("SMR") is True
    assert instance.gateway.get_open_orders.call_args_list[1].kwargs == {
        "page_size": 100,
        "last_client_order_id": "group-99",
    }


def test_open_order_guard_does_not_match_option_leg_symbol():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(get_open_orders=Mock(return_value=[{
        "client_order_id": "group-1",
        "orders": [{"symbol": "SMR", "instrument_type": "OPTION"}],
    }]))

    assert instance.has_open_order("SMR") is False


def test_open_order_guard_accepts_official_stock_response_alias():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(get_open_orders=Mock(return_value=[{
        "client_order_id": "group-stock",
        "orders": [{
            "client_order_id": "stock-order",
            "instrument_type": "STOCK",
            "symbol": "AAPL",
        }],
    }]))

    assert instance.has_open_order("AAPL") is True


def _order_detail_broker(detail_payload):
    correlated = {"client_order_id": "coid", **detail_payload}
    detail = Mock(return_value={"orders": [correlated]})
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "account"
    instance.gateway = SimpleNamespace(get_order_detail=detail)
    return instance, detail


def test_get_order_status_reads_filled_quantity_from_order_detail():
    """A filled order reports a positive filled quantity (Manual Test Lab check)."""
    instance, detail = _order_detail_broker(
        {"order_id": "order-1", "status": "FILLED", "filled_quantity": "0.10247"}
    )

    status = instance.get_order_status("coid")

    assert status.filled_quantity == 0.10247
    assert status.is_filled is True
    assert status.is_terminal_unfilled is False
    detail.assert_called_once_with("coid")


def test_get_order_status_flags_accepted_but_cancelled_order_as_unfilled():
    """The reported bug: accepted id, cancelled, nothing filled."""
    instance, _ = _order_detail_broker(
        {"order_id": "order-1", "status": "CANCELLED", "filled_quantity": "0"}
    )

    status = instance.get_order_status("coid")

    assert status.filled_quantity == 0.0
    assert status.is_filled is False
    assert status.is_terminal_unfilled is True


def test_get_order_status_keeps_partial_fill_nonterminal():
    instance, _ = _order_detail_broker(
        {
            "status": "PARTIAL_FILLED",
            "quantity": "1",
            "filledQty": "0.5",
        }
    )

    status = instance.get_order_status("coid")

    assert status.has_fill is True
    assert status.is_partial_fill is True
    assert status.is_filled is False
    assert status.is_terminal is False
    assert status.remaining_quantity == 0.5


def test_get_order_status_missing_fill_field_defaults_to_zero():
    instance, _ = _order_detail_broker({"status": "PENDING"})

    status = instance.get_order_status("coid")

    assert status.filled_quantity == 0.0
    assert status.is_filled is False
    # Pending is neither filled nor a terminal reject.
    assert status.is_terminal_unfilled is False


def test_get_order_status_requires_one_official_detail_group_object():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(get_order_detail=Mock(return_value=[{
        "client_order_id": "coid",
        "orders": [{"client_order_id": "coid", "status": "FILLED"}],
    }]))

    with pytest.raises(
        broker.BrokerValidationError,
        match="one group object",
    ):
        instance.get_order_status("coid")


def test_cancel_correlates_flat_response_and_confirms_detail():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(cancel_order=Mock(return_value={
        "client_order_id": "coid",
        "order_id": "order-1",
    }))
    instance.get_order_status = Mock(return_value=broker.OrderStatus(
        client_order_id="coid",
        status="CANCELLED",
        filled_quantity=0.0,
        raw_response={},
    ))

    result = instance.cancel_order("coid")

    assert result == {
        "client_order_id": "coid",
        "order_id": "order-1",
        "status": "CANCELLED",
        "filled_quantity": 0.0,
    }
    instance.gateway.cancel_order.assert_called_once_with("coid")
    instance.get_order_status.assert_called_once_with("coid")


def test_cancel_uses_history_when_detail_remains_stale():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(cancel_order=Mock(return_value={
        "client_order_id": "coid",
        "order_id": "order-1",
    }))
    stale = broker.OrderStatus(
        client_order_id="coid",
        status="SUBMITTED",
        filled_quantity=0.0,
        raw_response={},
    )
    cancelled = broker.OrderStatus(
        client_order_id="coid",
        status="CANCELLED",
        filled_quantity=0.0,
        raw_response={},
    )
    instance.get_order_status = Mock(return_value=stale)
    instance.lookup_order_status = Mock(return_value=cancelled)

    result = instance.cancel_order("coid")

    assert result["status"] == "CANCELLED"
    instance.gateway.cancel_order.assert_called_once_with("coid")
    instance.lookup_order_status.assert_called_once_with("coid")


def test_lookup_prefers_terminal_history_over_stale_detail():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.get_order_detail = Mock(return_value={
        "client_order_id": "coid",
        "orders": [{
            "client_order_id": "coid",
            "status": "SUBMITTED",
            "filled_quantity": "0",
            "total_quantity": "1",
        }],
    })
    instance.get_open_orders = Mock(return_value=[])
    instance.get_order_history = Mock(return_value=[{
        "client_order_id": "coid",
        "orders": [{
            "client_order_id": "coid",
            "status": "CANCELLED",
            "filled_quantity": "0",
            "total_quantity": "1",
        }],
    }])

    result = instance.lookup_order_status("coid")

    assert result is not None
    assert result.normalized_status == "CANCELLED"


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"client_order_id": "other", "order_id": "order-1"},
        {"client_order_id": "coid", "order_id": 1},
    ],
)
def test_cancel_malformed_or_mismatched_response_is_ambiguous(response):
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.gateway = SimpleNamespace(cancel_order=Mock(return_value=response))
    instance.get_order_status = Mock()

    with pytest.raises(broker.OrderCancellationUnknownError):
        instance.cancel_order("coid")

    instance.gateway.cancel_order.assert_called_once_with("coid")
    instance.get_order_status.assert_not_called()


def test_filled_label_without_executed_quantity_is_not_a_verified_fill():
    """Do not turn an inconsistent/transient detail response into a fill."""
    instance, _ = _order_detail_broker(
        {"status": "FILLED", "filled_quantity": "0"}
    )

    status = instance.get_order_status("coid")

    assert status.is_filled is False
    assert status.is_terminal_unfilled is False


def test_negative_filled_quantity_is_rejected():
    instance, _ = _order_detail_broker(
        {"status": "FILLED", "filled_quantity": "-0.1"}
    )

    with pytest.raises(broker.BrokerValidationError, match="negative filled quantity"):
        instance.get_order_status("coid")


def test_get_position_quantity_reads_only_position_endpoint():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance._fetch_quantity = Mock(return_value=4.273)

    assert instance.get_position_quantity("smr") == 4.273
    instance._fetch_quantity.assert_called_once_with("SMR")


@pytest.mark.parametrize("quantity", [-1, float("inf"), float("nan")])
def test_get_position_quantity_rejects_invalid_snapshot(quantity):
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance._fetch_quantity = Mock(return_value=quantity)

    with pytest.raises(broker.BrokerValidationError, match="invalid quantity"):
        instance.get_position_quantity("SMR")


def test_place_market_order_is_submitted_once_and_never_resubmitted():
    """Order placement must not retry: a resubmit trips 417 / risks a dup fill."""
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "acct"
    instance.config = SimpleNamespace(
        support_trading_session="CORE",
    )
    # A 504 on the submit is ambiguous — the order may already have landed.
    preview = Mock(return_value={
        "estimated_cost": "10",
        "estimated_transaction_fee": "0",
    })
    place = Mock(side_effect=broker.BrokerHTTPError(504, "gateway timeout"))
    instance.gateway = SimpleNamespace(
        preview_market_order=preview,
        place_market_order=place,
    )

    with patch("broker.time.sleep") as sleep:
        with pytest.raises(broker.OrderSubmissionUnknownError):
            instance.place_market_order("SMR", "BUY", 1.0, "coid")

    assert place.call_count == 1   # submitted exactly once
    sleep.assert_not_called()      # no retry/backoff on the order path


def test_order_with_id_defaults_to_submitted_instead_of_unknown():
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "acct"
    instance.config = SimpleNamespace(
        support_trading_session="CORE",
    )
    place = Mock(return_value={
        "client_order_id": "coid",
        "order_id": "order-1",
    })
    instance.gateway = SimpleNamespace(
        preview_market_order=Mock(return_value={
            "estimated_cost": "10",
            "estimated_transaction_fee": "0",
        }),
        place_market_order=place,
    )

    result = instance.place_market_order("SMR", "BUY", 1.0, "coid")

    assert result.order_id == "order-1"
    assert result.status == "SUBMITTED"
    assert result.accepted is True
    assert result.reason is None


@pytest.mark.parametrize(
    "place_response",
    [
        {},
        {"client_order_id": "COID", "order_id": "order-1"},
        {"client_order_id": "coid", "order_id": ""},
        {"client_order_id": "coid", "order_id": 123},
        {"orders": [{"client_order_id": "coid", "order_id": "nested"}]},
    ],
)
def test_place_response_must_be_exact_flat_string_contract(place_response):
    instance = _order_broker(place_response)
    with pytest.raises(broker.OrderSubmissionUnknownError):
        instance.place_market_order("SMR", "BUY", 1.0, "coid")


def _order_broker(place_return):
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "acct"
    instance.config = SimpleNamespace(
        support_trading_session="CORE",
    )
    place = Mock(return_value=place_return)
    instance.gateway = SimpleNamespace(
        preview_market_order=Mock(return_value={
            "estimated_cost": "10",
            "estimated_transaction_fee": "0",
        }),
        place_market_order=place,
    )
    return instance


def test_place_order_without_id_is_not_accepted():
    """A 200 body with no order id means no live order — must not be 'accepted'."""
    instance = _order_broker({
        "client_order_id": "coid",
        "status": "REJECTED",
        "msg": "fractional order not supported",
        "code": "INVALID",
    })

    result = instance.place_market_order("SMR", "SELL", 0.1, "coid")

    assert result.order_id is None
    assert result.accepted is False
    assert result.reason == "Webull rejected the correlated order"


def test_place_order_with_rejected_status_is_not_accepted():
    """An id but a terminal-reject status still means the order never booked."""
    instance = _order_broker({
        "client_order_id": "coid",
        "order_id": "order-9",
        "status": "REJECTED",
    })

    result = instance.place_market_order("SMR", "SELL", 0.1, "coid")

    assert result.order_id == "order-9"
    assert result.status == "REJECTED"
    assert result.accepted is False


def _preview_broker(preview_return, place_mock, *, preview_raises=None):
    instance = broker.WebullBroker.__new__(broker.WebullBroker)
    instance.account_id = "acct"
    instance.config = SimpleNamespace(
        support_trading_session="CORE",
    )
    if preview_raises is not None:
        preview = Mock(side_effect=preview_raises)
    else:
        preview = Mock(return_value=preview_return)
    instance.gateway = SimpleNamespace(
        preview_market_order=preview,
        place_market_order=place_mock,
    )
    return instance, preview


def test_preview_ok_then_order_is_placed():
    """A preview that returns a cost estimate must proceed to placement."""
    place = Mock(return_value={
        "client_order_id": "coid",
        "order_id": "order-1",
    })
    instance, preview = _preview_broker(
        {"estimated_cost": "319.27", "estimated_transaction_fee": "3.42"},
        place,
    )

    result = instance.place_market_order("AAPL", "SELL", 1.0, "coid")

    assert preview.call_count == 1
    assert place.call_count == 1
    assert result.accepted is True
    assert result.order_id == "order-1"


def test_preview_error_body_blocks_placement():
    """A 200 preview with an error and no estimate must NOT place the order."""
    place = Mock()
    instance, preview = _preview_broker(
        {
            "code": "TRADE_FRAC_NOT_SUPPORT",
            "msg": "fractional order not supported",
        },
        place,
    )

    result = instance.place_market_order("AAPL", "SELL", 0.10247, "coid")

    assert preview.call_count == 1
    place.assert_not_called()
    assert result.accepted is False
    assert result.status == "PREVIEW_REJECTED"
    assert result.reason == (
        "preview rejected (TRADE_FRAC_NOT_SUPPORT): fractional order not supported"
    )


@pytest.mark.parametrize(
    "preview_response",
    [
        {},
        [],
        {"estimated_cost": "10"},
        {"estimated_transaction_fee": "1"},
        {"estimated_cost": 10, "estimated_transaction_fee": "1"},
        {"estimated_cost": "10", "estimated_transaction_fee": 1},
        {"estimatedCost": "10", "estimatedTransactionFee": "1"},
        {
            "estimated_cost": "10",
            "estimatedTransactionFee": "1",
        },
        {
            "nested": {
                "estimated_cost": "10",
                "estimated_transaction_fee": "1",
            }
        },
    ],
)
def test_preview_requires_exact_top_level_string_estimates(preview_response):
    place = Mock()
    instance, _ = _preview_broker(preview_response, place)

    result = instance.place_market_order("AAPL", "BUY", 1.0, "coid")

    assert result.accepted is False
    assert result.status == "PREVIEW_REJECTED"
    place.assert_not_called()


def test_preview_http_reject_blocks_placement():
    """A 4xx from preview means the order itself is invalid — do not place it."""
    place = Mock()
    instance, preview = _preview_broker(
        None, place,
        preview_raises=broker.BrokerHTTPError(400, "invalid quantity"),
    )

    result = instance.place_market_order("AAPL", "SELL", 0.10247, "coid")

    place.assert_not_called()
    assert result.accepted is False
    assert result.status == "PREVIEW_REJECTED"
    assert result.reason == "preview rejected (HTTP 400)"


def test_retry_retries_broker_connection_error():
    calls = 0

    @broker._retry()
    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise broker.BrokerConnectionError("network down")
        return "ok"

    with patch("broker.time.sleep"):
        assert flaky() == "ok"
    assert calls == 2


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class FakeDocument:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, transaction=None):
        return self.snapshot


class FakeTransaction:
    def __init__(self):
        self.writes = []

    def set(self, ref, payload, merge=False):
        self.writes.append((ref, payload, merge))


class FakeDb:
    def __init__(self, document):
        self.document_ref = document
        self.txn = FakeTransaction()

    def collection(self, name):
        return self

    def document(self, name):
        return self.document_ref

    def transaction(self):
        return self.txn


class FakeFirestore:
    SERVER_TIMESTAMP = object()

    @staticmethod
    def transactional(function):
        return function


def test_reserve_step_atomically_returns_signal_and_increments():
    document = FakeDocument(FakeSnapshot({"dna_step": 2}))
    db = FakeDb(document)

    with patch("state._get_firestore", return_value=(db, FakeFirestore)):
        result = state.reserve_step("p", "c", "d", "s", "SMR", 5, lambda step: step % 2)

    assert result == state.StepReservation(dna_step=2, dna_signal=0)
    assert db.txn.writes[0][1]["dna_step"] == 3
    assert db.txn.writes[0][1]["last_reserved_step"] == 2
    assert db.txn.writes[0][2] is True


def test_reserve_step_does_not_write_after_timeline_end():
    document = FakeDocument(FakeSnapshot({"dna_step": 5}))
    db = FakeDb(document)

    with patch("state._get_firestore", return_value=(db, FakeFirestore)):
        result = state.reserve_step("p", "c", "d", "s", "SMR", 5, lambda step: 1)

    assert result == state.StepReservation(dna_step=5, dna_signal=0)
    assert db.txn.writes == []


@pytest.mark.parametrize("value", [None, "bad", -1])
def test_invalid_firestore_steps_are_rejected(value):
    with pytest.raises(state.StepReadError):
        state._parse_step(value)
