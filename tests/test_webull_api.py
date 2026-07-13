from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import webull_api
from webull.data.quotes.market_data import MarketData as SdkMarketData


class JsonResponse:
    def __init__(self, value, status_code=200, text=""):
        self._value = value
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._value


def make_config(**overrides):
    values = {
        "app_key": "test-app-key",
        "app_secret": "test-app-secret",
        "account_id": "test-account-id",
        "region": "th",
        "endpoint": "th-api.uat.webullbroker.com",
        "token_dir": "test-token-dir",
        "support_trading_session": "CORE",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def market_payload(client_order_id="order_123", symbol="AAPL"):
    return [{
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": "1",
        "support_trading_session": "CORE",
        "side": "BUY",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }]


@pytest.fixture
def fake_gateway(monkeypatch):
    account_v2 = SimpleNamespace(
        get_account_list=Mock(return_value=JsonResponse([{"account_id": "acct"}])),
        get_account_balance=Mock(return_value=JsonResponse({"total_cash_balance": "10"})),
        get_account_position=Mock(return_value=JsonResponse([{"symbol": "AAPL", "quantity": "2"}])),
    )
    order_v3 = SimpleNamespace(
        get_order_open=Mock(return_value=JsonResponse([{"orders": []}])),
        get_order_history=Mock(return_value=JsonResponse([{"orders": []}])),
        get_order_detail=Mock(return_value=JsonResponse({"orders": []})),
        preview_order=Mock(return_value=JsonResponse({
            "estimated_cost": "10",
            "estimated_transaction_fee": "0",
        })),
        place_order=Mock(return_value=JsonResponse({"client_order_id": "order_123", "order_id": "1"})),
        cancel_order=Mock(return_value=JsonResponse({"client_order_id": "order_123", "order_id": "1"})),
    )
    market_data = SimpleNamespace(
        get_snapshot=Mock(return_value=JsonResponse([{"symbol": "AAPL", "price": "5"}])),
    )
    events = []

    class FakeApiClient:
        def __init__(self, app_key, app_secret, region):
            events.append(("api_client", app_key, app_secret, region))
            self._stream_logger_set = None
            self._file_logger_set = None
            self.endpoint = None
            self.token_dir = None

        def add_endpoint(self, region, endpoint):
            self.endpoint = (region, endpoint)
            events.append(("endpoint", region, endpoint))

        def set_token_dir(self, token_dir):
            self.token_dir = token_dir
            events.append(("token_dir", token_dir))

        def set_file_logger(self, *args, **kwargs):
            raise AssertionError("gateway must not install SDK file logging")

        def set_stream_logger(self, *args, **kwargs):
            raise AssertionError("gateway must not install SDK stream logging")

    class FakeTradeClient:
        def __init__(self, api_client):
            assert api_client.endpoint == ("th", "th-api.uat.webullbroker.com")
            assert api_client.token_dir == "test-token-dir"
            assert api_client._stream_logger_set is True
            assert api_client._file_logger_set is True
            events.append(("trade_client", api_client))
            self.account_v2 = account_v2
            self.order_v3 = order_v3

    class FakeMarketData:
        def __new__(cls, api_client):
            events.append(("market_data", api_client))
            return market_data

    monkeypatch.setattr(webull_api, "ApiClient", FakeApiClient)
    monkeypatch.setattr(webull_api, "TradeClient", FakeTradeClient)
    monkeypatch.setattr(webull_api, "MarketData", FakeMarketData)

    gateway = webull_api.WebullApiGateway(make_config())
    return SimpleNamespace(
        gateway=gateway,
        account_v2=account_v2,
        order_v3=order_v3,
        market_data=market_data,
        events=events,
    )


def test_real_sdk_shape_initializes_once_without_auto_logging_or_network(tmp_path):
    """TradeClient is the only owner of ClientInitializer; MarketData is direct."""
    from webull.core.client import ApiClient
    from webull.core.http.initializer.client_initializer import ClientInitializer

    config = make_config(token_dir=str(tmp_path / "token"))
    with (
        patch.object(ClientInitializer, "initializer") as initializer,
        patch.object(ApiClient, "set_file_logger") as file_logger,
        patch.object(ApiClient, "set_stream_logger") as stream_logger,
    ):
        gateway = webull_api.WebullApiGateway(config)

    initializer.assert_called_once_with(gateway.api_client)
    file_logger.assert_not_called()
    stream_logger.assert_not_called()
    assert gateway.trade_client.account_v2 is not None
    assert gateway.trade_client.order_v3 is not None
    assert gateway.market_data.client is gateway.api_client
    assert not (tmp_path / "token").exists()


def test_initialization_orders_endpoint_token_logging_trade_and_market(fake_gateway):
    names = [event[0] for event in fake_gateway.events]
    assert names == [
        "api_client",
        "endpoint",
        "token_dir",
        "trade_client",
        "market_data",
    ]
    assert sum(name == "trade_client" for name in names) == 1
    assert sum(name == "market_data" for name in names) == 1


def test_gateway_serializes_calls_through_shared_sdk_client(fake_gateway):
    active = 0
    max_active = 0
    guard = threading.Lock()

    def operation():
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return {"ok": True}

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _: fake_gateway.gateway._call_json("test", operation),
                range(4),
            )
        )

    assert results == [{"ok": True}] * 4
    assert max_active == 1


