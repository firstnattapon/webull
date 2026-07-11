from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
from flask import Flask

import main
from broker import BrokerHTTPError, MarketState, OrderResult
from config import AppConfig
from state import StepReservation


@pytest.fixture
def app():
    return Flask(__name__)


@pytest.fixture
def app_config():
    return AppConfig(
        project_id="project",
        strategy_id="strategy",
        symbol="SMR",
        fix_c=1000.0,
        p0=50.0,
        diff=100.0,
        dna_code="bypass:3",
        start_timestamp=0,
        firestore_state_collection="state",
        firestore_trade_collection="trades",
        firestore_state_document="strategy_SMR",
    )


def invoke(app, **patches):
    defaults = {
        "load_app_config": Mock(),
        "is_us_market_open": Mock(return_value=True),
        "_get_dna_array": Mock(return_value=np.array([1, 0, 1], dtype=np.int8)),
        "reserve_step": Mock(return_value=StepReservation(0, 1)),
        "flush_trade_logs": Mock(),
    }
    defaults.update(patches)
    patchers = [patch.object(main, name, value) for name, value in defaults.items()]
    for item in patchers:
        item.start()
    try:
        with app.test_request_context("/", method="POST"):
            response, status_code = main.rebalance_trigger(SimpleNamespace(path="/", args={}))
            return response.get_json(), status_code, defaults
    finally:
        for item in reversed(patchers):
            item.stop()


def test_waiting_response_shape_is_preserved(app, app_config):
    waiting = AppConfig(**{**app_config.__dict__, "start_timestamp": 999})
    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=waiting),
        time=SimpleNamespace(time=lambda: 100),
    )

    assert (body, code) == ({"status": "PASS_WAITING_TO_START", "start_timestamp": 999}, 200)
    mocks["is_us_market_open"].assert_not_called()


def test_market_closed_response_shape_is_preserved(app, app_config):
    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        is_us_market_open=Mock(return_value=False),
    )

    assert (body, code) == ({"status": "PASS_MARKET_CLOSED"}, 200)
    mocks["reserve_step"].assert_not_called()


@pytest.mark.parametrize(
    ("reservation", "expected"),
    [
        (StepReservation(3, 0), {"status": "TIMELINE_ENDED", "dna_step": 3, "dna_length": 3}),
        (StepReservation(1, 0), {"status": "PASS_DNA_ZERO", "dna_step": 1, "dna_signal": 0}),
    ],
)
def test_dna_exit_response_shapes_are_preserved(app, app_config, reservation, expected):
    body, code, _ = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        reserve_step=Mock(return_value=reservation),
    )
    assert (body, code) == (expected, 200)


def test_threshold_response_and_log_payload_are_preserved(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(10.0, 100.0)
    log_trade = Mock()

    body, code, _ = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace()),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "PASS_THRESHOLD"
    assert body["dna_step"] == 0
    assert body["dna_signal"] == 1
    assert body["decision"]["action"] == "PASS"
    assert body["decision"]["order_qty"] == 0.0
    log_trade.assert_called_once()
    payload = log_trade.call_args.args[1]
    assert payload["status"] == "PASS_THRESHOLD"
    assert payload["last_price"] == 100.0
    assert payload["quantity"] == 10.0
    assert payload["market_state"] == {"quantity": 10.0, "last_price": 100.0}


def test_order_response_and_deterministic_client_id_are_preserved(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(5.0, 100.0)
    broker_instance.has_open_order.return_value = False
    broker_instance.place_market_order.return_value = OrderResult(
        client_order_id="id",
        order_id="order-1",
        status="SUBMITTED",
        preview=None,
        raw_response={"order_id": "order-1"},
    )
    log_trade = Mock()

    body, code, _ = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace()),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "OK"
    assert body["decision"]["action"] == "BUY"
    assert body["order"]["order_id"] == "order-1"
    payload = log_trade.call_args.args[1]
    assert payload["status"] == "ORDER_SUBMITTED"
    assert payload["last_price"] == 100.0
    assert payload["quantity"] == 5.0
    assert payload["order_result"]["order_id"] == "order-1"
    kwargs = broker_instance.place_market_order.call_args.kwargs
    assert kwargs["symbol"] == "SMR"
    assert kwargs["side"] == "BUY"
    assert kwargs["quantity"] == 5.0
    assert len(kwargs["client_order_id"]) == 32


def test_open_order_prevents_duplicate_rebalance_submission(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(5.0, 100.0)
    broker_instance.has_open_order.return_value = True
    log_trade = Mock()

    body, code, _ = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace()),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "PASS_OPEN_ORDER"
    assert body["decision"]["action"] == "BUY"
    broker_instance.place_market_order.assert_not_called()
    log_trade.assert_called_once()
    payload = log_trade.call_args.args[1]
    assert payload["status"] == "PASS_OPEN_ORDER"
    assert payload["last_price"] == 100.0


def test_broker_error_response_shape_is_preserved(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.side_effect = BrokerHTTPError(503, "down")

    body, code, _ = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace()),
        get_broker=Mock(return_value=broker_instance),
        _try_log_error=Mock(),
    )

    assert code == 502
    assert body == {
        "status": "BROKER_ERROR",
        "error_type": "BrokerHTTPError",
        "message": "Webull HTTP 503: down",
    }


def test_unexpected_error_response_shape_is_preserved(app):
    body, code, _ = invoke(
        app,
        load_app_config=Mock(side_effect=ValueError("bad config")),
        _try_log_error=Mock(),
    )

    assert (body, code) == ({
        "status": "ERROR",
        "error_type": "ValueError",
        "message": "bad config",
    }, 500)


def test_health_request_detection_contract():
    assert main._is_health_request(SimpleNamespace(path="/health/", args={}))
    assert main._is_health_request(SimpleNamespace(path="/", args={"health": "true"}))
    assert not main._is_health_request(SimpleNamespace(path="/", args={}))
