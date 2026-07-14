from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

import state


class FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = copy.deepcopy(data) if data is not None else None

    def to_dict(self):
        return copy.deepcopy(self._data)


class FakeDocument:
    def __init__(self, db, collection_name, document_id):
        self.db = db
        self.collection_name = collection_name
        self.document_id = document_id

    @property
    def key(self):
        return self.collection_name, self.document_id

    def get(self, transaction=None):
        return FakeSnapshot(self.db.documents.get(self.key))


class FakeCollection:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def document(self, document_id):
        return FakeDocument(self.db, self.name, document_id)


class FakeTransaction:
    def __init__(self, db, firestore_module):
        self.db = db
        self.firestore_module = firestore_module
        self.writes = []

    def set(self, ref, payload, merge=False):
        self.writes.append((ref, copy.deepcopy(payload), merge))

    def commit(self):
        for ref, payload, merge in self.writes:
            current = copy.deepcopy(self.db.documents.get(ref.key, {})) if merge else {}
            for key, value in payload.items():
                if value is self.firestore_module.DELETE_FIELD:
                    current.pop(key, None)
                else:
                    current[key] = copy.deepcopy(value)
            self.db.documents[ref.key] = current


class FakeDb:
    def __init__(self, firestore_module):
        self.firestore_module = firestore_module
        self.documents = {}
        self.transactions = []

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self):
        transaction = FakeTransaction(self, self.firestore_module)
        self.transactions.append(transaction)
        return transaction


class FakeDeleteField:
    def __deepcopy__(self, memo):
        return self


class FakeFirestore:
    SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
    DELETE_FIELD = FakeDeleteField()

    @staticmethod
    def transactional(function):
        def wrapper(transaction, *args, **kwargs):
            result = function(transaction, *args, **kwargs)
            transaction.commit()
            return result

        return wrapper


@pytest.fixture
def fake_firestore():
    db = FakeDb(FakeFirestore)
    with patch("state._get_firestore", return_value=(db, FakeFirestore)):
        yield db


def write_lifecycle(client_order_id, payload):
    return state.write_order_lifecycle(
        project_id="project",
        trade_collection="trades",
        state_collection="strategy_state",
        strategy_id="strategy",
        symbol="smr",
        state_document="strategy_SMR",
        client_order_id=client_order_id,
        payload=payload,
    )


def test_lifecycle_updates_merge_into_one_deterministic_document(fake_firestore):
    first_id = write_lifecycle(
        "client-order-1",
        {
            "status": "SUBMITTED",
            "side": "SELL",
            "order_quantity": 0.25,
            "position_before": 4.773,
            "raw_response": {"order_id": "webull-order-1"},
        },
    )
    second_id = write_lifecycle(
        "client-order-1",
        {
            "status": "PARTIAL_FILLED",
            "filled_quantity": 0.1,
        },
    )

    assert first_id == second_id == state._order_lifecycle_document_id(
        "client-order-1"
    )
    trade_documents = {
        key: value
        for key, value in fake_firestore.documents.items()
        if key[0] == "trades"
    }
    assert list(trade_documents) == [("trades", first_id)]
    lifecycle = trade_documents[("trades", first_id)]
    assert lifecycle["status"] == "PARTIAL_FILLED"
    assert lifecycle["side"] == "SELL"
    assert lifecycle["filled_quantity"] == 0.1
    assert lifecycle["created_at"] == FakeFirestore.SERVER_TIMESTAMP
    assert lifecycle["updated_at"] == FakeFirestore.SERVER_TIMESTAMP

    first_log_write = fake_firestore.transactions[0].writes[0]
    second_log_write = fake_firestore.transactions[1].writes[0]
    assert first_log_write[2] is True
    assert second_log_write[2] is True
    assert "created_at" in first_log_write[1]
    assert "created_at" not in second_log_write[1]

    strategy_state = fake_firestore.documents[("strategy_state", "strategy_SMR")]
    assert strategy_state["last_status"] == "PARTIAL_FILLED"
    assert strategy_state["pending_order"] == {
        "client_order_id": "client-order-1",
        "status": "PARTIAL_FILLED",
        "strategy_id": "strategy",
        "symbol": "SMR",
        "trade_collection": "trades",
        "state_document": "strategy_SMR",
        "lifecycle_document_id": first_id,
        "updated_at": FakeFirestore.SERVER_TIMESTAMP,
        "side": "SELL",
        "order_quantity": 0.25,
        "filled_quantity": 0.1,
        "position_before": 4.773,
    }