def test_account_methods_return_raw_json_and_use_account_v2(fake_gateway):
    gateway = fake_gateway.gateway

    assert gateway.get_account_list() == [{"account_id": "acct"}]
    assert gateway.get_account_balance() == {"total_cash_balance": "10"}
    assert gateway.get_positions() == [{"symbol": "AAPL", "quantity": "2"}]

    fake_gateway.account_v2.get_account_list.assert_called_once_with()
    fake_gateway.account_v2.get_account_balance.assert_called_once_with(
        "test-account-id"
    )
    fake_gateway.account_v2.get_account_position.assert_called_once_with(
        "test-account-id"
    )


@pytest.mark.parametrize(
    ("session", "extend_flag", "overnight_flag"),
    [
        ("CORE", "false", "false"),
        ("ALL", "true", "false"),
        ("NIGHT", "false", "true"),
        ("ALL_DAY", "true", "true"),
    ],
)
def test_quote_uses_documented_string_flags(
    fake_gateway,
    session,
    extend_flag,
    overnight_flag,
):
    fake_gateway.gateway.support_trading_session = session
    result = fake_gateway.gateway.get_quote(" aapl ")

    assert result == [{"symbol": "AAPL", "price": "5"}]
    fake_gateway.market_data.get_snapshot.assert_called_once_with(
        "AAPL",
        "US_STOCK",
        extend_hour_required=extend_flag,
        overnight_required=overnight_flag,
    )


def test_sdk_snapshot_request_includes_required_false_query_params():
    captured = SimpleNamespace(request=None)

    class CaptureClient:
        def get_response(self, request):
            captured.request = request
            return JsonResponse([])

    SdkMarketData(CaptureClient()).get_snapshot(
        "AAPL",
        "US_STOCK",
        extend_hour_required="false",
        overnight_required="false",
    )

    assert captured.request._params["extend_hour_required"] == "false"
    assert captured.request._params["overnight_required"] == "false"


def test_position_and_quote_preserves_both_raw_results(fake_gateway):
    assert fake_gateway.gateway.get_position_and_quote("AAPL") == {
        "positions": [{"symbol": "AAPL", "quantity": "2"}],
        "quote": [{"symbol": "AAPL", "price": "5"}],
    }


def test_order_queries_use_order_v3_and_thailand_pagination(fake_gateway):
    gateway = fake_gateway.gateway

    assert gateway.get_open_orders(20, "cursor_1") == [{"orders": []}]
    assert gateway.get_order_history(
        30,
        "2026-07-01",
        "cursor_2",
    ) == [{"orders": []}]
    assert gateway.get_order_detail("detail_1") == {"orders": []}

    fake_gateway.order_v3.get_order_open.assert_called_once_with(
        "test-account-id",
        page_size=20,
        last_client_order_id="cursor_1",
    )
    history_call = fake_gateway.order_v3.get_order_history.call_args
    assert history_call.args == ("test-account-id",)
    assert history_call.kwargs == {
        "page_size": 30,
        "start_date": "2026-07-01",
        "last_client_order_id": "cursor_2",
    }
    assert "end_date" not in history_call.kwargs
    fake_gateway.order_v3.get_order_detail.assert_called_once_with(
        "test-account-id",
        "detail_1",
    )


def test_absent_optional_query_values_are_omitted(fake_gateway):
    fake_gateway.gateway.get_open_orders()
    fake_gateway.gateway.get_order_history()

    assert fake_gateway.order_v3.get_order_open.call_args.kwargs == {"page_size": 20}
    assert fake_gateway.order_v3.get_order_history.call_args.kwargs == {
        "page_size": 20
    }


