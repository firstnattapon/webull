from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest
from flask import Flask

import main
from broker import (
    BrokerError,
    BrokerHTTPError,
    MarketState,
    OrderResult,
    OrderStatus,
    OrderSubmissionUnknownError,
    WebullBroker,
)
from config import AppConfig
from state import StepReservation, TERMINAL_ORDER_LIFECYCLE_STATUSES


def _filled_status(client_order_id: str = "id", filled: float = 5.0) -> OrderStatus:
    return OrderStatus(
        client_order_id=client_order_id,
        status="FILLED",
        filled_quantity=filled,
        raw_response={"status": "FILLED", "filled_quantity": filled},
    )


def _unfilled_status(client_order_id: str = "id", status: str = "CANCELLED") -> OrderStatus:
    return OrderStatus(
        client_order_id=client_order_id,
        status=status,
        filled_quantity=0.0,
        raw_response={"status": status, "filled_quantity": 0},
    )


def _pending_status(client_order_id: str = "id") -> OrderStatus:
    return OrderStatus(
        client_order_id=client_order_id,
        status="SUBMITTED",
        filled_quantity=0.0,
        raw_response={"status": "SUBMITTED", "filled_quantity": 0},
    )


def test_verify_fill_polls_pending_order_until_executed():
    broker_instance = Mock()
    broker_instance.get_order_status.side_effect = [
        _pending_status(),
        _filled_status(filled=0.5),
    ]

    with patch("main.time.sleep") as sleep:
        result = main._verify_fill(broker_instance, "id")

    assert result.is_filled is True
    assert result.filled_quantity == 0.5
    assert broker_instance.get_order_status.call_count == 2
    sleep.assert_called_once_with(main.FILL_VERIFICATION_INTERVAL_SECONDS)


def test_verify_fill_stops_after_bounded_pending_reads():
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _pending_status()

    with patch("main.time.sleep") as sleep:
        result = main._verify_fill(broker_instance, "id")

    assert result.status == "SUBMITTED"
    assert broker_instance.get_order_status.call_count == main.FILL_VERIFICATION_ATTEMPTS
    assert sleep.call_count == main.FILL_VERIFICATION_ATTEMPTS - 1


def test_verify_fill_does_not_abandon_active_partial_fill():
    broker_instance = Mock()
    partial = OrderStatus(
        client_order_id="id",
        status="PARTIAL_FILLED",
        filled_quantity=0.5,
        order_quantity=1.0,
        raw_response={"status": "PARTIAL_FILLED"},
    )
    broker_instance.get_order_status.side_effect = [partial, _filled_status(filled=1.0)]

    with patch("main.time.sleep") as sleep:
        result = main._verify_fill(broker_instance, "id")

    assert result.is_filled is True
    assert broker_instance.get_order_status.call_count == 2
    sleep.assert_called_once_with(main.FILL_VERIFICATION_INTERVAL_SECONDS)


def test_verify_fill_does_not_multiply_exhausted_broker_retries():
    broker_instance = Mock()
    broker_instance.get_order_status.side_effect = BrokerError(
        "order detail unavailable"
    )

    with patch("main.time.sleep") as sleep:
        result = main._verify_fill(broker_instance, "id")

    assert result is None
    broker_instance.get_order_status.assert_called_once_with("id")
    sleep.assert_not_called()


def test_position_reconciliation_waits_for_positions_snapshot_to_update():
    broker_instance = Mock()
    broker_instance.get_position_quantity.side_effect = [4.773, 4.273]

    with patch("main.time.sleep") as sleep:
        result = main._reconcile_position(
            broker_instance,
            symbol="AAPL",
            side="SELL",
            position_before=4.773,
            filled_quantity=0.5,
        )

    assert result["position_before"] == 4.773
    assert result["expected_position_after"] == pytest.approx(4.273)
    assert result["position_after"] == 4.273
    assert result["position_delta"] == pytest.approx(-0.5)
    assert result["position_reconciled"] is True
    assert result["position_sync_status"] == "CONFIRMED"
    assert broker_instance.get_position_quantity.call_count == 2
    sleep.assert_called_once_with(main.POSITION_RECONCILIATION_INTERVAL_SECONDS)