def test_read_pending_order_returns_detached_copy_or_none(fake_firestore):
    assert state.read_pending_order(
        "project", "strategy_state", "missing"
    ) is None

    write_lifecycle("client-order-2", {"status": "SUBMITTED"})
    first = state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    )
    assert first is not None
    first["status"] = "MUTATED_BY_CALLER"

    second = state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    )
    assert second is not None
    assert second["status"] == "SUBMITTED"


def test_pending_position_reconcile_cycle_is_mirrored_to_state(fake_firestore):
    write_lifecycle(
        "client-order-cycle",
        {
            "status": "ORDER_FILLED_POSITION_PENDING",
            "position_reconcile_cycles": 2,
        },
    )

    pending = state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    )

    assert pending is not None
    assert pending["position_reconcile_cycles"] == 2


@pytest.mark.parametrize(
    "terminal_status",
    [
        "FILLED",
        "ORDER_FILLED_POSITION_UNAVAILABLE",
        "ORDER_FILLED_POSITION_UNCONFIRMED",
        "ORDER_PARTIAL_POSITION_UNAVAILABLE",
        "ORDER_PARTIAL_POSITION_UNCONFIRMED",
    ],
)
def test_terminal_update_atomically_clears_pending_order(
    fake_firestore, terminal_status
):
    lifecycle_id = write_lifecycle(
        "client-order-3",
        {"status": "SUBMITTED", "order_id": "webull-order-3"},
    )
    write_lifecycle(
        "client-order-3",
        {"status": terminal_status, "filled_quantity": 1.0},
    )

    terminal_transaction = fake_firestore.transactions[-1]
    assert len(terminal_transaction.writes) == 2
    state_write = terminal_transaction.writes[1][1]
    assert state_write["pending_order"] is FakeFirestore.DELETE_FIELD

    lifecycle = fake_firestore.documents[("trades", lifecycle_id)]
    strategy_state = fake_firestore.documents[("strategy_state", "strategy_SMR")]
    assert lifecycle["status"] == terminal_status
    assert strategy_state["last_status"] == terminal_status
    assert "pending_order" not in strategy_state
    assert state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    ) is None


@pytest.mark.parametrize(
    ("client_order_id", "payload", "message"),
    [
        ("", {"status": "SUBMITTED"}, "client_order_id"),
        (" surrounded ", {"status": "SUBMITTED"}, "surrounding whitespace"),
        ("x" * 33, {"status": "SUBMITTED"}, "1-32"),
        ("order-4", {}, "non-empty dict"),
        ("order-4", {"status": "bad/status"}, "status is invalid"),
        (
            "order-4",
            {"status": "SUBMITTED", "client_order_id": "different"},
            "does not match",
        ),
    ],
)
def test_lifecycle_rejects_invalid_identifiers_and_payloads(
    fake_firestore,
    client_order_id,
    payload,
    message,
):
    with pytest.raises(ValueError, match=message):
        write_lifecycle(client_order_id, payload)
    assert fake_firestore.documents == {}


@pytest.mark.parametrize(
    "unsafe_payload",
    [
        {"status": "SUBMITTED", "app_secret": "must-not-persist"},
        {"status": "SUBMITTED", "appSecret": "must-not-persist"},
        {"status": "SUBMITTED", "access_token": "must-not-persist"},
        {
            "status": "SUBMITTED",
            "raw_response": {
                "request_headers": {"x-signature": "must-not-persist"}
            },
        },
        {"status": "SUBMITTED", "account_id": "must-not-persist"},
        {"status": "SUBMITTED", "accountId": "must-not-persist"},
    ],
)
def test_lifecycle_never_persists_credentials_or_signed_request_metadata(
    fake_firestore,
    unsafe_payload,
):
    with pytest.raises(ValueError, match="credentials or signed request metadata"):
        write_lifecycle("client-order-5", unsafe_payload)
    assert fake_firestore.documents == {}


