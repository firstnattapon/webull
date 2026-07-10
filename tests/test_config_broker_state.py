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
    }
    assert all(value.startswith("ok") for value in checks.values())


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
    quote = {"data": [{"symbol": "SMR", "lastPrice": "11.25"}]}

    assert broker.WebullBroker._extract_quantity(response, "SMR") == 4.5
    assert broker.WebullBroker._extract_last_price(quote, "SMR") == 11.25
    assert broker.WebullBroker._extract_quantity(response, "AAPL") == 0.0


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


@pytest.fixture
def fake_sdk_exceptions(monkeypatch):
    """Install a stub ``webull.core.exception.exceptions`` module.

    Mirrors the real SDK: ``ServerException`` carries ``http_status`` and is a
    plain ``Exception`` (NOT a broker error), which is exactly why untranslated
    SDK errors used to bypass retry and surface as generic 500s.
    """
    import sys
    from types import ModuleType

    class ServerException(Exception):
        def __init__(self, code, msg="", http_status=None, request_id=None):
            super().__init__()
            self.error_code = code
            self.error_msg = msg
            self.http_status = http_status
            self.request_id = request_id

        def __str__(self):
            return "HTTP Status: %s, Code: %s, Msg: %s, RequestID: %s" % (
                self.http_status, self.error_code, self.error_msg, self.request_id,
            )

    class ClientException(Exception):
        def __init__(self, code, msg=""):
            super().__init__()
            self.error_code = code
            self.error_msg = msg

        def __str__(self):
            return "%s %s" % (self.error_code, self.error_msg)

    exceptions_module = ModuleType("webull.core.exception.exceptions")
    exceptions_module.ServerException = ServerException
    exceptions_module.ClientException = ClientException

    for name in (
        "webull",
        "webull.core",
        "webull.core.exception",
        "webull.core.exception.exceptions",
    ):
        monkeypatch.setitem(
            sys.modules,
            name,
            exceptions_module if name.endswith("exceptions") else ModuleType(name),
        )
    return exceptions_module


def test_sdk_server_exception_translates_to_retryable_http_error(fake_sdk_exceptions):
    def gateway_timeout():
        raise fake_sdk_exceptions.ServerException(
            "GATEWAY_TIMEOUT", "", http_status=504, request_id="req-1",
        )

    with pytest.raises(broker.BrokerHTTPError) as excinfo:
        broker._call_sdk(gateway_timeout)

    assert excinfo.value.status_code == 504
    assert "GATEWAY_TIMEOUT" in excinfo.value.body


def test_sdk_server_exception_without_status_defaults_to_502(fake_sdk_exceptions):
    def unknown_failure():
        raise fake_sdk_exceptions.ServerException("MYSTERY", http_status=None)

    with pytest.raises(broker.BrokerHTTPError) as excinfo:
        broker._call_sdk(unknown_failure)

    assert excinfo.value.status_code == 502


def test_sdk_client_exception_translates_to_connection_error(fake_sdk_exceptions):
    def network_failure():
        raise fake_sdk_exceptions.ClientException("SDK_HTTP_ERROR", "boom")

    with pytest.raises(broker.BrokerConnectionError):
        broker._call_sdk(network_failure)


def test_retry_recovers_from_sdk_gateway_timeout(fake_sdk_exceptions):
    """End-to-end: a transient 504 ServerException is retried and succeeds."""
    calls = 0

    @broker._retry()
    def flaky_fetch():
        nonlocal calls
        calls += 1
        if calls == 1:
            return broker._call_sdk(
                Mock(side_effect=fake_sdk_exceptions.ServerException(
                    "GATEWAY_TIMEOUT", "", http_status=504, request_id="req-2",
                ))
            )
        return "ok"

    with patch("broker.time.sleep"):
        assert flaky_fetch() == "ok"
    assert calls == 2


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


def test_release_step_rolls_back_failed_reservation():
    # Step 2 was reserved (pointer moved to 3) but execution failed.
    document = FakeDocument(FakeSnapshot({"dna_step": 3}))
    db = FakeDb(document)

    with patch("state._get_firestore", return_value=(db, FakeFirestore)):
        released = state.release_step("p", "c", "d", reserved_step=2)

    assert released is True
    assert db.txn.writes[0][1]["dna_step"] == 2
    assert db.txn.writes[0][1]["last_release_reason"] == "EXECUTION_FAILED"
    assert db.txn.writes[0][2] is True  # merge=True


def test_release_step_leaves_pointer_when_another_invocation_advanced():
    # Another instance already reserved step 3 (pointer at 4): rolling back
    # to 2 would clobber its reservation, so the release must be a no-op.
    document = FakeDocument(FakeSnapshot({"dna_step": 4}))
    db = FakeDb(document)

    with patch("state._get_firestore", return_value=(db, FakeFirestore)):
        released = state.release_step("p", "c", "d", reserved_step=2)

    assert released is False
    assert db.txn.writes == []


def test_release_step_never_raises():
    with patch("state._get_firestore", side_effect=RuntimeError("firestore down")):
        assert state.release_step("p", "c", "d", reserved_step=0) is False