def test_position_reconciliation_failure_is_best_effort():
    broker_instance = Mock()
    broker_instance.get_position_quantity.side_effect = BrokerError("positions unavailable")

    with patch("main.time.sleep") as sleep:
        result = main._reconcile_position(
            broker_instance,
            symbol="AAPL",
            side="BUY",
            position_before=4.0,
            filled_quantity=1.0,
        )

    assert result["position_after"] is None
    assert result["position_reconciled"] is False
    assert result["position_reconcile_error"] == "BrokerError"
    assert result["position_sync_status"] == "UNAVAILABLE"
    broker_instance.get_position_quantity.assert_called_once_with("AAPL")
    sleep.assert_not_called()


def test_position_reconciliation_rejects_unchanged_point_zero_one_fill():
    broker_instance = Mock()
    broker_instance.get_position_quantity.return_value = 5.0

    with patch("main.time.sleep") as sleep:
        result = main._reconcile_position(
            broker_instance,
            symbol="SMR",
            side="BUY",
            position_before=5.0,
            filled_quantity=0.01,
        )

    assert result["expected_position_after"] == pytest.approx(5.01)
    assert result["position_after"] == 5.0
    assert result["position_delta"] == 0.0
    assert result["position_reconciled"] is False
    assert result["position_sync_status"] == "MISMATCH"
    assert (
        broker_instance.get_position_quantity.call_count
        == main.POSITION_RECONCILIATION_ATTEMPTS
    )
    assert sleep.call_count == main.POSITION_RECONCILIATION_ATTEMPTS - 1


def _pending_order(**overrides):
    values = {
        "client_order_id": "pending-id",
        "status": "ORDER_SUBMITTED",
        "side": "BUY",
        "order_quantity": 1.0,
        "position_before": 5.0,
        "last_price": 100.0,
        "broker_environment": "prod",
        "broker_endpoint": "api.webull.co.th",
        "account_fingerprint": "account-fp",
        "order_id": "order-1",
        "strategy_id": "strategy",
        "symbol": "SMR",
        "trade_collection": "trades",
        "state_document": "strategy_SMR",
    }
    values.update(overrides)
    return values


