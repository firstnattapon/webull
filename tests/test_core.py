from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest
import pytz

from dna_engine import (
    decode_dna,
    decode_number_stream,
    encode_dna,
    normalize_mutation_rate,
    parse_bypass_dna_code,
    parse_dna_spec,
)
from market_utils import is_us_market_open, to_new_york_time
from strategy import calculate_shannon_decision, generate_client_order_id


def test_number_stream_and_spec_preserve_known_dna_contract():
    encoded = "26021034252903219354832053493"

    assert decode_number_stream(encoded) == [60, 10, 425, 90, 219, 548, 205, 493]
    spec = parse_dna_spec(encoded)
    assert spec.length == 60
    assert spec.mutation_rate == 0.1
    assert spec.seeds == (425, 90, 219, 548, 205, 493)


def test_dna_decode_is_deterministic_and_keeps_first_signal_enabled():
    encoded = "26021034252903219354832053493"

    first = decode_dna(encoded)
    second = decode_dna(encoded)

    np.testing.assert_array_equal(first, second)
    assert first.dtype == np.int8
    assert len(first) == 60
    assert int(first[0]) == 1
    assert set(first.tolist()) <= {0, 1}
    assert first.tolist() == [
        1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1,
        1, 0, 1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0,
        0, 1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1,
        0, 1, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 1,
    ]


@pytest.mark.parametrize("encoded,length", [("bypass:4", 4), ("BYPASS:2", 2), ("[1, 3]", 3)])
def test_bypass_codes_return_all_ones(encoded, length):
    assert parse_bypass_dna_code(encoded) == length
    np.testing.assert_array_equal(decode_dna(encoded), np.ones(length, dtype=np.int8))


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0.0), (0.25, 0.25), (1, 1.0), (10, 0.1), (100, 1.0)],
)
def test_mutation_rate_normalization(value, expected):
    assert normalize_mutation_rate(value) == expected


@pytest.mark.parametrize("value", [-1, 101])
def test_mutation_rate_rejects_out_of_range_values(value):
    with pytest.raises(ValueError):
        normalize_mutation_rate(value)


def test_encode_decode_number_stream_round_trip():
    encoded = encode_dna(60, 10, [425, 290])
    assert decode_number_stream(encoded) == [60, 10, 425, 290]


@pytest.mark.parametrize(
    ("quantity", "price", "expected_action", "expected_qty", "expected_reason"),
    [
        (10.0, 100.0, "PASS", 0.0, "WITHIN_THRESHOLD"),
        (8.0, 100.0, "BUY", 2.0, "BELOW_TARGET"),
        (12.0, 100.0, "SELL", 2.0, "ABOVE_TARGET"),
    ],
)
def test_shannon_decision_actions(quantity, price, expected_action, expected_qty, expected_reason):
    decision = calculate_shannon_decision(
        quantity=quantity,
        last_price=price,
        fix_c=1000.0,
        p0=50.0,
        diff=100.0,
    )

    assert decision.action == expected_action
    assert decision.side == (None if expected_action == "PASS" else expected_action)
    assert decision.order_quantity == expected_qty
    assert decision.reason == expected_reason
    assert decision.value_now_usd == quantity * price


def test_shannon_threshold_is_inclusive_and_output_aliases_are_preserved():
    decision = calculate_shannon_decision(9.0, 100.0, 1000.0, 50.0, 100.0)
    payload = decision.to_dict()

    assert decision.action == "PASS"
    assert payload["order_qty"] == payload["order_quantity"] == 0.0
    assert payload["rebalance"] == payload["rebalance_amount"] == 100.0
    assert payload["baseline"] == payload["baseline_pnl"]


def test_export_fix_c_example_uses_position_quantity_not_zero():
    decision = calculate_shannon_decision(
        quantity=10.0,
        last_price=150.0,
        fix_c=2000.0,
        p0=100.0,
        diff=60.0,
    )

    assert decision.value_now_usd == 1500.0
    assert decision.rebalance_amount == 500.0
    assert decision.action == "BUY"
    assert decision.order_quantity == 3.33333


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": -1},
        {"last_price": 0},
        {"fix_c": 0},
        {"p0": 0},
        {"diff": -1},
        {"decimal_precision": -1},
    ],
)
def test_shannon_decision_rejects_invalid_inputs(overrides):
    arguments = {
        "quantity": 1.0,
        "last_price": 100.0,
        "fix_c": 1000.0,
        "p0": 50.0,
        "diff": 10.0,
        "decimal_precision": 5,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError):
        calculate_shannon_decision(**arguments)


def test_client_order_id_is_stable_normalized_and_bounded():
    first = generate_client_order_id("strategy", "aapl", 7)
    second = generate_client_order_id("strategy", "AAPL", 7)

    assert first == second
    assert len(first) == 32
    assert first != generate_client_order_id("strategy", "AAPL", 8)


@pytest.mark.parametrize(
    ("utc_value", "expected"),
    [
        (datetime(2026, 7, 6, 13, 29, tzinfo=pytz.utc), False),
        (datetime(2026, 7, 6, 13, 30, tzinfo=pytz.utc), True),
        (datetime(2026, 7, 6, 20, 0, tzinfo=pytz.utc), False),
        (datetime(2026, 7, 5, 15, 0, tzinfo=pytz.utc), False),
    ],
)
def test_market_hours_contract(utc_value, expected):
    assert is_us_market_open(utc_value) is expected


def test_naive_datetime_is_treated_as_utc():
    converted = to_new_york_time(datetime(2026, 7, 6, 13, 30))
    assert converted.hour == 9
    assert converted.minute == 30
    assert converted.tzinfo is not None