def test_corrupt_pending_order_is_reported(fake_firestore):
    fake_firestore.documents[("strategy_state", "strategy_SMR")] = {
        "pending_order": "not-a-map"
    }

    with pytest.raises(state.LifecycleReadError, match="must be a map"):
        state.read_pending_order("project", "strategy_state", "strategy_SMR")


def test_second_nonterminal_order_is_rejected_before_overwriting_pending(fake_firestore):
    write_lifecycle("first-order", {"status": "ORDER_CREATED"})

    with pytest.raises(state.LifecycleConflictError, match="Another pending order"):
        write_lifecycle("second-order", {"status": "ORDER_CREATED"})

    pending = state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    )
    assert pending is not None
    assert pending["client_order_id"] == "first-order"
    assert (
        "trades",
        state._order_lifecycle_document_id("second-order"),
    ) not in fake_firestore.documents


def test_late_terminal_update_does_not_clear_newer_pending_order(fake_firestore):
    write_lifecycle("old-order", {"status": "ORDER_CREATED"})
    write_lifecycle("old-order", {"status": "ORDER_REJECTED"})
    write_lifecycle("new-order", {"status": "ORDER_CREATED"})

    write_lifecycle("old-order", {"status": "ORDER_NOT_FILLED"})

    pending = state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    )
    assert pending is not None
    assert pending["client_order_id"] == "new-order"


def test_terminal_lifecycle_absorbs_stale_nonterminal_and_smaller_fill(
    fake_firestore,
):
    lifecycle_id = write_lifecycle(
        "terminal-order",
        {"status": "ORDER_SUBMITTED", "filled_quantity": 0.0},
    )
    write_lifecycle(
        "terminal-order",
        {
            "status": "ORDER_FILLED",
            "filled_quantity": 1.0,
            "position_reconciled": True,
            "position_after": 6.0,
            "quantity": 6.0,
        },
    )

    write_lifecycle(
        "terminal-order",
        {
            "status": "ORDER_SUBMITTED",
            "filled_quantity": 0.25,
            "position_reconciled": False,
            "position_after": 5.25,
            "quantity": 5.25,
        },
    )

    lifecycle = fake_firestore.documents[("trades", lifecycle_id)]
    assert lifecycle["status"] == "ORDER_FILLED"
    assert lifecycle["filled_quantity"] == 1.0
    assert lifecycle["position_reconciled"] is True
    assert lifecycle["position_after"] == 6.0
    assert lifecycle["quantity"] == 6.0
    assert fake_firestore.transactions[-1].writes == []
    assert state.read_pending_order(
        "project", "strategy_state", "strategy_SMR"
    ) is None


def test_verified_observation_absorbs_stale_same_fill_update(fake_firestore):
    lifecycle_id = write_lifecycle(
        "verified-order",
        {
            "status": "ORDER_PARTIAL_FILLED",
            "filled_quantity": 0.5,
            "position_reconciled": True,
            "position_after": 5.5,
            "quantity": 5.5,
            "market_state": {"quantity": 5.5, "last_price": 100.0},
        },
    )

    write_lifecycle(
        "verified-order",
        {
            "status": "ORDER_PARTIAL_FILLED",
            "filled_quantity": 0.5,
            "position_reconciled": False,
            "position_after": 5.0,
            "quantity": 5.0,
            "market_state": {"quantity": 5.0, "last_price": 100.0},
        },
    )

    lifecycle = fake_firestore.documents[("trades", lifecycle_id)]
    assert lifecycle["status"] == "ORDER_PARTIAL_FILLED"
    assert lifecycle["filled_quantity"] == 0.5
    assert lifecycle["position_reconciled"] is True
    assert lifecycle["position_after"] == 5.5
    assert lifecycle["quantity"] == 5.5
    assert lifecycle["market_state"]["quantity"] == 5.5
    assert fake_firestore.transactions[-1].writes == []