def _broker_config(**overrides):
    values = {
        "environment_label": "prod",
        "endpoint": "api.webull.co.th",
        "is_production": True,
        "account_fingerprint": "account-fp",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_pending_terminal_fill_updates_same_lifecycle_and_position(app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _filled_status(
        client_order_id="pending-id", filled=1.0
    )
    broker_instance.get_position_quantity.return_value = 6.0
    write_lifecycle = Mock()

    with patch.object(main, "_write_lifecycle", write_lifecycle):
        result = main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(),
        )

    assert result["terminal"] is True
    assert result["outcome"] == "ORDER_FILLED"
    payload = write_lifecycle.call_args.args[2]
    assert payload["status"] == "ORDER_FILLED"
    assert payload["quantity"] == 6.0
    assert payload["position_reconciled"] is True
    assert "raw_response" not in payload["order_status"]


def test_pending_filled_order_with_position_mismatch_stays_pending(app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _filled_status(
        client_order_id="pending-id", filled=1.0
    )
    broker_instance.get_position_quantity.return_value = 5.0
    write_lifecycle = Mock()

    with (
        patch.object(main, "_write_lifecycle", write_lifecycle),
        patch("main.time.sleep"),
    ):
        result = main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(),
        )

    assert result["terminal"] is False
    assert result["status"] == "ORDER_FILLED_POSITION_PENDING"
    assert result["position_after"] == 5.0
    assert result["position_reconciled"] is False
    payload = write_lifecycle.call_args.args[2]
    assert payload["status"] == "ORDER_FILLED_POSITION_PENDING"
    assert payload["position_reconciled"] is False
    assert payload["position_reconcile_cycles"] == 1
    assert (
        broker_instance.get_position_quantity.call_count
        == main.POSITION_RECONCILIATION_ATTEMPTS
    )


def test_pending_filled_order_keeps_lock_when_positions_unavailable(app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _filled_status(
        client_order_id="pending-id", filled=1.0
    )
    broker_instance.get_position_quantity.side_effect = BrokerError(
        "positions unavailable"
    )
    write_lifecycle = Mock()

    with patch.object(main, "_write_lifecycle", write_lifecycle):
        result = main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(status="ORDER_FILLED_POSITION_PENDING"),
        )

    assert result["terminal"] is False
    assert result["status"] == "ORDER_FILLED_POSITION_PENDING"
    assert result["position_sync_status"] == "UNAVAILABLE"
    payload = write_lifecycle.call_args.args[2]
    assert payload["position_reconcile_error"] == "BrokerError"
    assert payload["position_reconcile_cycles"] == 1
    broker_instance.get_position_quantity.assert_called_once_with("SMR")


def test_pending_filled_order_alerts_but_keeps_lock_after_stale_cycles(app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _filled_status(
        client_order_id="pending-id", filled=1.0
    )
    broker_instance.get_position_quantity.return_value = 5.0
    write_lifecycle = Mock()

    with (
        patch.object(main, "_write_lifecycle", write_lifecycle),
        patch("main.time.sleep"),
    ):
        result = main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(
                status="ORDER_FILLED_POSITION_PENDING",
                position_reconcile_cycles=(
                    main.POSITION_RECONCILIATION_ALERT_CYCLES - 1
                ),
            ),
        )

    assert result["terminal"] is False
    assert result["status"] == "ORDER_FILLED_POSITION_PENDING"
    assert result["position_sync_status"] == "MISMATCH"
    assert result["manual_resolution_required"] is True
    payload = write_lifecycle.call_args.args[2]
    assert (
        payload["position_reconcile_cycles"]
        == main.POSITION_RECONCILIATION_ALERT_CYCLES
    )
    assert payload["manual_resolution_required"] is True


def test_pending_partial_fill_remains_durable_until_terminal(app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = OrderStatus(
        client_order_id="pending-id",
        status="PARTIAL_FILLED",
        filled_quantity=0.5,
        order_quantity=1.0,
        raw_response={"must": "stay in memory"},
    )
    broker_instance.get_position_quantity.return_value = 5.5
    write_lifecycle = Mock()

    with patch.object(main, "_write_lifecycle", write_lifecycle):
        result = main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(),
        )

    assert result["terminal"] is False
    assert result["outcome"] == "ORDER_PARTIAL_FILLED"
    assert write_lifecycle.call_args.args[2]["status"] == "ORDER_PARTIAL_FILLED"


def test_pending_order_is_never_read_through_wrong_environment_or_account(app_config):
    broker_instance = Mock()

    with pytest.raises(BrokerError, match="environment"):
        main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(environment_label="uat"),
            broker_instance,
            _pending_order(),
        )
    broker_instance.get_order_status.assert_not_called()

    with pytest.raises(BrokerError, match="account"):
        main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(account_fingerprint="different"),
            broker_instance,
            _pending_order(),
        )
    broker_instance.get_order_status.assert_not_called()


@pytest.mark.parametrize(
    ("identity_name", "drifted_value"),
    [
        ("strategy_id", "different-strategy"),
        ("symbol", "AAPL"),
        ("trade_collection", "other-trades"),
        ("state_document", "other-state"),
        ("broker_endpoint", "th-api.uat.webullbroker.com"),
    ],
)
def test_pending_order_identity_drift_is_rejected_before_broker_read(
    app_config,
    identity_name,
    drifted_value,
):
    broker_instance = Mock()

    with pytest.raises(BrokerError, match=identity_name):
        main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(**{identity_name: drifted_value}),
        )

    broker_instance.get_order_status.assert_not_called()
    broker_instance.lookup_order_status.assert_not_called()


