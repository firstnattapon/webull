from __future__ import annotations

import sys
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts import webull_live_test as live


ENV_NAMES = (
    "WEBULL_ENV",
    "WEBULL_REGION",
    "WEBULL_API_VERSION",
    "WEBULL_TRADING_ENDPOINT",
    "WEBULL_APP_KEY",
    "WEBULL_APP_SECRET",
    "WEBULL_ACCOUNT_ID",
    "WEBULL_TEST_SYMBOL",
    "WEBULL_TEST_SIDE",
    "WEBULL_TEST_QUANTITY",
    "WEBULL_TEST_SESSION",
    "WEBULL_TEST_MAX_NOTIONAL",
    "WEBULL_TEST_LIMIT_PRICE",
    "WEBULL_TEST_CLIENT_ORDER_ID",
    "WEBULL_TEST_POLL_ATTEMPTS",
    "WEBULL_TEST_POLL_INTERVAL_SECONDS",
    "WEBULL_MUTATION_ARM",
    "WEBULL_MUTATION_ACCOUNT_ID_CONFIRM",
    "WEBULL_MUTATION_ORDER_CONFIRM",
)


def _clear_harness_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _base_env(monkeypatch):
    _clear_harness_env(monkeypatch)
    monkeypatch.setenv("WEBULL_ENV", "uat")
    monkeypatch.setenv("WEBULL_APP_KEY", "test-app-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "1234567890")


def _arm_mutation(
    monkeypatch,
    *,
    mode=live.MARKET_PLACE_MODE,
    max_notional="500",
    limit_price=None,
):
    monkeypatch.setenv("WEBULL_TEST_SYMBOL", "AAPL")
    monkeypatch.setenv("WEBULL_TEST_SIDE", "BUY")
    monkeypatch.setenv("WEBULL_TEST_QUANTITY", "1")
    monkeypatch.setenv("WEBULL_TEST_SESSION", "CORE")
    monkeypatch.setenv("WEBULL_TEST_MAX_NOTIONAL", max_notional)
    if limit_price is not None:
        monkeypatch.setenv("WEBULL_TEST_LIMIT_PRICE", limit_price)
    monkeypatch.setenv("WEBULL_MUTATION_ARM", live.MUTATION_ARMING_PHRASE)
    monkeypatch.setenv("WEBULL_MUTATION_ACCOUNT_ID_CONFIRM", "1234567890")
    monkeypatch.setenv(
        "WEBULL_MUTATION_ORDER_CONFIRM",
        live.expected_mutation_order_confirmation(
            "1234567890",
            mode,
            "AAPL",
            "BUY",
            Decimal("1"),
            "CORE",
            Decimal(max_notional),
            Decimal(limit_price) if limit_price is not None else None,
        ),
    )


def _arm_market_mutation(monkeypatch):
    _arm_mutation(monkeypatch)


def _arm_limit_mutation(monkeypatch, *, limit_price="50"):
    _arm_mutation(
        monkeypatch,
        mode=live.LIMIT_CANCEL_MODE,
        limit_price=limit_price,
    )


def test_live_harness_requires_explicit_environment(monkeypatch):
    _clear_harness_env(monkeypatch)
    monkeypatch.setenv("WEBULL_APP_KEY", "test-app-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "1234567890")

    with pytest.raises(live.HarnessFailure, match="missing_webull_env"):
        live.load_harness_config(live.READ_ONLY_MODE)


def test_live_harness_rejects_non_allowlisted_endpoint(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBULL_TRADING_ENDPOINT", "attacker.invalid")

    with pytest.raises(live.HarnessFailure, match="endpoint_not_allowlisted"):
        live.load_harness_config(live.READ_ONLY_MODE)


def test_live_harness_rejects_production_even_for_read_preview(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBULL_ENV", "prod")

    with pytest.raises(live.HarnessFailure, match="live_harness_requires_uat"):
        live.load_harness_config(live.READ_ONLY_MODE)


def test_detail_client_order_id_must_fit_official_32_character_limit(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBULL_TEST_CLIENT_ORDER_ID", "x" * 33)

    with pytest.raises(
        live.HarnessFailure,
        match="invalid_webull_test_client_order_id",
    ):
        live.load_harness_config(live.READ_ONLY_MODE)


def test_mutation_requires_complete_parameters_and_exact_arming(monkeypatch):
    _base_env(monkeypatch)

    with pytest.raises(live.HarnessFailure, match="missing_webull_test_symbol"):
        live.load_harness_config(live.MARKET_PLACE_MODE)

    _arm_market_mutation(monkeypatch)
    monkeypatch.setenv("WEBULL_MUTATION_ARM", "yes")
    with pytest.raises(live.HarnessFailure, match="mutation_not_armed"):
        live.load_harness_config(live.MARKET_PLACE_MODE)


def test_mutation_binds_exact_configured_account(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    monkeypatch.setenv("WEBULL_MUTATION_ACCOUNT_ID_CONFIRM", "different-account")

    with pytest.raises(live.HarnessFailure, match="mutation_account_binding_failed"):
        live.load_harness_config(live.MARKET_PLACE_MODE)


def test_mutation_requires_exact_normalized_order_binding(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    monkeypatch.setenv("WEBULL_TEST_QUANTITY", "1.00000")
    monkeypatch.setenv("WEBULL_MUTATION_ORDER_CONFIRM", "uat|wrong-order-binding")

    with pytest.raises(live.HarnessFailure, match="mutation_order_binding_failed"):
        live.load_harness_config(live.MARKET_PLACE_MODE)

    expected = live.expected_mutation_order_confirmation(
        "1234567890",
        live.MARKET_PLACE_MODE,
        "AAPL",
        "BUY",
        Decimal("1.00000"),
        "CORE",
        Decimal("500"),
        None,
    )
    monkeypatch.setenv("WEBULL_MUTATION_ORDER_CONFIRM", expected)
    config = live.load_harness_config(live.MARKET_PLACE_MODE)

    assert "1234567890" not in expected
    assert "quantity=1|" in expected
    assert config.quantity == Decimal("1.00000")


def test_armed_market_mutation_loads_only_for_uat(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)

    config = live.load_harness_config(live.MARKET_PLACE_MODE)

    assert config.mutating is True
    assert config.environment == "uat"
    assert config.endpoint == live.OFFICIAL_ENDPOINTS["uat"]
    assert config.max_notional == Decimal("500")


def test_order_payload_preserves_integer_and_fractional_decimal_text(monkeypatch):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)

    integer_config = replace(config, quantity=Decimal("100"))
    integer_payload = live.build_order_payload(
        integer_config,
        "client-1",
        order_type="MARKET",
    )
    fractional_payload = live.build_order_payload(
        replace(config, quantity=Decimal("0.05000")),
        "client-2",
        order_type="LIMIT",
        limit_price=Decimal("10.500"),
    )

    assert integer_payload[0]["quantity"] == "100"
    assert integer_payload[0]["order_type"] == "MARKET"
    assert "limit_price" not in integer_payload[0]
    assert fractional_payload[0]["quantity"] == "0.05"
    assert fractional_payload[0]["limit_price"] == "10.5"


def test_untrusted_status_text_is_not_copied_into_metadata():
    assert live._order_status({"status": "raw response secret with spaces"}) == "UNKNOWN"


class _ReadOnlyFakeGateway:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.place_calls = 0
        self.cancel_calls = 0

    def get_account_list(self):
        print("SDK-CONSOLE-RAW")
        return [{"account_id": self.account_id, "raw_marker": "ACCOUNT-RAW"}]

    def get_account_balance(self):
        return {"buying_power": "100000", "raw_marker": "BALANCE-RAW"}

    def get_positions(self):
        return {
            "positions": [
                {"symbol": "AAPL", "quantity": "2", "raw_marker": "POSITION-RAW"}
            ]
        }

    def get_quote(self, symbol):
        return {"symbol": symbol, "price": "100", "raw_marker": "QUOTE-RAW"}

    def get_open_orders(self, page_size=20, last_client_order_id=None):
        return []

    def get_order_history(
        self,
        page_size=20,
        start_date=None,
        last_client_order_id=None,
    ):
        return [{
            "client_order_id": "existing-order-for-detail",
            "orders": [
                {
                    "client_order_id": "existing-order-for-detail",
                    "status": "FILLED",
                    "raw_marker": "HISTORY-RAW",
                }
            ],
        }]

    def get_order_detail(self, client_order_id):
        return {
            "client_order_id": client_order_id,
            "orders": [
                {
                    "client_order_id": client_order_id,
                    "status": "FILLED",
                    "filled_quantity": "1",
                    "raw_marker": "DETAIL-RAW",
                }
            ]
        }

    def preview_market_order(self, payload):
        return {
            "estimated_cost": "100",
            "estimated_transaction_fee": "0",
            "raw_marker": "PREVIEW-RAW",
        }

    def place_market_order(self, payload):
        self.place_calls += 1
        raise AssertionError("read-preview must never place")

    def cancel_order(self, client_order_id):
        self.cancel_calls += 1
        raise AssertionError("read-preview must never cancel")


def test_default_run_is_read_preview_only_and_report_is_metadata(monkeypatch, capsys):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)
    fake = _ReadOnlyFakeGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)
    encoded = live.serialize_report(report, config.secret_values())

    assert report["status"] == "PASS"
    assert fake.place_calls == 0
    assert fake.cancel_calls == 0
    assert [step["name"] for step in report["steps"]] == [
        "account_list",
        "account_balance",
        "positions",
        "quote",
        "open_orders",
        "order_history",
        "order_detail",
        "market_order_preview",
    ]
    for raw_marker in (
        "ACCOUNT-RAW",
        "BALANCE-RAW",
        "POSITION-RAW",
        "QUOTE-RAW",
        "HISTORY-RAW",
        "DETAIL-RAW",
        "PREVIEW-RAW",
    ):
        assert raw_marker not in encoded
    assert config.app_key not in encoded
    assert config.app_secret not in encoded
    assert config.account_id not in encoded
    assert "SDK-CONSOLE-RAW" not in capsys.readouterr().out


def test_gateway_initialization_error_does_not_copy_exception_message(monkeypatch):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)

    def failing_factory(_config, _token_dir):
        raise RuntimeError(f"signed request contained {config.app_secret}")

    report = live.run_harness(config, gateway_factory=failing_factory)
    encoded = live.serialize_report(report, config.secret_values())

    assert report["status"] == "FAIL"
    assert report["failure_code"] == "gateway_initialization_failed"
    assert config.app_secret not in encoded


def test_cancel_guard_accepts_only_ids_created_by_same_run(monkeypatch):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)
    runner = live.LiveHarnessRunner(config, _ReadOnlyFakeGateway(config.account_id))

    with pytest.raises(live.HarnessFailure, match="cancel_target_not_owned_by_run"):
        runner._assert_owned("pre-existing-order")

    runner.owned_client_order_ids.add("created-this-run")
    runner._assert_owned("created-this-run")


@pytest.mark.parametrize("session", ["CORE", "ALL", "NIGHT", "ALL_DAY"])
def test_harness_accepts_only_official_trading_sessions(monkeypatch, session):
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBULL_TEST_SESSION", session)

    assert live.load_harness_config(live.READ_ONLY_MODE).session == session


def test_harness_rejects_unknown_trading_session(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBULL_TEST_SESSION", "PRE")

    with pytest.raises(live.HarnessFailure, match="invalid_webull_test_session"):
        live.load_harness_config(live.READ_ONLY_MODE)


def test_limit_cancel_has_longer_bounded_propagation_defaults(monkeypatch):
    _base_env(monkeypatch)
    read_config = live.load_harness_config(live.READ_ONLY_MODE)
    _arm_limit_mutation(monkeypatch, limit_price="50")
    limit_config = live.load_harness_config(live.LIMIT_CANCEL_MODE)

    assert (read_config.poll_attempts, read_config.poll_interval_seconds) == (6, 2.0)
    assert (limit_config.poll_attempts, limit_config.poll_interval_seconds) == (20, 3.0)


def test_gateway_failure_metadata_exposes_only_bounded_status_and_code(monkeypatch):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)
    runner = live.LiveHarnessRunner(config, _ReadOnlyFakeGateway(config.account_id))

    error = RuntimeError("secret response body")
    error.status_code = 417
    error.error_code = "ORDER_PRICE_INVALID"
    with pytest.raises(live.HarnessFailure, match="diagnostic_gateway_call_failed"):
        runner._execute("diagnostic", lambda: (_ for _ in ()).throw(error), lambda _: {})

    metadata = runner.steps[-1].metadata
    assert metadata == {
        "failure_code": "gateway_call_failed",
        "error_type": "RuntimeError",
        "http_status": 417,
        "upstream_error_code": "ORDER_PRICE_INVALID",
    }
    assert "secret response body" not in live.serialize_report(
        {"steps": [runner.steps[-1].to_dict()]}, config.secret_values()
    )


def test_limit_cancel_requires_safely_non_marketable_price(monkeypatch):
    _base_env(monkeypatch)
    _arm_limit_mutation(monkeypatch, limit_price="99.01")
    config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    runner = live.LiveHarnessRunner(config, _ReadOnlyFakeGateway(config.account_id))
    runner.quote = Decimal("100")

    with pytest.raises(live.HarnessFailure, match="limit_price_not_safely_non_marketable"):
        runner._validate_non_marketable_limit()

    safe_runner = live.LiveHarnessRunner(
        replace(config, limit_price=Decimal("99")),
        _ReadOnlyFakeGateway(config.account_id),
    )
    safe_runner.quote = Decimal("100")
    safe_runner._validate_non_marketable_limit()

    unsafe_sell_runner = live.LiveHarnessRunner(
        replace(config, side="SELL", limit_price=Decimal("100.99")),
        _ReadOnlyFakeGateway(config.account_id),
    )
    unsafe_sell_runner.quote = Decimal("100")
    with pytest.raises(live.HarnessFailure, match="limit_price_not_safely_non_marketable"):
        unsafe_sell_runner._validate_non_marketable_limit()

    safe_sell_runner = live.LiveHarnessRunner(
        replace(config, side="SELL", limit_price=Decimal("101")),
        _ReadOnlyFakeGateway(config.account_id),
    )
    safe_sell_runner.quote = Decimal("100")
    safe_sell_runner._validate_non_marketable_limit()


class _StatefulMutationGateway(_ReadOnlyFakeGateway):
    def __init__(self, account_id: str):
        super().__init__(account_id)
        self.created_id = None
        self.created_order_type = None
        self.order_open = False
        self.created_status = None
        self.created_filled = Decimal("0")
        self.cancelled_ids = []
        self.market_preview_payloads = []
        self.market_place_payloads = []

    def get_positions(self):
        quantity = Decimal("2")
        if self.created_id is not None:
            quantity += self.created_filled
        return {
            "positions": [
                {"symbol": "AAPL", "quantity": live._decimal_text(quantity)}
            ]
        }

    def get_open_orders(self, page_size=20, last_client_order_id=None):
        if self.order_open and self.created_id:
            return [{
                "client_order_id": self.created_id,
                "orders": [
                    {
                        "client_order_id": self.created_id,
                        "order_id": "created-order-id",
                        "status": "WORKING",
                    }
                ]
            }]
        return []

    def get_order_history(
        self,
        page_size=20,
        start_date=None,
        last_client_order_id=None,
    ):
        orders = [{"client_order_id": "existing-order-for-detail", "status": "FILLED"}]
        if self.created_id:
            orders.append(self._created_record())
        return [{
            "client_order_id": orders[-1]["client_order_id"],
            "orders": orders,
        }]

    def get_order_detail(self, client_order_id):
        if client_order_id == self.created_id:
            return {
                "client_order_id": client_order_id,
                "orders": [self._created_record()],
            }
        return {
            "client_order_id": client_order_id,
            "orders": [
                {
                    "client_order_id": client_order_id,
                    "status": "FILLED",
                    "filled_quantity": "1",
                }
            ]
        }

    def _created_record(self):
        return {
            "client_order_id": self.created_id,
            "order_id": "created-order-id",
            "status": self.created_status,
            "filled_quantity": live._decimal_text(self.created_filled),
        }

    def place_market_order(self, payload):
        self.place_calls += 1
        self.market_place_payloads.append(payload)
        self.created_id = payload[0]["client_order_id"]
        self.created_order_type = "MARKET"
        self.created_status = "FILLED"
        self.created_filled = Decimal(payload[0]["quantity"])
        return {
            "client_order_id": self.created_id,
            "order_id": "created-order-id",
        }

    def preview_market_order(self, payload):
        self.market_preview_payloads.append(payload)
        return super().preview_market_order(payload)

    def preview_order(self, payload):
        return self.preview_market_order(payload)

    def place_order(self, payload):
        self.place_calls += 1
        self.created_id = payload[0]["client_order_id"]
        self.created_order_type = "LIMIT"
        self.created_status = "WORKING"
        self.created_filled = Decimal("0")
        self.order_open = True
        return {
            "client_order_id": self.created_id,
            "order_id": "created-order-id",
        }

    def cancel_order(self, client_order_id):
        self.cancel_calls += 1
        self.cancelled_ids.append(client_order_id)
        assert client_order_id == self.created_id
        self.order_open = False
        self.created_status = "CANCELLED"
        return {
            "client_order_id": self.created_id,
            "order_id": "created-order-id",
        }


def test_armed_market_mode_places_once_and_reconciles_without_cancel(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    config = live.load_harness_config(live.MARKET_PLACE_MODE)
    fake = _StatefulMutationGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "PASS"
    assert fake.place_calls == 1
    assert fake.cancel_calls == 0
    assert fake.market_preview_payloads[-1] is fake.market_place_payloads[-1]
    assert (
        fake.market_preview_payloads[-1][0]["client_order_id"]
        == fake.created_id
    )
    assert any(
        step["name"] == "market_order_place_preview"
        and step["status"] == "PASS"
        for step in report["steps"]
    )
    assert any(
        step["name"] == "market_order_position_reconciled"
        and step["status"] == "PASS"
        for step in report["steps"]
    )
    submission_preview = next(
        step
        for step in report["steps"]
        if step["name"] == "market_order_place_preview"
    )
    placed = next(
        step for step in report["steps"] if step["name"] == "market_order_place"
    )
    assert submission_preview["metadata"]["advisory_notional_guard_passed"] is True
    assert submission_preview["metadata"]["hard_price_cap_enforced"] is False
    assert placed["metadata"]["hard_price_cap_enforced"] is False
    assert "within_max_notional" not in placed["metadata"]


def test_limit_cancel_uses_generic_place_and_cancels_only_its_own_id(monkeypatch):
    _base_env(monkeypatch)
    _arm_limit_mutation(monkeypatch)
    config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    fake = _StatefulMutationGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "PASS"
    assert fake.created_order_type == "LIMIT"
    assert fake.place_calls == 1
    assert fake.cancel_calls == 1
    assert fake.cancelled_ids == [fake.created_id]
    assert "existing-order-for-detail" not in fake.cancelled_ids


class _StaleCancelledDetailGateway(_StatefulMutationGateway):
    def cancel_order(self, client_order_id):
        response = super().cancel_order(client_order_id)
        self.created_status = "CANCELED"
        return response

    def get_order_detail(self, client_order_id):
        response = super().get_order_detail(client_order_id)
        if client_order_id == self.created_id and self.created_status in live.CANCELLED_STATUSES:
            response["orders"][0]["status"] = "SUBMITTED"
        return response


def test_limit_cancel_uses_exact_history_when_detail_remains_stale(monkeypatch):
    _base_env(monkeypatch)
    _arm_limit_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    fake = _StaleCancelledDetailGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "PASS"
    confirmation = next(
        step for step in report["steps"] if step["name"] == "limit_order_cancelled_detail"
    )
    assert confirmation["metadata"]["confirmation_source"] == "history"
    assert confirmation["metadata"]["status_category"] == "CANCELED"
    assert fake.cancel_calls == 1


def test_order_correlation_uses_unique_documented_orders_only():
    response = [
        {
            "client_order_id": "target",
            "orders": [
                {"client_order_id": "target-extra", "order_id": "wrong"},
                {"client_order_id": "target", "order_id": "right"},
            ],
        }
    ]

    assert live._find_correlated_order(response, "target") == {
        "client_order_id": "target",
        "order_id": "right",
    }

    with pytest.raises(
        live.HarnessFailure,
        match="multiple_orders_matched_client_order_id",
    ):
        live._find_correlated_order(
            {
                "orders": [
                    {"client_order_id": "target"},
                    {"client_order_id": "target"},
                ]
            },
            "target",
        )


def test_group_ids_and_nested_child_status_are_not_borrowed():
    response = [
        {
            "client_order_id": "group-id",
            "orders": [
                {
                    "client_order_id": "target",
                    "orders": [
                        {
                            "client_order_id": "different-child",
                            "status": "FILLED",
                            "filled_quantity": "1",
                        }
                    ],
                }
            ],
        }
    ]

    assert live._documented_client_order_ids(response) == ["target"]
    record = live._find_correlated_order(response, "target")
    assert record is not None
    assert live._order_status(record) == "UNKNOWN"
    assert live._filled_quantity(record) == Decimal("0")


def test_order_query_and_detail_require_their_official_top_level_shapes(
    monkeypatch,
):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)
    runner = live.LiveHarnessRunner(
        config,
        _ReadOnlyFakeGateway(config.account_id),
    )

    with pytest.raises(live.HarnessFailure, match="invalid_order_query_response"):
        runner._validate_orders_container({"orders": []})
    with pytest.raises(live.HarnessFailure, match="invalid_order_detail_response"):
        runner._validate_detail(
            [{"client_order_id": "target", "orders": []}],
            "target",
        )


def test_core_quote_never_uses_a_prior_close():
    with pytest.raises(live.HarnessFailure, match="invalid_quote_response"):
        live._extract_quote({"symbol": "AAPL", "close": "100"}, "AAPL")


def test_open_order_absence_is_checked_across_every_page(monkeypatch):
    _base_env(monkeypatch)
    config = replace(
        live.load_harness_config(live.READ_ONLY_MODE),
        poll_attempts=1,
    )
    first_page = [
        {"client_order_id": f"group_{index}", "orders": []}
        for index in range(live.ORDER_QUERY_PAGE_SIZE)
    ]
    second_page = [{
        "client_order_id": "target_group",
        "orders": [{"client_order_id": "target_order"}],
    }]

    class PaginatedGateway:
        def __init__(self):
            self.cursors = []

        def get_open_orders(self, page_size, last_client_order_id):
            self.cursors.append(last_client_order_id)
            return first_page if last_client_order_id is None else second_page

    gateway = PaginatedGateway()
    runner = live.LiveHarnessRunner(config, gateway)

    with pytest.raises(live.HarnessFailure, match="cancelled_order_still_open"):
        runner._poll_open_presence("target_order", expected_present=False)

    assert gateway.cursors == [None, "group_99"]


def test_position_reconciliation_rejects_one_percent_style_tolerance(
    monkeypatch,
):
    _base_env(monkeypatch)
    config = replace(
        live.load_harness_config(live.READ_ONLY_MODE),
        poll_attempts=1,
    )
    gateway = SimpleNamespace(
        get_positions=lambda: {
            "positions": [{"symbol": "AAPL", "quantity": "2.99999"}]
        }
    )
    runner = live.LiveHarnessRunner(config, gateway)
    runner.position_before = Decimal("2")

    with pytest.raises(
        live.HarnessFailure,
        match="filled_order_position_not_reconciled",
    ):
        runner._poll_position_reconciliation(Decimal("1"))


def test_preview_and_mutation_acknowledgements_follow_flat_official_contract():
    preview = {
        "estimated_cost": "100",
        "estimated_transaction_fee": "0",
    }
    assert live._validate_preview(preview)["accepted"] is True

    with pytest.raises(
        live.HarnessFailure,
        match="preview_missing_required_string_estimate",
    ):
        live._validate_preview({"estimated_cost": "100"})
    with pytest.raises(live.HarnessFailure, match="invalid_preview_response"):
        live._validate_preview({"orders": [preview]})

    flat_ack = {"client_order_id": "target", "order_id": "order-id"}
    assert live._validate_place_response(flat_ack, "target")["correlated"] is True
    assert live._validate_cancel_response(flat_ack, "target")["correlated"] is True
    with pytest.raises(live.HarnessFailure, match="place_invalid_response"):
        live._validate_place_response({"orders": [flat_ack]}, "target")


def test_default_gateway_factory_passes_support_trading_session(
    monkeypatch,
    tmp_path,
):
    _base_env(monkeypatch)
    config = live.load_harness_config(live.READ_ONLY_MODE)
    captured = {}

    class FakeGateway:
        def __init__(self, gateway_config):
            captured["config"] = gateway_config

    monkeypatch.setitem(
        sys.modules,
        "webull_api",
        SimpleNamespace(WebullApiGateway=FakeGateway),
    )

    live._default_gateway_factory(config, str(tmp_path))

    assert captured["config"].support_trading_session == "CORE"


def test_mutation_binding_includes_mode_max_notional_and_limit_price(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)

    market_config = live.load_harness_config(live.MARKET_PLACE_MODE)
    confirmation = live.expected_mutation_order_confirmation(
        market_config.account_id,
        market_config.mode,
        market_config.symbol,
        market_config.side,
        market_config.quantity,
        market_config.session,
        market_config.max_notional,
        market_config.limit_price,
    )
    assert "|mode=market-place|" in confirmation
    assert "|max-notional=500|limit-price=none" in confirmation

    monkeypatch.setenv("WEBULL_TEST_MAX_NOTIONAL", "600")
    with pytest.raises(live.HarnessFailure, match="mutation_order_binding_failed"):
        live.load_harness_config(live.MARKET_PLACE_MODE)

    _arm_market_mutation(monkeypatch)
    monkeypatch.setenv("WEBULL_TEST_LIMIT_PRICE", "50")
    with pytest.raises(live.HarnessFailure, match="mutation_order_binding_failed"):
        live.load_harness_config(live.LIMIT_CANCEL_MODE)

    _arm_limit_mutation(monkeypatch, limit_price="50")
    limit_config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    limit_confirmation = live.expected_mutation_order_confirmation(
        limit_config.account_id,
        limit_config.mode,
        limit_config.symbol,
        limit_config.side,
        limit_config.quantity,
        limit_config.session,
        limit_config.max_notional,
        limit_config.limit_price,
    )
    assert "|mode=limit-cancel|" in limit_confirmation
    assert limit_confirmation.endswith("|limit-price=50")
    monkeypatch.setenv("WEBULL_TEST_LIMIT_PRICE", "40")
    with pytest.raises(live.HarnessFailure, match="mutation_order_binding_failed"):
        live.load_harness_config(live.LIMIT_CANCEL_MODE)


class _PreviewOverLimitGateway(_StatefulMutationGateway):
    def preview_market_order(self, payload):
        response = super().preview_market_order(payload)
        if len(self.market_preview_payloads) > 1:
            response["estimated_cost"] = "600"
        return response


def test_market_preview_notional_guard_is_advisory_and_blocks_before_place(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    config = live.load_harness_config(live.MARKET_PLACE_MODE)
    fake = _PreviewOverLimitGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert report["failure_code"] == "advisory_max_notional_exceeded_by_preview"
    assert fake.place_calls == 0
    assert fake.cancel_calls == 0


class _PartialMarketGateway(_StatefulMutationGateway):
    def place_market_order(self, payload):
        self.place_calls += 1
        self.market_place_payloads.append(payload)
        self.created_id = payload[0]["client_order_id"]
        self.created_order_type = "MARKET"
        self.created_status = "PARTIALLY_FILLED"
        self.created_filled = Decimal("0.4")
        self.order_open = True
        return {
            "client_order_id": self.created_id,
            "order_id": "created-order-id",
        }


def test_market_partial_fill_fails_cancels_owned_residual_and_reconciles(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.MARKET_PLACE_MODE)
    fake = _PartialMarketGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert report["failure_code"] == "market_order_not_fully_filled"
    assert fake.cancel_calls == 1
    assert fake.cancelled_ids == [fake.created_id]
    assert any(
        step["name"] == "market_order_cleanup"
        and step["status"] == "PASS"
        and step["metadata"]["terminal_state_verified"] is True
        for step in report["steps"]
    )
    assert any(
        step["name"] == "market_order_cleanup_position_reconciled"
        and step["status"] == "PASS"
        for step in report["steps"]
    )


class _AmbiguousMarketPlaceGateway(_StatefulMutationGateway):
    def place_market_order(self, payload):
        self.place_calls += 1
        self.market_place_payloads.append(payload)
        self.created_id = payload[0]["client_order_id"]
        self.created_order_type = "MARKET"
        self.created_status = "WORKING"
        self.created_filled = Decimal("0")
        self.order_open = True
        raise RuntimeError("simulated lost placement response")


def test_market_lost_place_response_queries_detail_and_cleans_owned_order(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.MARKET_PLACE_MODE)
    fake = _AmbiguousMarketPlaceGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert report["failure_code"] == "market_order_place_gateway_call_failed"
    assert fake.place_calls == 1
    assert fake.cancel_calls == 1
    assert any(
        step["name"] == "market_order_cleanup" and step["status"] == "PASS"
        for step in report["steps"]
    )


class _DelayedDetailAfterAmbiguousPlaceGateway(_AmbiguousMarketPlaceGateway):
    def __init__(self, account_id):
        super().__init__(account_id)
        self.created_detail_calls = 0

    def get_order_detail(self, client_order_id):
        if client_order_id == self.created_id:
            self.created_detail_calls += 1
            if self.created_detail_calls <= 6:
                raise RuntimeError("simulated detail propagation delay")
        return super().get_order_detail(client_order_id)


def test_market_cleanup_cancels_owned_id_when_detail_visibility_is_delayed(monkeypatch):
    _base_env(monkeypatch)
    _arm_market_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.MARKET_PLACE_MODE)
    fake = _DelayedDetailAfterAmbiguousPlaceGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert fake.cancel_calls == 1
    cleanup = next(
        step for step in report["steps"] if step["name"] == "market_order_cleanup"
    )
    assert cleanup["status"] == "PASS"
    assert cleanup["metadata"]["terminal_state_verified"] is True
    assert cleanup["metadata"]["detail_visibility_delayed"] is True


class _PartialLimitGateway(_StatefulMutationGateway):
    def place_order(self, payload):
        response = super().place_order(payload)
        self.created_status = "PARTIALLY_FILLED"
        self.created_filled = Decimal("0.25")
        return response


def test_limit_partial_fill_fails_reconciles_and_does_not_retry_cancel(monkeypatch):
    _base_env(monkeypatch)
    _arm_limit_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    fake = _PartialLimitGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert report["failure_code"] == "limit_order_filled_before_cancel"
    assert fake.cancel_calls == 1
    assert any(
        step["name"] == "limit_order_cleanup_position_reconciled"
        and step["status"] == "PASS"
        for step in report["steps"]
    )
    assert any(
        step["name"] == "limit_order_cleanup" and step["status"] == "PASS"
        for step in report["steps"]
    )


class _UnconfirmedCancelGateway(_StatefulMutationGateway):
    def cancel_order(self, client_order_id):
        self.cancel_calls += 1
        self.cancelled_ids.append(client_order_id)
        assert client_order_id == self.created_id
        return {
            "client_order_id": self.created_id,
            "order_id": "created-order-id",
        }


def test_cleanup_requires_terminal_detail_and_never_retries_ambiguous_cancel(monkeypatch):
    _base_env(monkeypatch)
    _arm_limit_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    fake = _UnconfirmedCancelGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert fake.cancel_calls == 1
    cleanup = next(
        step for step in report["steps"] if step["name"] == "limit_order_cleanup"
    )
    assert cleanup["status"] == "FAIL"
    assert cleanup["metadata"]["failure_code"] == (
        "owned_order_terminal_state_not_verified"
    )


class _MalformedPlaceResponseGateway(_StatefulMutationGateway):
    def __init__(self, account_id):
        super().__init__(account_id)
        self.open_calls = 0

    def get_open_orders(self, page_size=20, last_client_order_id=None):
        self.open_calls += 1
        return super().get_open_orders(page_size, last_client_order_id)

    def place_order(self, payload):
        super().place_order(payload)
        return {"client_order_id": "different-id", "order_id": "created-order-id"}


def test_cleanup_uses_exact_detail_instead_of_first_open_orders_page(monkeypatch):
    _base_env(monkeypatch)
    _arm_limit_mutation(monkeypatch)
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    config = live.load_harness_config(live.LIMIT_CANCEL_MODE)
    fake = _MalformedPlaceResponseGateway(config.account_id)

    report = live.run_harness(config, gateway_factory=lambda _config, _token_dir: fake)

    assert report["status"] == "FAIL"
    assert report["failure_code"] == "place_response_not_correlated"
    assert fake.open_calls == 1  # Initial read only; cleanup uses exact detail.
    assert fake.cancel_calls == 1
    assert any(
        step["name"] == "limit_order_cleanup" and step["status"] == "PASS"
        for step in report["steps"]
    )