def test_market_order_preview_place_cancel_return_raw_json(fake_gateway):
    payload = market_payload()

    assert fake_gateway.gateway.preview_market_order(payload) == {
        "estimated_cost": "10",
        "estimated_transaction_fee": "0",
    }
    assert fake_gateway.gateway.place_market_order(payload) == {
        "client_order_id": "order_123",
        "order_id": "1",
    }
    assert fake_gateway.gateway.cancel_order("order_123") == {
        "client_order_id": "order_123",
        "order_id": "1",
    }

    fake_gateway.order_v3.preview_order.assert_called_once_with(
        "test-account-id",
        payload,
    )
    fake_gateway.order_v3.place_order.assert_called_once_with(
        "test-account-id",
        payload,
    )
    fake_gateway.order_v3.cancel_order.assert_called_once_with(
        "test-account-id",
        "order_123",
    )


def test_generic_preview_and_place_support_cancelable_limit_order(fake_gateway):
    payload = market_payload()
    payload[0]["order_type"] = "LIMIT"
    payload[0]["limit_price"] = "1.00"

    assert fake_gateway.gateway.preview_order(payload) == {
        "estimated_cost": "10",
        "estimated_transaction_fee": "0",
    }
    assert fake_gateway.gateway.place_order(payload)["order_id"] == "1"

    fake_gateway.order_v3.preview_order.assert_called_once_with(
        "test-account-id", payload
    )
    fake_gateway.order_v3.place_order.assert_called_once_with(
        "test-account-id", payload
    )


def test_amount_market_order_is_supported_without_mutating_payload(fake_gateway):
    payload = market_payload()
    order = payload[0]
    order["entrust_type"] = "AMOUNT"
    order.pop("quantity")
    order["total_cash_amount"] = "25.50"
    original = [dict(order)]

    fake_gateway.gateway.place_market_order(payload)

    assert payload == original
    assert fake_gateway.order_v3.place_order.call_args.args[1] is payload


@pytest.mark.parametrize("page_size", [0, 9, 101, True, 20.0, "20"])
def test_invalid_page_sizes_are_rejected_before_sdk_call(fake_gateway, page_size):
    with pytest.raises(webull_api.WebullApiValidationError):
        fake_gateway.gateway.get_open_orders(page_size)
    fake_gateway.order_v3.get_order_open.assert_not_called()


@pytest.mark.parametrize("page_size", [10, 100])
def test_official_page_size_boundaries_are_accepted(fake_gateway, page_size):
    fake_gateway.gateway.get_open_orders(page_size)
    assert fake_gateway.order_v3.get_order_open.call_args.kwargs["page_size"] == page_size


@pytest.mark.parametrize("symbol", ["", " ", "AAPL US", "AAPL/US", object()])
def test_invalid_symbols_are_rejected_before_sdk_call(fake_gateway, symbol):
    with pytest.raises(webull_api.WebullApiValidationError):
        fake_gateway.gateway.get_quote(symbol)
    fake_gateway.market_data.get_snapshot.assert_not_called()


@pytest.mark.parametrize(
    "client_order_id",
    ["", "has space", "bad/slash", "x" * 33, None],
)
def test_invalid_client_ids_are_rejected_before_sdk_call(
    fake_gateway,
    client_order_id,
):
    with pytest.raises(webull_api.WebullApiValidationError):
        fake_gateway.gateway.cancel_order(client_order_id)
    fake_gateway.order_v3.cancel_order.assert_not_called()


def test_32_character_client_id_is_accepted(fake_gateway):
    client_order_id = "x" * 32
    fake_gateway.gateway.cancel_order(client_order_id)
    fake_gateway.order_v3.cancel_order.assert_called_once_with(
        "test-account-id",
        client_order_id,
    )


@pytest.mark.parametrize("session", ["CORE", "NIGHT", "ALL", "ALL_DAY"])
def test_official_trading_sessions_are_accepted(fake_gateway, session):
    gateway = webull_api.WebullApiGateway(
        make_config(support_trading_session=session)
    )
    assert gateway.support_trading_session == session


@pytest.mark.parametrize("session", ["PRE", "AFTER", "OVERNIGHT"])
def test_legacy_trading_sessions_are_rejected(fake_gateway, session):
    with pytest.raises(webull_api.WebullApiValidationError):
        webull_api.WebullApiGateway(make_config(support_trading_session=session))


@pytest.mark.parametrize("start_date", ["2000-01-01", "2999-01-01"])
def test_history_start_date_outside_six_month_window_is_rejected(
    fake_gateway,
    start_date,
):
    with pytest.raises(webull_api.WebullApiValidationError, match="six months"):
        fake_gateway.gateway.get_order_history(start_date=start_date)