@pytest.mark.parametrize("pending_status", ["ORDER_CREATED", "ORDER_SUBMIT_UNKNOWN"])
def test_unresolved_pre_submit_lifecycle_uses_lookup_and_requires_manual_review(
    app_config,
    pending_status,
):
    broker_instance = Mock()
    broker_instance.lookup_order_status.return_value = None
    write_lifecycle = Mock()

    with patch.object(main, "_write_lifecycle", write_lifecycle):
        result = main._reconcile_pending_lifecycle(
            app_config,
            _broker_config(),
            broker_instance,
            _pending_order(status=pending_status, order_id=None),
        )

    broker_instance.lookup_order_status.assert_called_once_with("pending-id")
    broker_instance.get_order_status.assert_not_called()
    assert result["terminal"] is False
    assert result["status"] == "ORDER_SUBMIT_UNKNOWN_NOT_FOUND"
    assert result["manual_resolution_required"] is True
    assert result["not_found_attempts"] == 1
    payload = write_lifecycle.call_args.args[2]
    assert payload["status"] == "ORDER_SUBMIT_UNKNOWN_NOT_FOUND"
    assert payload["manual_resolution_required"] is True


def test_ambiguous_lookup_checks_open_orders_and_history_before_returning_none():
    broker_instance = object.__new__(WebullBroker)
    broker_instance.get_order_detail = Mock(
        side_effect=BrokerHTTPError(404, "order not found")
    )
    broker_instance.get_open_orders = Mock(return_value=[])
    broker_instance.get_order_history = Mock(return_value=[])

    result = broker_instance.lookup_order_status("pending-id")

    assert result is None
    broker_instance.get_order_detail.assert_called_once_with("pending-id")
    broker_instance.get_open_orders.assert_called_once()
    broker_instance.get_order_history.assert_called_once()


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
        "read_pending_order": Mock(return_value=None),
        "_write_lifecycle": Mock(return_value="order-doc"),
        "flush_trade_logs": Mock(),
        "FILL_VERIFICATION_INTERVAL_SECONDS": 0,
        "POSITION_RECONCILIATION_INTERVAL_SECONDS": 0,
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


def test_known_pre_submit_failure_closes_intent_as_terminal(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(5.0, 100.0)
    broker_instance.has_open_order.return_value = False
    broker_instance.place_market_order.side_effect = BrokerError(
        "preview failed before submission"
    )

    _, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=_broker_config()),
        get_broker=Mock(return_value=broker_instance),
        _try_log_error=Mock(),
    )

    statuses = [
        call.args[2]["status"]
        for call in mocks["_write_lifecycle"].call_args_list
    ]
    assert code == 502
    assert statuses == ["ORDER_CREATED", "ORDER_PRE_SUBMIT_FAILED"]
    assert statuses[-1] in TERMINAL_ORDER_LIFECYCLE_STATUSES


def test_unknown_submission_outcome_keeps_intent_pending(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(5.0, 100.0)
    broker_instance.has_open_order.return_value = False
    broker_instance.place_market_order.side_effect = OrderSubmissionUnknownError(
        "submission outcome unknown"
    )

    _, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=_broker_config()),
        get_broker=Mock(return_value=broker_instance),
        _try_log_error=Mock(),
    )

    statuses = [
        call.args[2]["status"]
        for call in mocks["_write_lifecycle"].call_args_list
    ]
    assert code == 502
    assert statuses == ["ORDER_CREATED", "ORDER_SUBMIT_UNKNOWN"]
    assert statuses[-1] not in TERMINAL_ORDER_LIFECYCLE_STATUSES


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


def test_pending_order_reconciles_before_market_and_dna_gates(app, app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _unfilled_status(
        client_order_id="pending-id",
        status="CANCELLED",
    )

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        read_pending_order=Mock(return_value=_pending_order()),
        load_broker_config=Mock(return_value=_broker_config()),
        get_broker=Mock(return_value=broker_instance),
        is_us_market_open=Mock(return_value=False),
    )

    assert code == 200
    assert body["status"] == "ORDER_RECONCILED"
    assert body["order_lifecycle"]["outcome"] == "ORDER_NOT_FILLED"
    mocks["is_us_market_open"].assert_not_called()
    mocks["reserve_step"].assert_not_called()


