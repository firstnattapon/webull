#!/usr/bin/env python3
"""Guarded, metadata-only live checks for the Webull Thailand API.

The default mode makes real UAT calls for account reads, a quote, order
queries, order detail, and a MARKET preview.  It never places or cancels an
order.  Mutating modes are deliberately separate and require an exact arming
phrase plus complete order parameters supplied through the environment.

Credentials are never accepted as command-line arguments and raw Webull
responses are never printed.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


OFFICIAL_ENDPOINTS = {
    "uat": "th-api.uat.webullbroker.com",
    "prod": "api.webull.co.th",
}
READ_ONLY_MODE = "read-preview"
MARKET_PLACE_MODE = "market-place"
LIMIT_CANCEL_MODE = "limit-cancel"
MUTATING_MODES = frozenset({MARKET_PLACE_MODE, LIMIT_CANCEL_MODE})
MUTATION_ARMING_PHRASE = "I_UNDERSTAND_THIS_MUTATES_WEBULL_UAT"
ORDER_QUERY_PAGE_SIZE = 100
ORDER_QUERY_MAX_PAGES = 50
OFFICIAL_TRADING_SESSIONS = frozenset({"CORE", "ALL", "NIGHT", "ALL_DAY"})

ACCOUNT_ID_FIELDS = ("account_id",)
SYMBOL_FIELDS = ("symbol",)
POSITION_QUANTITY_FIELDS = ("quantity",)
LAST_PRICE_FIELDS = ("price",)
ORDER_STATUS_FIELDS = ("status",)
FILLED_QUANTITY_FIELDS = ("filled_quantity",)
PREVIEW_COST_FIELD = "estimated_cost"
PREVIEW_FEE_FIELD = "estimated_transaction_fee"
REJECTED_STATUSES = frozenset({"FAILED"})
CANCELLED_STATUSES = frozenset({"CANCELLED", "CANCELED"})
FILLED_STATUSES = frozenset({"FILLED"})
TERMINAL_STATUSES = REJECTED_STATUSES | FILLED_STATUSES | CANCELLED_STATUSES

_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_STATUS_PATTERN = re.compile(r"^[A-Z0-9_-]{1,40}$")


class HarnessFailure(RuntimeError):
    """A sanitized failure with a stable, non-sensitive error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _safe_gateway_error_metadata(exc: Exception) -> dict[str, Any]:
    """Expose only bounded status/code diagnostics, never exception text."""

    metadata: dict[str, Any] = {
        "failure_code": "gateway_call_failed",
        "error_type": type(exc).__name__,
    }
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, bool):
        try:
            normalized_status = int(status_code)
        except (TypeError, ValueError):
            normalized_status = None
        if normalized_status is not None and 100 <= normalized_status <= 599:
            metadata["http_status"] = normalized_status
    error_code = getattr(exc, "error_code", None)
    if isinstance(error_code, str) and _STATUS_PATTERN.fullmatch(error_code):
        metadata["upstream_error_code"] = error_code
    return metadata


@dataclass(frozen=True)
class HarnessConfig:
    mode: str
    environment: str
    endpoint: str
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_id: str = field(repr=False)
    symbol: str
    side: str
    quantity: Decimal
    session: str
    max_notional: Decimal | None
    limit_price: Decimal | None
    detail_client_order_id: str | None = field(default=None, repr=False)
    poll_attempts: int = 6
    poll_interval_seconds: float = 2.0

    @property
    def mutating(self) -> bool:
        return self.mode in MUTATING_MODES

    def secret_values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (self.app_key, self.app_secret, self.account_id)
            if value
        )


@dataclass
class StepResult:
    name: str
    status: str
    elapsed_ms: int
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "metadata": self.metadata,
        }


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HarnessFailure(f"missing_{name.lower()}")
    return value