def test_history_accepts_today_as_start_date(fake_gateway):
    fake_gateway.gateway.get_order_history(start_date=date.today().isoformat())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda order: order.update(order_type="LIMIT"),
        lambda order: order.update(side="SHORT"),
        lambda order: order.update(quantity="0"),
        lambda order: order.update(limit_price="10"),
        lambda order: order.update(client_order_id="near match!"),
    ],
)
def test_invalid_market_payload_is_rejected_before_submission(
    fake_gateway,
    mutate,
):
    payload = market_payload()
    mutate(payload[0])

    with pytest.raises(webull_api.WebullApiValidationError):
        fake_gateway.gateway.place_market_order(payload)
    fake_gateway.order_v3.place_order.assert_not_called()


def test_exact_nested_order_correlation_ignores_group_and_prefix_ids():
    response = [
        {
            "client_order_id": "target",
            "orders": [
                {"client_order_id": "target-extra", "order_id": "wrong"},
                {"client_order_id": "target", "order_id": "right"},
            ],
        }
    ]

    match = webull_api.find_order_by_client_order_id(response, "target")

    assert match == {"client_order_id": "target", "order_id": "right"}
    assert webull_api.WebullApiGateway.find_order_by_client_order_id(
        response,
        "TARGET",
    ) is None


def test_duplicate_exact_order_matches_are_rejected():
    response = [
        {"orders": [{"client_order_id": "same"}]},
        {"orders": [{"client_order_id": "same"}]},
    ]

    with pytest.raises(webull_api.WebullApiProtocolError):
        webull_api.find_order_by_client_order_id(response, "same")


def test_order_correlation_rejects_undocumented_recursive_wrapper():
    response = {
        "wrapper": {
            "orders": [{"client_order_id": "target", "order_id": "wrong"}]
        }
    }

    with pytest.raises(webull_api.WebullApiProtocolError, match="orders"):
        webull_api.find_order_by_client_order_id(response, "target")


def test_sdk_server_exception_is_sanitized_without_context(fake_gateway):
    secret = "do-not-leak-this-secret"
    fake_gateway.account_v2.get_account_list.side_effect = ServerExceptionForTest(
        secret
    )

    with pytest.raises(webull_api.WebullApiUpstreamError) as caught:
        fake_gateway.gateway.get_account_list()

    assert secret not in str(caught.value)
    assert "get_account_list failed" in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


class ServerExceptionForTest(webull_api.ServerException):
    def __init__(self, secret):
        super().__init__(
            "INVALID_PARAMETER",
            f"request headers and body contain {secret}",
            http_status=417,
            request_id="safe-request-id",
        )


def test_non_2xx_response_body_is_not_copied_to_exception(fake_gateway):
    secret = "response-body-secret"
    fake_gateway.account_v2.get_account_list.return_value = JsonResponse(
        {"error": secret},
        status_code=500,
        text=secret,
    )

    with pytest.raises(webull_api.WebullApiUpstreamError) as caught:
        fake_gateway.gateway.get_account_list()

    assert str(caught.value) == "get_account_list failed (HTTP 500)"
    assert secret not in str(caught.value)


def test_invalid_json_error_is_sanitized(fake_gateway):
    class InvalidJsonResponse:
        status_code = 200
        text = "credential-bearing body"

        def json(self):
            raise ValueError(self.text)

    fake_gateway.account_v2.get_account_list.return_value = InvalidJsonResponse()

    with pytest.raises(webull_api.WebullApiProtocolError) as caught:
        fake_gateway.gateway.get_account_list()

    assert "credential-bearing" not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_key": ""},
        {"app_secret": " secret-with-space "},
        {"account_id": None},
        {"region": ""},
        {"endpoint": "https://th-api.uat.webullbroker.com"},
        {"endpoint": "th-api.uat.webullbroker.com/path"},
        {"token_dir": ""},
    ],
)
def test_invalid_config_fails_before_sdk_construction(monkeypatch, overrides):
    api_client = Mock(side_effect=AssertionError("must not construct SDK"))
    monkeypatch.setattr(webull_api, "ApiClient", api_client)

    with pytest.raises(webull_api.WebullApiValidationError):
        webull_api.WebullApiGateway(make_config(**overrides))

    api_client.assert_not_called()


def test_history_rejects_invalid_dates_before_sdk_call(fake_gateway):
    with pytest.raises(webull_api.WebullApiValidationError):
        fake_gateway.gateway.get_order_history(start_date="2026-02-30")
    fake_gateway.order_v3.get_order_history.assert_not_called()