def test_unavailable_filled_position_blocks_market_and_dna_gates(app, app_config):
    broker_instance = Mock()
    broker_instance.get_order_status.return_value = _filled_status(
        client_order_id="pending-id",
        filled=1.0,
    )
    broker_instance.get_position_quantity.side_effect = BrokerError(
        "positions unavailable"
    )

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        read_pending_order=Mock(return_value=_pending_order(
            status="ORDER_FILLED_POSITION_PENDING"
        )),
        load_broker_config=Mock(return_value=_broker_config()),
        get_broker=Mock(return_value=broker_instance),
        is_us_market_open=Mock(return_value=True),
    )

    assert code == 200
    assert body["status"] == "ORDER_PENDING"
    assert body["order_lifecycle"]["status"] == "ORDER_FILLED_POSITION_PENDING"
    assert body["order_lifecycle"]["position_sync_status"] == "UNAVAILABLE"
    mocks["is_us_market_open"].assert_not_called()
    mocks["reserve_step"].assert_not_called()


@pytest.mark.parametrize(
    ("reservation", "expected"),
    [
        (StepReservation(3, 0), {"status": "TIMELINE_ENDED", "dna_step": 3, "dna_length": 3}),
        (StepReservation(1, 0), {"status": "PASS_DNA_ZERO", "dna_step": 1, "dna_signal": 0}),
    ],
)
def test_dna_exit_response_shapes_are_preserved(app, app_config, reservation, expected):
    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        reserve_step=Mock(return_value=reservation),
    )
    assert (body, code) == (expected, 200)


def test_duplicate_slot_tick_passes_without_trading(app, app_config):
    """A Force run inside an already-reserved slot must not touch the broker."""
    get_broker = Mock()
    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        reserve_step=Mock(return_value=StepReservation(2, 0, duplicate=True)),
        get_broker=get_broker,
    )

    assert (body, code) == ({"status": "PASS_DUPLICATE_TICK", "dna_step": 2}, 200)
    get_broker.assert_not_called()


def test_reserve_step_receives_configured_slot_width(app, app_config):
    slotted = AppConfig(**{**app_config.__dict__, "schedule_slot_seconds": 600})
    _, _, mocks = invoke(
        app,
        load_app_config=Mock(return_value=slotted),
        reserve_step=Mock(return_value=StepReservation(1, 0)),
    )

    assert mocks["reserve_step"].call_args.kwargs["slot_seconds"] == 600