def _parse_decimal(name: str, raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise HarnessFailure(f"invalid_{name.lower()}") from exc
    if not value.is_finite() or value <= 0:
        raise HarnessFailure(f"invalid_{name.lower()}")
    return value


def _parse_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise HarnessFailure(f"invalid_{name.lower()}") from exc
    if not minimum <= value <= maximum:
        raise HarnessFailure(f"invalid_{name.lower()}")
    return value


def _parse_bounded_float(
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise HarnessFailure(f"invalid_{name.lower()}") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise HarnessFailure(f"invalid_{name.lower()}")
    return value


def load_harness_config(mode: str) -> HarnessConfig:
    """Load and validate the harness configuration from environment only."""
    if mode not in {READ_ONLY_MODE, *MUTATING_MODES}:
        raise HarnessFailure("invalid_mode")

    environment = _required_env("WEBULL_ENV").lower()
    if environment not in OFFICIAL_ENDPOINTS:
        raise HarnessFailure("unsupported_webull_env")

    # This harness intentionally operates on the public/dedicated UAT system.
    # Production reads belong in a separately authorized operational check;
    # mutating production is never enabled here.
    if environment != "uat":
        raise HarnessFailure("live_harness_requires_uat")

    expected_endpoint = OFFICIAL_ENDPOINTS[environment]
    configured_endpoint = os.environ.get("WEBULL_TRADING_ENDPOINT", "").strip()
    if configured_endpoint and configured_endpoint.lower().rstrip("/") != expected_endpoint:
        raise HarnessFailure("endpoint_not_allowlisted")

    configured_region = os.environ.get("WEBULL_REGION", "th").strip().lower()
    if configured_region != "th":
        raise HarnessFailure("region_must_be_th")
    configured_api_version = os.environ.get("WEBULL_API_VERSION", "v3").strip().lower()
    if configured_api_version != "v3":
        raise HarnessFailure("api_version_must_be_v3")

    app_key = _required_env("WEBULL_APP_KEY")
    app_secret = _required_env("WEBULL_APP_SECRET")
    account_id = _required_env("WEBULL_ACCOUNT_ID")

    if mode in MUTATING_MODES:
        symbol_raw = _required_env("WEBULL_TEST_SYMBOL")
        side_raw = _required_env("WEBULL_TEST_SIDE")
        quantity_raw = _required_env("WEBULL_TEST_QUANTITY")
        session_raw = _required_env("WEBULL_TEST_SESSION")
        max_notional_raw = _required_env("WEBULL_TEST_MAX_NOTIONAL")
    else:
        symbol_raw = os.environ.get("WEBULL_TEST_SYMBOL", "AAPL")
        side_raw = os.environ.get("WEBULL_TEST_SIDE", "BUY")
        quantity_raw = os.environ.get("WEBULL_TEST_QUANTITY", "1")
        session_raw = os.environ.get("WEBULL_TEST_SESSION", "CORE")
        max_notional_raw = ""

    symbol = symbol_raw.strip().upper()
    side = side_raw.strip().upper()
    session = session_raw.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise HarnessFailure("invalid_webull_test_symbol")
    if side not in {"BUY", "SELL"}:
        raise HarnessFailure("invalid_webull_test_side")
    if session not in OFFICIAL_TRADING_SESSIONS:
        raise HarnessFailure("invalid_webull_test_session")

    quantity = _parse_decimal("WEBULL_TEST_QUANTITY", quantity_raw.strip())
    if quantity.as_tuple().exponent < -5:
        raise HarnessFailure("webull_test_quantity_too_precise")

    max_notional = (
        _parse_decimal("WEBULL_TEST_MAX_NOTIONAL", max_notional_raw)
        if mode in MUTATING_MODES
        else None
    )
    limit_price = None
    if mode == LIMIT_CANCEL_MODE:
        limit_price = _parse_decimal(
            "WEBULL_TEST_LIMIT_PRICE",
            _required_env("WEBULL_TEST_LIMIT_PRICE"),
        )

    if mode in MUTATING_MODES:
        if os.environ.get("WEBULL_MUTATION_ARM", "").strip() != MUTATION_ARMING_PHRASE:
            raise HarnessFailure("mutation_not_armed")
        if (
            os.environ.get("WEBULL_MUTATION_ACCOUNT_ID_CONFIRM", "").strip()
            != account_id
        ):
            raise HarnessFailure("mutation_account_binding_failed")
        expected_order_confirmation = expected_mutation_order_confirmation(
            account_id,
            mode,
            symbol,
            side,
            quantity,
            session,
            max_notional,
            limit_price,
        )
        if (
            os.environ.get("WEBULL_MUTATION_ORDER_CONFIRM", "").strip()
            != expected_order_confirmation
        ):
            raise HarnessFailure("mutation_order_binding_failed")

    detail_client_order_id = (
        os.environ.get("WEBULL_TEST_CLIENT_ORDER_ID", "").strip() or None
    )
    if (
        detail_client_order_id is not None
        and not _CLIENT_ORDER_ID_PATTERN.fullmatch(detail_client_order_id)
    ):
        raise HarnessFailure("invalid_webull_test_client_order_id")
    # UAT cancel acknowledgements can precede the terminal detail update by
    # close to a minute. Keep MARKET/read checks short, but give the explicit
    # limit-cancel roundtrip a bounded propagation window without retrying
    # cancel itself.
    default_poll_attempts = 20 if mode == LIMIT_CANCEL_MODE else 6
    default_poll_interval = 3.0 if mode == LIMIT_CANCEL_MODE else 2.0
    poll_attempts = _parse_bounded_int(
        "WEBULL_TEST_POLL_ATTEMPTS",
        default=default_poll_attempts,
        minimum=1,
        maximum=20,
    )
    poll_interval = _parse_bounded_float(
        "WEBULL_TEST_POLL_INTERVAL_SECONDS",
        default=default_poll_interval,
        minimum=0.5,
        maximum=10.0,
    )

    return HarnessConfig(
        mode=mode,
        environment=environment,
        endpoint=expected_endpoint,
        app_key=app_key,
        app_secret=app_secret,
        account_id=account_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        session=session,
        max_notional=max_notional,
        limit_price=limit_price,
        detail_client_order_id=detail_client_order_id,
        poll_attempts=poll_attempts,
        poll_interval_seconds=poll_interval,
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def expected_mutation_order_confirmation(
    account_id: str,
    mode: str,
    symbol: str,
    side: str,
    quantity: Decimal,
    session: str,
    max_notional: Decimal,
    limit_price: Decimal | None,
) -> str:
    """Return an exact order-intent binding without exposing the account ID."""
    if mode not in MUTATING_MODES:
        raise HarnessFailure("invalid_mutation_confirmation_mode")
    limit_price_text = "none" if limit_price is None else _decimal_text(limit_price)
    return (
        f"uat|acct-sha256={_hash_identifier(account_id)}"
        f"|mode={mode}|symbol={symbol.upper()}|side={side.upper()}"
        f"|quantity={_decimal_text(quantity)}|session={session.upper()}"
        f"|max-notional={_decimal_text(max_notional)}"
        f"|limit-price={limit_price_text}"
    )


def build_order_payload(
    config: HarnessConfig,
    client_order_id: str,
    *,
    order_type: str,
    limit_price: Decimal | None = None,
) -> list[dict[str, str]]:
    order_type = order_type.upper()
    if order_type not in {"MARKET", "LIMIT"}:
        raise HarnessFailure("unsupported_order_type")
    if order_type == "LIMIT" and limit_price is None:
        raise HarnessFailure("limit_price_required")
    if order_type == "MARKET" and limit_price is not None:
        raise HarnessFailure("market_order_must_not_have_limit_price")

    order = {
        "combo_type": "NORMAL",
        "client_order_id": client_order_id,
        "symbol": config.symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": order_type,
        "quantity": _decimal_text(config.quantity),
        "support_trading_session": config.session,
        "side": config.side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }
    if limit_price is not None:
        order["limit_price"] = _decimal_text(limit_price)
    return [order]


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_dicts(child)


def _first_scalar(value: Any, fields: tuple[str, ...]) -> Any:
    for item in _iter_dicts(value):
        for field_name in fields:
            candidate = item.get(field_name)
            if candidate not in (None, "") and not isinstance(candidate, (dict, list)):
                return candidate
    return None


def _all_scalar_text(value: Any, fields: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    for item in _iter_dicts(value):
        for field_name in fields:
            candidate = item.get(field_name)
            if candidate not in (None, "") and not isinstance(candidate, (dict, list)):
                found.add(str(candidate))
    return found


def _direct_scalar(record: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(record, dict):
        return None
    for field_name in fields:
        candidate = record.get(field_name)
        if candidate not in (None, "") and not isinstance(
            candidate, (dict, list, tuple)
        ):
            return candidate
    return None


def _parse_response_decimal(raw: Any, failure_code: str) -> Decimal:
    if isinstance(raw, str):
        raw = raw.replace(",", "").strip()
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise HarnessFailure(failure_code) from exc
    if not value.is_finite():
        raise HarnessFailure(failure_code)
    return value


def _documented_order_records(response: Any) -> list[dict[str, Any]]:
    """Return direct records from the official group ``orders[]`` shape.

    Query responses are arrays of group objects; detail is one group object.
    Arbitrary recursive traversal is intentionally forbidden because it can
    correlate an unrelated nested object on a shared account.
    """

    if isinstance(response, dict):
        groups = [response]
    elif isinstance(response, list):
        groups = response
    else:
        raise HarnessFailure("invalid_order_response_container")

    records: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise HarnessFailure("invalid_order_group")
        orders = group.get("orders")
        if not isinstance(orders, list):
            raise HarnessFailure("invalid_orders_array")
        for order in orders:
            if not isinstance(order, dict):
                raise HarnessFailure("invalid_order_record")
            records.append(order)
    return records


def _documented_client_order_ids(response: Any) -> list[str]:
    client_order_ids: list[str] = []
    for record in _documented_order_records(response):
        client_order_id = record.get("client_order_id")
        if (
            not isinstance(client_order_id, str)
            or not _CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id)
        ):
            raise HarnessFailure("invalid_order_client_order_id")
        client_order_ids.append(client_order_id)
    return client_order_ids


def _validate_order_query_shape(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, list):
        raise HarnessFailure("invalid_order_query_response")
    _documented_order_records(response)
    return response


def _validate_order_detail_shape(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise HarnessFailure("invalid_order_detail_response")
    _documented_order_records(response)
    return response


def _find_correlated_order(response: Any, client_order_id: str) -> dict[str, Any] | None:
    """Return one exact match from documented ``orders`` arrays only.

    Group/container identifiers are not executable orders.  Duplicate exact
    matches are ambiguous and therefore fail closed instead of selecting the
    first record returned by a shared UAT account.
    """

    records = _documented_order_records(response)
    client_order_ids = _documented_client_order_ids(response)
    matches = [
        record
        for record, returned_id in zip(records, client_order_ids)
        if returned_id == client_order_id
    ]
    if len(matches) > 1:
        raise HarnessFailure("multiple_orders_matched_client_order_id")
    return matches[0] if matches else None


def _order_status(record: Any) -> str:
    raw = _direct_scalar(record, ORDER_STATUS_FIELDS)
    normalized = str(raw or "UNKNOWN").strip().upper()
    return normalized if _STATUS_PATTERN.fullmatch(normalized) else "UNKNOWN"


def _filled_quantity(record: Any) -> Decimal:
    raw = _direct_scalar(record, FILLED_QUANTITY_FIELDS)
    if raw in (None, ""):
        return Decimal("0")
    value = _parse_response_decimal(raw, "invalid_filled_quantity")
    if value < 0:
        raise HarnessFailure("invalid_filled_quantity")
    return value


def _extract_symbol_number(
    response: Any,
    symbol: str,
    number_fields: tuple[str, ...],
    failure_code: str,
) -> Decimal | None:
    target = symbol.upper()
    symbol_seen = False
    for item in _iter_dicts(response):
        item_symbol = _first_scalar(item, SYMBOL_FIELDS)
        if item_symbol is None or str(item_symbol).strip().upper() != target:
            continue
        symbol_seen = True
        raw_number = _first_scalar(item, number_fields)
        if raw_number not in (None, ""):
            return _parse_response_decimal(raw_number, failure_code)
    if symbol_seen:
        raise HarnessFailure(failure_code)
    return None


def _extract_quote(response: Any, symbol: str) -> Decimal:
    value = _extract_symbol_number(
        response,
        symbol,
        LAST_PRICE_FIELDS,
        "invalid_quote_response",
    )
    if value is None:
        candidates = {
            _parse_response_decimal(raw, "invalid_quote_response")
            for item in _iter_dicts(response)
            for field_name in LAST_PRICE_FIELDS
            if (raw := item.get(field_name)) not in (None, "")
        }
        if len(candidates) == 1:
            value = candidates.pop()
    if value is None or value <= 0:
        raise HarnessFailure("invalid_quote_response")
    return value


def _extract_position(response: Any, symbol: str) -> Decimal:
    value = _extract_symbol_number(
        response,
        symbol,
        POSITION_QUANTITY_FIELDS,
        "invalid_position_response",
    )
    if value is None:
        return Decimal("0")
    if value < 0:
        raise HarnessFailure("invalid_position_response")
    return value


def _record_count(response: Any, fields: tuple[str, ...]) -> int:
    return len(_all_scalar_text(response, fields))


def _response_container(response: Any, failure_code: str) -> None:
    if not isinstance(response, (dict, list)):
        raise HarnessFailure(failure_code)


def _validate_preview(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or "orders" in response:
        raise HarnessFailure("invalid_preview_response")
    status = str(response.get("status") or "").strip().upper()
    if status in REJECTED_STATUSES:
        raise HarnessFailure("preview_rejected")
    for field_name in (PREVIEW_COST_FIELD, PREVIEW_FEE_FIELD):
        value = response.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise HarnessFailure("preview_missing_required_string_estimate")
    return {
        "accepted": True,
        "estimated_cost_present": True,
        "estimated_transaction_fee_present": True,
    }


def _extract_preview_notional(response: Any) -> Decimal:
    if not isinstance(response, dict):
        raise HarnessFailure("invalid_preview_response")
    raw = response.get(PREVIEW_COST_FIELD)
    if not isinstance(raw, str) or not raw.strip():
        raise HarnessFailure("preview_missing_notional_estimate")
    value = abs(_parse_response_decimal(raw, "invalid_preview_notional_estimate"))
    if value <= 0:
        raise HarnessFailure("invalid_preview_notional_estimate")
    return value


def _validate_flat_order_response(
    response: Any,
    client_order_id: str,
    *,
    failure_prefix: str,
) -> dict[str, Any]:
    if not isinstance(response, dict) or "orders" in response:
        raise HarnessFailure(f"{failure_prefix}_invalid_response")
    returned_client_order_id = response.get("client_order_id")
    if (
        not isinstance(returned_client_order_id, str)
        or returned_client_order_id != client_order_id
    ):
        raise HarnessFailure(f"{failure_prefix}_response_not_correlated")
    order_id = response.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise HarnessFailure(f"{failure_prefix}_response_missing_order_id")
    return {"correlated": True, "order_id_present": True}


def _validate_place_response(response: Any, client_order_id: str) -> dict[str, Any]:
    metadata = _validate_flat_order_response(
        response,
        client_order_id,
        failure_prefix="place",
    )
    status = _order_status(response)
    if status in REJECTED_STATUSES:
        raise HarnessFailure("place_response_rejected")
    return {
        **metadata,
        "status_category": status,
    }


def _validate_cancel_response(response: Any, client_order_id: str) -> dict[str, Any]:
    return {
        **_validate_flat_order_response(
            response,
            client_order_id,
            failure_prefix="cancel",
        ),
        "cancel_accepted": True,
    }


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _quiet_call(function: Callable[[], Any]) -> Any:
    # The SDK historically writes verbose request details to stdout/stderr.
    # Discard both streams for every external call; the harness emits only its
    # own sanitized report after all calls finish.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        return function()


@contextlib.contextmanager
def _suppressed_sdk_logging() -> Iterable[None]:
    logger_names = ("webull", "webull.core", "webull.data", "webull.trade")
    previous: dict[str, tuple[list[logging.Handler], bool, bool, int]] = {}
    for logger_name in logger_names:
        sdk_logger = logging.getLogger(logger_name)
        previous[logger_name] = (
            list(sdk_logger.handlers),
            sdk_logger.propagate,
            sdk_logger.disabled,
            sdk_logger.level,
        )
        sdk_logger.handlers.clear()
        sdk_logger.addHandler(logging.NullHandler())
        sdk_logger.propagate = False
        sdk_logger.disabled = True
    try:
        yield
    finally:
        for logger_name, state in previous.items():
            handlers, propagate, disabled, level = state
            sdk_logger = logging.getLogger(logger_name)
            sdk_logger.handlers.clear()
            sdk_logger.handlers.extend(handlers)
            sdk_logger.propagate = propagate
            sdk_logger.disabled = disabled
            sdk_logger.setLevel(level)


def _default_gateway_factory(config: HarnessConfig, token_dir: str) -> Any:
    from webull_api import WebullApiGateway

    gateway_config = SimpleNamespace(
        app_key=config.app_key,
        app_secret=config.app_secret,
        account_id=config.account_id,
        region="th",
        endpoint=config.endpoint,
        token_dir=token_dir,
        support_trading_session=config.session,
    )
    return WebullApiGateway(gateway_config)


class LiveHarnessRunner:
    def __init__(self, config: HarnessConfig, gateway: Any):
        self.config = config
        self.gateway = gateway
        self.steps: list[StepResult] = []
        self.owned_client_order_ids: set[str] = set()
        self.quote: Decimal | None = None
        self.position_before: Decimal | None = None
        self.initial_order_ids: set[str] = set()

    def _append(
        self,
        name: str,
        status: str,
        elapsed_seconds: float,
        metadata: dict[str, Any],
    ) -> None:
        self.steps.append(
            StepResult(
                name=name,
                status=status,
                elapsed_ms=max(0, round(elapsed_seconds * 1000)),
                metadata=metadata,
            )
        )

    def _execute(
        self,
        name: str,
        operation: Callable[[], Any],
        validator: Callable[[Any], dict[str, Any]],
    ) -> Any:
        started = time.perf_counter()
        try:
            response = _quiet_call(operation)
            metadata = validator(response)
        except HarnessFailure as exc:
            self._append(
                name,
                "FAIL",
                time.perf_counter() - started,
                {"failure_code": exc.code},
            )
            raise
        except Exception as exc:
            self._append(
                name,
                "FAIL",
                time.perf_counter() - started,
                _safe_gateway_error_metadata(exc),
            )
            raise HarnessFailure(f"{name}_gateway_call_failed") from None
        self._append(name, "PASS", time.perf_counter() - started, metadata)
        return response

    def _sleep_between_polls(self, attempt: int) -> None:
        if attempt + 1 < self.config.poll_attempts:
            time.sleep(self.config.poll_interval_seconds)

    def _assert_owned(self, client_order_id: str) -> None:
        if client_order_id not in self.owned_client_order_ids:
            raise HarnessFailure("cancel_target_not_owned_by_run")

    def _generic_order_method(self, name: str) -> Callable[[Any], Any]:
        method = getattr(self.gateway, name, None)
        if not callable(method):
            raise HarnessFailure(f"gateway_missing_{name}")
        return method

    def _check_notional(
        self,
        price: Decimal,
        *,
        failure_code: str = "max_notional_exceeded",
    ) -> None:
        if self.config.max_notional is None:
            raise HarnessFailure("max_notional_required")
        notional = self.config.quantity * price
        if notional > self.config.max_notional:
            raise HarnessFailure(failure_code)

    def _validate_market_submission_preview(self, response: Any) -> dict[str, Any]:
        metadata = _validate_preview(response)
        if self.config.max_notional is None:
            raise HarnessFailure("max_notional_required")
        preview_notional = _extract_preview_notional(response)
        if preview_notional > self.config.max_notional:
            raise HarnessFailure("advisory_max_notional_exceeded_by_preview")
        return {
            **metadata,
            "advisory_notional_guard_passed": True,
            "guard_uses_quote_and_preview": True,
            "hard_price_cap_enforced": False,
        }

    def _run_reads_and_preview(self) -> None:
        account_response = self._execute(
            "account_list",
            self.gateway.get_account_list,
            self._validate_account_list,
        )
        del account_response

        self._execute(
            "account_balance",
            self.gateway.get_account_balance,
            lambda response: self._validate_container(
                response, "invalid_balance_response", "balance_payload_valid"
            ),
        )

        positions_response = self._execute(
            "positions",
            self.gateway.get_positions,
            self._validate_positions,
        )
        self.position_before = _extract_position(positions_response, self.config.symbol)

        quote_response = self._execute(
            "quote",
            lambda: self.gateway.get_quote(self.config.symbol),
            self._validate_quote,
        )
        self.quote = _extract_quote(quote_response, self.config.symbol)

        open_response = self._execute(
            "open_orders",
            lambda: self.gateway.get_open_orders(
                page_size=20, last_client_order_id=None
            ),
            self._validate_orders_container,
        )
        history_response = self._execute(
            "order_history",
            lambda: self.gateway.get_order_history(
                page_size=20, start_date=None, last_client_order_id=None
            ),
            self._validate_orders_container,
        )
        self.initial_order_ids = set(
            _documented_client_order_ids(open_response)
        ) | set(_documented_client_order_ids(history_response))

        detail_id = self.config.detail_client_order_id
        if detail_id is None and self.initial_order_ids:
            detail_id = sorted(self.initial_order_ids)[0]
        if detail_id is None:
            self._append(
                "order_detail",
                "FAIL",
                0,
                {"failure_code": "no_order_id_available_for_detail"},
            )
            raise HarnessFailure("no_order_id_available_for_detail")
        self._execute(
            "order_detail",
            lambda: self.gateway.get_order_detail(detail_id),
            lambda response: self._validate_detail(response, detail_id),
        )

        preview_client_order_id = uuid.uuid4().hex
        preview_payload = build_order_payload(
            self.config,
            preview_client_order_id,
            order_type="MARKET",
        )
        self._execute(
            "market_order_preview",
            lambda: self.gateway.preview_market_order(preview_payload),
            _validate_preview,
        )

    def _validate_account_list(self, response: Any) -> dict[str, Any]:
        _response_container(response, "invalid_account_list_response")
        account_ids = _all_scalar_text(response, ACCOUNT_ID_FIELDS)
        if self.config.account_id not in account_ids:
            raise HarnessFailure("configured_account_not_returned")
        return {
            "configured_account_bound": True,
            "account_record_count": len(account_ids),
        }

    @staticmethod
    def _validate_container(
        response: Any,
        failure_code: str,
        metadata_key: str,
    ) -> dict[str, Any]:
        _response_container(response, failure_code)
        return {metadata_key: True}

    def _validate_positions(self, response: Any) -> dict[str, Any]:
        _response_container(response, "invalid_positions_response")
        # Parsing the configured symbol here makes a malformed matching record
        # fail closed. A genuinely absent position is valid and normalizes to 0.
        _extract_position(response, self.config.symbol)
        return {
            "payload_valid": True,
            "position_record_count": _record_count(response, SYMBOL_FIELDS),
        }

    def _validate_quote(self, response: Any) -> dict[str, Any]:
        _response_container(response, "invalid_quote_response")
        _extract_quote(response, self.config.symbol)
        return {"payload_valid": True, "positive_price": True}

    @staticmethod
    def _validate_orders_container(response: Any) -> dict[str, Any]:
        _validate_order_query_shape(response)
        client_order_ids = _documented_client_order_ids(response)
        return {
            "payload_valid": True,
            "order_record_count": len(client_order_ids),
        }

    @staticmethod
    def _validate_detail(response: Any, client_order_id: str) -> dict[str, Any]:
        _validate_order_detail_shape(response)
        record = _find_correlated_order(response, client_order_id)
        if record is None:
            raise HarnessFailure("order_detail_not_correlated")
        return {
            "correlated": True,
            "correlation_hash": _hash_identifier(client_order_id),
            "status_category": _order_status(record),
        }

    def _poll_for_filled_detail(self, client_order_id: str) -> tuple[Any, int]:
        last_correlated: Any = None
        for attempt in range(self.config.poll_attempts):
            try:
                response = _quiet_call(
                    lambda: self.gateway.get_order_detail(client_order_id)
                )
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            _validate_order_detail_shape(response)
            record = _find_correlated_order(response, client_order_id)
            if record is not None:
                last_correlated = response
                filled = _filled_quantity(record)
                status = _order_status(record)
                if status in FILLED_STATUSES and filled >= self.config.quantity:
                    return response, attempt + 1
                if status in TERMINAL_STATUSES:
                    if filled > 0:
                        raise HarnessFailure("market_order_terminal_partial_fill")
                    raise HarnessFailure("market_order_terminal_unfilled")
            self._sleep_between_polls(attempt)
        if last_correlated is None:
            raise HarnessFailure("placed_order_detail_not_visible")
        raise HarnessFailure("market_order_not_fully_filled")

    def _poll_history(self, client_order_id: str) -> tuple[Any, int]:
        for attempt in range(self.config.poll_attempts):
            try:
                response = self._query_all_order_pages(history=True)
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            if _find_correlated_order(response, client_order_id) is not None:
                return response, attempt + 1
            self._sleep_between_polls(attempt)
        raise HarnessFailure("placed_order_not_visible_in_history")

    def _poll_position_reconciliation(
        self,
        filled_quantity: Decimal,
    ) -> tuple[Any, int]:
        if self.position_before is None:
            raise HarnessFailure("position_baseline_missing")
        expected_delta = filled_quantity if self.config.side == "BUY" else -filled_quantity
        # Quantity payloads use at most five decimal places.  A tolerance one
        # order tick smaller proves the exact cumulative position delta rather
        # than accepting a percentage-sized mismatch.
        epsilon = Decimal("0.000001")
        for attempt in range(self.config.poll_attempts):
            try:
                response = _quiet_call(self.gateway.get_positions)
                position_after = _extract_position(response, self.config.symbol)
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            actual_delta = position_after - self.position_before
            if abs(actual_delta - expected_delta) <= epsilon:
                return response, attempt + 1
            self._sleep_between_polls(attempt)
        raise HarnessFailure("filled_order_position_not_reconciled")

    def _run_market_place(self) -> None:
        if self.quote is None:
            raise HarnessFailure("quote_missing_for_notional_guard")
        self._check_notional(
            self.quote,
            failure_code="advisory_max_notional_exceeded_by_quote",
        )

        client_order_id = uuid.uuid4().hex
        if client_order_id in self.initial_order_ids:
            raise HarnessFailure("generated_client_order_id_collision")
        self.owned_client_order_ids.add(client_order_id)
        payload = build_order_payload(
            self.config,
            client_order_id,
            order_type="MARKET",
        )

        placement_attempted = False
        completed = False
        try:
            # Preview the exact object that will be submitted. The earlier
            # read-only lab preview proves connectivity, but it intentionally
            # has a different client_order_id and cannot authorize this mutation.
            self._execute(
                "market_order_place_preview",
                lambda: self.gateway.preview_market_order(payload),
                self._validate_market_submission_preview,
            )
            placement_attempted = True
            self._execute(
                "market_order_place",
                lambda: self.gateway.place_market_order(payload),
                lambda response: {
                    **_validate_place_response(response, client_order_id),
                    "advisory_notional_guard_passed": True,
                    "hard_price_cap_enforced": False,
                    "correlation_hash": _hash_identifier(client_order_id),
                },
            )

            def poll_detail() -> Any:
                response, attempts = self._poll_for_filled_detail(client_order_id)
                self._market_detail_poll_attempts = attempts
                return response

            detail_response = self._execute(
                "market_order_detail_filled",
                poll_detail,
                lambda response: self._validate_filled_detail(
                    response, client_order_id
                ),
            )
            detail_record = _find_correlated_order(detail_response, client_order_id)
            if detail_record is None:
                raise HarnessFailure("placed_order_detail_not_visible")
            filled_quantity = _filled_quantity(detail_record)

            def poll_history() -> Any:
                response, attempts = self._poll_history(client_order_id)
                self._history_poll_attempts = attempts
                return response

            self._execute(
                "market_order_history_visible",
                poll_history,
                lambda response: {
                    "correlated": _find_correlated_order(response, client_order_id)
                    is not None,
                    "poll_attempts": getattr(self, "_history_poll_attempts", 1),
                },
            )

            def poll_reconciliation() -> Any:
                response, attempts = self._poll_position_reconciliation(filled_quantity)
                self._reconcile_poll_attempts = attempts
                return response

            self._execute(
                "market_order_position_reconciled",
                poll_reconciliation,
                lambda response: {
                    "position_reconciled": True,
                    "poll_attempts": getattr(self, "_reconcile_poll_attempts", 1),
                    "payload_valid": isinstance(response, (dict, list)),
                },
            )
            completed = True
        finally:
            if placement_attempted and not completed:
                self._best_effort_owned_cleanup(
                    client_order_id,
                    scope="market_order",
                    cancel_already_attempted=False,
                )

    def _validate_filled_detail(
        self,
        response: Any,
        client_order_id: str,
    ) -> dict[str, Any]:
        record = _find_correlated_order(response, client_order_id)
        if record is None:
            raise HarnessFailure("placed_order_detail_not_visible")
        filled = _filled_quantity(record)
        status = _order_status(record)
        if status not in FILLED_STATUSES or filled < self.config.quantity:
            raise HarnessFailure("market_order_not_fully_filled")
        return {
            "correlated": True,
            "full_fill_confirmed": True,
            "status_category": status,
            "poll_attempts": getattr(self, "_market_detail_poll_attempts", 1),
        }

    def _validate_non_marketable_limit(self) -> None:
        if self.quote is None or self.config.limit_price is None:
            raise HarnessFailure("limit_guard_inputs_missing")
        if self.config.side == "BUY":
            # A 1% quote buffer is non-marketable for the immediate roundtrip
            # while remaining inside the UAT venue's accepted price band.
            # Wider 10% and 50% distances are rejected as ORDER_PRICE_ILLEGAL.
            safe = self.config.limit_price <= self.quote * Decimal("0.99")
        else:
            safe = self.config.limit_price >= self.quote * Decimal("1.01")
        if not safe:
            raise HarnessFailure("limit_price_not_safely_non_marketable")
        self._check_notional(self.config.limit_price)

    def _query_all_order_pages(self, *, history: bool) -> list[dict[str, Any]]:
        """Read a complete open/history result with a bounded exact cursor."""

        groups: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(ORDER_QUERY_MAX_PAGES):
            if history:
                response = _quiet_call(
                    lambda: self.gateway.get_order_history(
                        page_size=ORDER_QUERY_PAGE_SIZE,
                        start_date=None,
                        last_client_order_id=cursor,
                    )
                )
            else:
                response = _quiet_call(
                    lambda: self.gateway.get_open_orders(
                        page_size=ORDER_QUERY_PAGE_SIZE,
                        last_client_order_id=cursor,
                    )
                )
            page = _validate_order_query_shape(response)
            groups.extend(page)
            if len(page) < ORDER_QUERY_PAGE_SIZE:
                return groups

            next_cursor = page[-1].get("client_order_id")
            if (
                not isinstance(next_cursor, str)
                or not _CLIENT_ORDER_ID_PATTERN.fullmatch(next_cursor)
            ):
                raise HarnessFailure("order_query_invalid_pagination_cursor")
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise HarnessFailure("order_query_repeated_pagination_cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise HarnessFailure("order_query_pagination_limit_exceeded")

    def _poll_open_presence(
        self,
        client_order_id: str,
        expected_present: bool,
    ) -> tuple[Any, int]:
        for attempt in range(self.config.poll_attempts):
            try:
                response = self._query_all_order_pages(history=False)
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            present = _find_correlated_order(response, client_order_id) is not None
            if present == expected_present:
                return response, attempt + 1
            self._sleep_between_polls(attempt)
        code = (
            "limit_order_not_visible_as_open"
            if expected_present
            else "cancelled_order_still_open"
        )
        raise HarnessFailure(code)

    def _poll_cancelled_detail(self, client_order_id: str) -> tuple[Any, int]:
        """Confirm cancellation from detail, or exact paginated history.

        Webull UAT can acknowledge a cancel and remove the order from open
        orders while its detail endpoint remains stale. History is an official
        terminal-order source, so it is a valid corroborating fallback; open
        absence alone is never sufficient.
        """

        for attempt in range(self.config.poll_attempts):
            try:
                response = _quiet_call(
                    lambda: self.gateway.get_order_detail(client_order_id)
                )
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            _validate_order_detail_shape(response)
            record = _find_correlated_order(response, client_order_id)
            if record is not None:
                status = _order_status(record)
                filled = _filled_quantity(record)
                if filled > 0:
                    raise HarnessFailure("limit_order_filled_before_cancel")
                if status in CANCELLED_STATUSES:
                    self._cancel_confirmation_source = "detail"
                    return response, attempt + 1
                if status in FILLED_STATUSES:
                    raise HarnessFailure("limit_order_terminal_fill_inconsistent")

            try:
                history = self._query_all_order_pages(history=True)
            except Exception:
                history = []
            history_record = _find_correlated_order(history, client_order_id)
            if history_record is not None:
                history_status = _order_status(history_record)
                history_filled = _filled_quantity(history_record)
                if history_filled > 0:
                    raise HarnessFailure("limit_order_filled_before_cancel")
                if history_status in CANCELLED_STATUSES:
                    for group in history:
                        if _find_correlated_order(group, client_order_id) is not None:
                            self._cancel_confirmation_source = "history"
                            return group, attempt + 1
                if history_status in FILLED_STATUSES:
                    raise HarnessFailure("limit_order_terminal_fill_inconsistent")
            self._sleep_between_polls(attempt)
        raise HarnessFailure("cancel_not_confirmed_by_detail_or_history")

    def _poll_owned_detail_visible(
        self,
        client_order_id: str,
    ) -> tuple[Any, dict[str, Any], int]:
        for attempt in range(self.config.poll_attempts):
            try:
                response = _quiet_call(
                    lambda: self.gateway.get_order_detail(client_order_id)
                )
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            _validate_order_detail_shape(response)
            record = _find_correlated_order(response, client_order_id)
            if record is not None:
                return response, record, attempt + 1
            self._sleep_between_polls(attempt)
        raise HarnessFailure("owned_order_detail_not_visible")

    def _poll_owned_terminal_detail(
        self,
        client_order_id: str,
    ) -> tuple[Any, dict[str, Any], int]:
        last_record_seen = False
        for attempt in range(self.config.poll_attempts):
            try:
                response = _quiet_call(
                    lambda: self.gateway.get_order_detail(client_order_id)
                )
            except Exception:
                self._sleep_between_polls(attempt)
                continue
            _validate_order_detail_shape(response)
            record = _find_correlated_order(response, client_order_id)
            if record is not None:
                last_record_seen = True
                if _order_status(record) in TERMINAL_STATUSES:
                    return response, record, attempt + 1
            self._sleep_between_polls(attempt)
        code = (
            "owned_order_terminal_state_not_verified"
            if last_record_seen
            else "owned_order_detail_not_visible"
        )
        raise HarnessFailure(code)

    def _record_cleanup_position_reconciliation(
        self,
        scope: str,
        filled_quantity: Decimal,
    ) -> bool:
        started = time.perf_counter()
        try:
            response, attempts = self._poll_position_reconciliation(filled_quantity)
            _response_container(response, "invalid_position_response")
        except HarnessFailure as exc:
            self._append(
                f"{scope}_cleanup_position_reconciled",
                "FAIL",
                time.perf_counter() - started,
                {"failure_code": exc.code},
            )
            return False
        except Exception:
            self._append(
                f"{scope}_cleanup_position_reconciled",
                "FAIL",
                time.perf_counter() - started,
                {"failure_code": "cleanup_position_gateway_call_failed"},
            )
            return False
        self._append(
            f"{scope}_cleanup_position_reconciled",
            "PASS",
            time.perf_counter() - started,
            {"position_reconciled": True, "poll_attempts": attempts},
        )
        return True

    def _best_effort_owned_cleanup(
        self,
        client_order_id: str,
        *,
        scope: str,
        cancel_already_attempted: bool,
    ) -> None:
        """Verify one run-owned order is terminal, cancelling at most once.

        Exact order detail is authoritative.  Open-order pages are deliberately
        not used for cleanup because absence from page one is not proof of a
        terminal state on a shared account.
        """

        started = time.perf_counter()
        cancel_attempted_once = cancel_already_attempted
        cancel_result_ambiguous = False
        try:
            self._assert_owned(client_order_id)
            detail_visibility_delayed = False
            visible_attempts = 0
            terminal_attempts = 0
            record: dict[str, Any] | None = None
            try:
                _, record, visible_attempts = self._poll_owned_detail_visible(
                    client_order_id
                )
            except HarnessFailure as exc:
                if exc.code != "owned_order_detail_not_visible":
                    raise
                # A lost place response can be accepted upstream before detail
                # becomes visible. The UUID is still run-owned, so issue at
                # most one exact-ID cancel rather than leave a queued order.
                detail_visibility_delayed = True

            status = _order_status(record)

            if record is None or status not in TERMINAL_STATUSES:
                if not cancel_attempted_once:
                    # Set before the call: a connection failure can still mean
                    # Webull received the request, so cleanup must never retry it.
                    cancel_attempted_once = True
                    try:
                        _quiet_call(lambda: self.gateway.cancel_order(client_order_id))
                    except Exception:
                        cancel_result_ambiguous = True
                _, record, terminal_attempts = self._poll_owned_terminal_detail(
                    client_order_id
                )
                status = _order_status(record)

            filled = _filled_quantity(record)
            if status in FILLED_STATUSES and filled <= 0:
                raise HarnessFailure("terminal_fill_quantity_inconsistent")

            position_reconciled = False
            if filled > 0:
                position_reconciled = self._record_cleanup_position_reconciliation(
                    scope,
                    filled,
                )
                if not position_reconciled:
                    raise HarnessFailure("cleanup_position_not_reconciled")

            self._append(
                f"{scope}_cleanup",
                "PASS",
                time.perf_counter() - started,
                {
                    "terminal_state_verified": True,
                    "status_category": status,
                    "cancel_attempted_once": cancel_attempted_once,
                    "cancel_result_ambiguous": cancel_result_ambiguous,
                    "position_reconciled_after_fill": position_reconciled,
                    "detail_poll_attempts": visible_attempts + terminal_attempts,
                    "detail_visibility_delayed": detail_visibility_delayed,
                },
            )
        except HarnessFailure as exc:
            self._append(
                f"{scope}_cleanup",
                "FAIL",
                time.perf_counter() - started,
                {
                    "failure_code": exc.code,
                    "cancel_attempted_once": cancel_attempted_once,
                    "cancel_result_ambiguous": cancel_result_ambiguous,
                },
            )
        except Exception:
            self._append(
                f"{scope}_cleanup",
                "FAIL",
                time.perf_counter() - started,
                {
                    "failure_code": "owned_order_cleanup_failed",
                    "cancel_attempted_once": cancel_attempted_once,
                    "cancel_result_ambiguous": cancel_result_ambiguous,
                },
            )

    def _run_limit_cancel(self) -> None:
        self._validate_non_marketable_limit()
        if self.config.limit_price is None:
            raise HarnessFailure("limit_price_required")

        client_order_id = uuid.uuid4().hex
        if client_order_id in self.initial_order_ids:
            raise HarnessFailure("generated_client_order_id_collision")
        self.owned_client_order_ids.add(client_order_id)
        payload = build_order_payload(
            self.config,
            client_order_id,
            order_type="LIMIT",
            limit_price=self.config.limit_price,
        )
        placement_attempted = False
        cancel_attempted = False
        completed = False
        try:
            self._execute(
                "limit_order_preview",
                lambda: self._generic_order_method("preview_order")(payload),
                _validate_preview,
            )
            placement_attempted = True
            self._execute(
                "limit_order_place",
                lambda: self._generic_order_method("place_order")(payload),
                lambda response: {
                    **_validate_place_response(response, client_order_id),
                    "limit_notional_guard_passed": True,
                    "hard_limit_price_cap_enforced": True,
                    "safely_non_marketable": True,
                    "correlation_hash": _hash_identifier(client_order_id),
                },
            )

            def poll_open() -> Any:
                response, attempts = self._poll_open_presence(
                    client_order_id, expected_present=True
                )
                self._open_poll_attempts = attempts
                return response

            self._execute(
                "limit_order_visible_open",
                poll_open,
                lambda response: {
                    "correlated": _find_correlated_order(response, client_order_id)
                    is not None,
                    "poll_attempts": getattr(self, "_open_poll_attempts", 1),
                },
            )

            self._assert_owned(client_order_id)
            cancel_attempted = True
            self._execute(
                "limit_order_cancel",
                lambda: self.gateway.cancel_order(client_order_id),
                lambda response: _validate_cancel_response(
                    response,
                    client_order_id,
                ),
            )

            def poll_cancelled() -> Any:
                response, attempts = self._poll_cancelled_detail(client_order_id)
                self._cancel_poll_attempts = attempts
                return response

            self._execute(
                "limit_order_cancelled_detail",
                poll_cancelled,
                lambda response: {
                    **self._validate_detail(response, client_order_id),
                    "cancelled": True,
                    "confirmation_source": getattr(
                        self, "_cancel_confirmation_source", "detail"
                    ),
                    "poll_attempts": getattr(self, "_cancel_poll_attempts", 1),
                },
            )

            def poll_absent() -> Any:
                response, attempts = self._poll_open_presence(
                    client_order_id, expected_present=False
                )
                self._absent_poll_attempts = attempts
                return response

            self._execute(
                "limit_order_absent_from_open",
                poll_absent,
                lambda response: {
                    "absent": _find_correlated_order(response, client_order_id)
                    is None,
                    "poll_attempts": getattr(self, "_absent_poll_attempts", 1),
                },
            )
            completed = True
        finally:
            if placement_attempted and not completed:
                self._best_effort_owned_cleanup(
                    client_order_id,
                    scope="limit_order",
                    cancel_already_attempted=cancel_attempted,
                )

    def run(self) -> dict[str, Any]:
        failure_code: str | None = None
        try:
            self._run_reads_and_preview()
            if self.config.mode == MARKET_PLACE_MODE:
                self._run_market_place()
            elif self.config.mode == LIMIT_CANCEL_MODE:
                self._run_limit_cancel()
        except HarnessFailure as exc:
            failure_code = exc.code
        except Exception:
            # Never allow an SDK/library exception (whose message may contain
            # a request body or signed headers) to reach the terminal.
            failure_code = "unexpected_harness_failure"

        has_failed_step = any(step.status == "FAIL" for step in self.steps)
        status = "FAIL" if failure_code or has_failed_step else "PASS"
        report: dict[str, Any] = {
            "harness": "webull-live-test",
            "status": status,
            "mode": self.config.mode,
            "environment": self.config.environment,
            "endpoint": self.config.endpoint,
            "network": "real",
            "mutations_enabled": self.config.mutating,
            "steps": [step.to_dict() for step in self.steps],
        }
        if failure_code:
            report["failure_code"] = failure_code
        return report


def run_harness(
    config: HarnessConfig,
    gateway_factory: Callable[[HarnessConfig, str], Any] = _default_gateway_factory,
) -> dict[str, Any]:
    """Run in an isolated temporary directory to contain SDK side effects."""
    original_cwd = Path.cwd()
    previous_token_dir = os.environ.get("WEBULL_OPENAPI_TOKEN_DIR")
    with tempfile.TemporaryDirectory(prefix="webull-live-test-") as token_dir:
        try:
            os.environ["WEBULL_OPENAPI_TOKEN_DIR"] = token_dir
            os.chdir(token_dir)
            with _suppressed_sdk_logging():
                try:
                    gateway = _quiet_call(lambda: gateway_factory(config, token_dir))
                except Exception as exc:
                    return {
                        "harness": "webull-live-test",
                        "status": "FAIL",
                        "mode": config.mode,
                        "environment": config.environment,
                        "endpoint": config.endpoint,
                        "network": "real",
                        "mutations_enabled": config.mutating,
                        "failure_code": "gateway_initialization_failed",
                        "failure_type": type(exc).__name__,
                        "steps": [],
                    }
                return LiveHarnessRunner(config, gateway).run()
        finally:
            os.chdir(original_cwd)
            if previous_token_dir is None:
                os.environ.pop("WEBULL_OPENAPI_TOKEN_DIR", None)
            else:
                os.environ["WEBULL_OPENAPI_TOKEN_DIR"] = previous_token_dir


def serialize_report(report: dict[str, Any], secret_values: Iterable[str]) -> str:
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(secret and secret in encoded for secret in secret_values):
        raise HarnessFailure("secret_leak_detected")
    return encoded


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run metadata-only Webull UAT checks. Credentials are read only "
            "from WEBULL_APP_KEY, WEBULL_APP_SECRET, and WEBULL_ACCOUNT_ID."
        )
    )
    parser.add_argument(
        "--mode",
        choices=(READ_ONLY_MODE, MARKET_PLACE_MODE, LIMIT_CANCEL_MODE),
        default=READ_ONLY_MODE,
        help="read-preview is non-mutating; other modes require explicit UAT arming",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = load_harness_config(args.mode)
    except HarnessFailure as exc:
        print(
            json.dumps(
                {
                    "harness": "webull-live-test",
                    "status": "FAIL",
                    "failure_code": exc.code,
                    "steps": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2

    report = run_harness(config)
    try:
        output = serialize_report(report, config.secret_values())
    except HarnessFailure:
        print(
            '{"failure_code":"secret_leak_detected","harness":"webull-live-test",'
            '"status":"FAIL","steps":[]}'
        )
        return 3
    print(output)
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