def test_threshold_response_and_log_payload_are_preserved(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(10.0, 100.0)
    log_trade = Mock()

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
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
    # Webull confirms the order actually executed.
    broker_instance.get_order_status.return_value = _filled_status(filled=5.0)
    # The Manual-style Positions read confirms BUY 5 moved 5 -> 10.
    broker_instance.get_position_quantity.return_value = 10.0
    log_trade = Mock()

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "OK"
    assert body["decision"]["action"] == "BUY"
    assert body["order"]["order_id"] == "order-1"
    payload = mocks["_write_lifecycle"].call_args.args[2]
    # A verified fill is logged as ORDER_FILLED, not a blind ORDER_SUBMITTED.
    assert payload["status"] == "ORDER_FILLED"
    assert payload["filled_quantity"] == 5.0
    assert payload["last_price"] == 100.0
    assert payload["quantity"] == 10.0
    assert payload["market_state"] == {"quantity": 10.0, "last_price": 100.0}
    assert payload["pre_order_market_state"] == {
        "quantity": 5.0, "last_price": 100.0,
    }
    assert payload["position_before"] == 5.0
    assert payload["position_after"] == 10.0
    assert payload["position_delta"] == 5.0
    assert payload["position_reconciled"] is True
    assert payload["order_result"]["order_id"] == "order-1"
    kwargs = broker_instance.place_market_order.call_args.kwargs
    assert kwargs["symbol"] == "SMR"
    assert kwargs["side"] == "BUY"
    assert kwargs["quantity"] == 5.0
    assert len(kwargs["client_order_id"]) == 32
    # The verified order is the one that was placed.
    broker_instance.get_order_status.assert_called_once_with(
        kwargs["client_order_id"]
    )
    broker_instance.get_position_quantity.assert_called_once_with("SMR")


def test_accepted_but_unfilled_order_is_logged_as_not_filled(app, app_config):
    """The reported bug: an order accepted with an id but never filled.

    Webull returns a real order id (accepted), yet the order is cancelled /
    expired with 0 filled, so the held quantity never moves. The log must call
    this ORDER_NOT_FILLED, not paint it as a submitted trade.
    """
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(4.773, 320.0)
    broker_instance.has_open_order.return_value = False
    broker_instance.place_market_order.return_value = OrderResult(
        client_order_id="id",
        order_id="order-1",
        status="SUBMITTED",
        preview=None,
        raw_response={"order_id": "order-1"},
    )
    broker_instance.get_order_status.return_value = _unfilled_status(status="CANCELLED")
    log_trade = Mock()

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "ORDER_NOT_FILLED"
    assert body["order_log_status"] == "ORDER_NOT_FILLED"
    payload = mocks["_write_lifecycle"].call_args.args[2]
    assert payload["status"] == "ORDER_NOT_FILLED"
    assert payload["filled_quantity"] == 0.0
    assert "did not move" in payload["not_filled_reason"]


def test_fill_verification_failure_falls_back_to_submitted(app, app_config):
    """If the fill can't be read back, the tick still succeeds (best-effort)."""
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
    broker_instance.get_order_status.side_effect = BrokerError("detail read failed")
    log_trade = Mock()

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "OK"
    payload = mocks["_write_lifecycle"].call_args.args[2]
    assert payload["status"] == "ORDER_SUBMITTED"
    assert payload["fill_verified"] is False


def test_unaccepted_order_is_logged_as_rejected_not_submitted(app, app_config):
    """Webull answered 200 but never booked the order: the log must say so.

    This is the reported bug — a SELL that shows in the log while the held
    quantity never moves. The handler must not paint it as ORDER_SUBMITTED.
    """
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(5.0, 100.0)
    broker_instance.has_open_order.return_value = False
    broker_instance.place_market_order.return_value = OrderResult(
        client_order_id="id",
        order_id=None,
        status="UNKNOWN",
        preview=None,
        raw_response={"msg": "rejected"},
        accepted=False,
        reason="rejected",
    )
    log_trade = Mock()

    body, code, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert code == 200
    assert body["status"] == "ORDER_REJECTED"
    payload = mocks["_write_lifecycle"].call_args.args[2]
    assert payload["status"] == "ORDER_REJECTED"
    assert payload["order_result"]["reason"] == "rejected"
    # A never-accepted order is not verified — there is no live order to read.
    broker_instance.get_order_status.assert_not_called()


def test_order_log_records_broker_environment(app, app_config):
    """A UAT sandbox no-op must be identifiable in the trade log."""
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
    # UAT accepts the order but never fills a real position.
    broker_instance.get_order_status.return_value = _unfilled_status(status="CANCELLED")
    log_trade = Mock()

    _, _, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="uat",
            endpoint="th-api.uat.webullbroker.com",
            is_production=False,
        )),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    payload = mocks["_write_lifecycle"].call_args.args[2]
    assert payload["broker_environment"] == "uat"
    assert payload["is_production"] is False
    # The accepted-but-unchanged-position symptom must be spelled out on UAT.
    assert "sandbox_note" in payload
    assert "shared test account" in payload["sandbox_note"]


def test_production_order_has_no_sandbox_note(app, app_config):
    """A production order must not carry the UAT sandbox note."""
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
    broker_instance.get_order_status.return_value = _filled_status(filled=5.0)
    broker_instance.get_position_quantity.return_value = 10.0
    log_trade = Mock()

    body, _, mocks = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
        get_broker=Mock(return_value=broker_instance),
        _log_trade=log_trade,
    )

    assert "sandbox_note" not in mocks["_write_lifecycle"].call_args.args[2]
    assert "sandbox_note" not in body


def test_open_order_prevents_duplicate_rebalance_submission(app, app_config):
    broker_instance = Mock()
    broker_instance.get_position_and_price.return_value = MarketState(5.0, 100.0)
    broker_instance.has_open_order.return_value = True
    log_trade = Mock()

    body, code, _ = invoke(
        app,
        load_app_config=Mock(return_value=app_config),
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
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
        load_broker_config=Mock(return_value=SimpleNamespace(
            environment_label="prod",
            endpoint="api.webull.co.th",
            is_production=True,
        )),
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
