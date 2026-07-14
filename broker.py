"""Unified Webull broker — single source of truth for all API interactions.

Single ``WebullBroker`` class with:
  - retry for idempotent reads only (3x jittered backoff on HTTP 5xx,
    network errors, and Webull's 417 OPENAPI_REPEAT_REQUEST throttle);
    order placement is submitted exactly once and never resubmitted
  - Manual-compatible sequential position + quote reads through one gateway
  - connection caching (module-level singleton, thread-safe)
  - lightweight metrics counters exposed via ``get_broker_metrics``
"""

from __future__ import annotations

import functools
import logging
import math
import random
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("shannon_demon_dna.broker")

ORDER_QUANTITY_DECIMAL_PRECISION = 5
OPEN_ORDER_PAGE_SIZE = 100
OPEN_ORDER_MAX_PAGES = 50
CANCEL_CONFIRM_ATTEMPTS = 3
CANCEL_CONFIRM_INTERVAL_SECONDS = 1.05
EQUITY_INSTRUMENT_TYPES = frozenset({"EQUITY", "STOCK"})

SYMBOL_FIELDS = (
    "symbol", "ticker", "instrument_symbol", "instrumentSymbol",
)
POSITION_QUANTITY_FIELDS = (
    "quantity", "qty", "position_quantity", "positionQuantity",
    "position_qty", "positionQty", "holding_quantity", "holdingQuantity",
    "net_quantity", "netQuantity", "available_qty", "availableQty",
)
SNAPSHOT_PRICE_FIELDS_BY_SESSION = {
    "CORE": ("price",),
    "ALL": ("price", "extend_hour_last_price"),
    "NIGHT": ("ovn_price",),
    "ALL_DAY": ("price", "extend_hour_last_price", "ovn_price"),
}
ORDER_ID_FIELDS = ("order_id", "orderId", "id")
ORDER_STATUS_FIELDS = ("status", "order_status", "orderStatus")
# Webull can answer a place request with HTTP 200 while the body reports the
# order was not actually accepted (fractional/quantity rejects, risk checks,
# a UAT sandbox that echoes the request without booking it). These states mean
# "no live order exists" even though no exception was raised.
REJECTED_ORDER_STATUSES = frozenset({
    "FAILED", "FAIL", "REJECTED", "REJECT", "CANCELLED", "CANCELED",
    "DENIED", "INVALID", "ERROR", "EXPIRED",
})
# Fields Webull uses to explain why a request did not succeed.
ORDER_REASON_FIELDS = (
    "msg", "message", "error", "error_msg", "errorMsg", "description",
    "desc", "reason", "failure_reason", "failureReason", "code",
    "error_code", "errorCode", "failure_code", "failureCode",
)
# The quantity Webull reports as actually executed on an order. Reading this
# back (via the Manual Test Lab's "Order detail" endpoint) is the only way to
# know an accepted order truly moved the position — a fractional order can be
# accepted with an id, then cancelled/expired unfilled, leaving the held
# quantity exactly where it was. That is the reported bug.
FILLED_QUANTITY_FIELDS = (
    "filled_quantity", "filledQuantity", "filled_qty", "filledQty",
    "cumulative_quantity", "cumulativeQuantity",
    "cumulative_filled_quantity", "cumulativeFilledQuantity",
    "executed_quantity", "executedQuantity", "filled",
)
ACCOUNT_ID_FIELDS = ("account_id", "accountId")
ORDER_QUANTITY_FIELDS = (
    "quantity", "qty", "order_quantity", "orderQuantity",
    "total_quantity", "totalQuantity",
)
TERMINAL_ORDER_STATUSES = frozenset({
    *REJECTED_ORDER_STATUSES,
    "FILLED",
})
PARTIAL_ORDER_STATUSES = frozenset({"PARTIAL_FILLED", "PARTIALLY_FILLED"})
# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BrokerError(RuntimeError):
    """Base exception for all broker-related errors."""
    pass


class BrokerHTTPError(BrokerError):
    """Webull API returned a non-2xx status code."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Webull HTTP {status_code}: {body}")


class BrokerConnectionError(BrokerError):
    """Network-level failure when reaching Webull."""
    pass


class BrokerValidationError(BrokerError):
    """Invalid input or invalid upstream data before/after an API call."""
    pass


class OrderSubmissionUnknownError(BrokerError):
    """A place request was attempted but its booking outcome is ambiguous."""

    pass


class OrderCancellationUnknownError(BrokerError):
    """A cancel was attempted once but its terminal outcome is not confirmed."""

    pass


# ---------------------------------------------------------------------------
# Data classes — typed return values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketState:
    quantity: float
    last_price: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class OrderResult:
    client_order_id: str
    order_id: str | None
    status: str
    preview: Any
    raw_response: Any
    # ``accepted`` is the single source of truth for "did Webull really take
    # this order". It is True only when the response carries an order id and
    # the reported status is not a terminal rejection. When False the order
    # never reached the live book, so the caller must NOT log it as submitted
    # (that mismatch is exactly why a SELL could show in the log while the
    # held quantity never moved). ``reason`` captures Webull's own message.
    accepted: bool = True
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Raw SDK bodies can contain account/order details and must remain
        # in-memory only.  Firestore/HTTP consumers receive the correlated,
        # normalized result instead of an opaque response dump.
        return {
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "status": self.status,
            "accepted": self.accepted,
            "reason": self.reason,
            "previewed": self.preview is not None,
        }


@dataclass(frozen=True)
class OrderStatus:
    """Verified state of a placed order, read back from Webull.

    ``place_market_order`` only proves Webull *accepted* the request (it
    returned an order id). Whether that order actually *filled* — moved the
    held quantity — is a separate fact only the order-detail endpoint can
    confirm. Reading it back is exactly what the Manual Test Lab does with its
    "Order detail" button, and it is what turns an invisible "accepted but
    never filled" order (the reported bug: a SELL logged while the held
    quantity never moves) into an explicit, logged outcome.
    """

    client_order_id: str
    status: str
    filled_quantity: float
    raw_response: Any
    order_quantity: float | None = None

    @property
    def normalized_status(self) -> str:
        return self.status.strip().upper()

    @property
    def has_fill(self) -> bool:
        return self.filled_quantity > 0

    @property
    def is_filled(self) -> bool:
        """Whether the full order is terminally filled.

        A positive cumulative fill is not automatically complete.  The old
        implementation stopped at the first partial fill and abandoned the
        remaining lifecycle, which left the dashboard permanently stale.
        """
        if self.normalized_status != "FILLED" or not self.has_fill:
            return False
        if self.order_quantity is None:
            return True
        return self.filled_quantity + 1e-8 >= self.order_quantity

    @property
    def is_partial_fill(self) -> bool:
        return self.has_fill and not self.is_filled

    @property
    def is_terminal(self) -> bool:
        return self.normalized_status in TERMINAL_ORDER_STATUSES

    @property
    def is_terminal_unfilled(self) -> bool:
        """Whether the order reached a dead end without executing anything."""
        return (
            not self.has_fill
            and self.normalized_status in REJECTED_ORDER_STATUSES
        )

    @property
    def remaining_quantity(self) -> float | None:
        if self.order_quantity is None:
            return None
        return max(0.0, self.order_quantity - self.filled_quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "status": self.status,
            "order_quantity": self.order_quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "has_fill": self.has_fill,
            "is_filled": self.is_filled,
            "is_partial_fill": self.is_partial_fill,
            "is_terminal": self.is_terminal,
            "is_terminal_unfilled": self.is_terminal_unfilled,
        }


# ---------------------------------------------------------------------------
# Metrics — cheap counters, exposed on the health endpoint
# ---------------------------------------------------------------------------

_metrics_lock = threading.Lock()
_metrics: dict[str, int] = {
    "api_calls": 0,
    "retries": 0,
    "errors": 0,
    "orders_placed": 0,
}


def _record_metric(name: str, amount: int = 1) -> None:
    with _metrics_lock:
        _metrics[name] = _metrics.get(name, 0) + amount


def get_broker_metrics() -> dict[str, int]:
    """Snapshot of broker counters since this instance cold-started."""
    with _metrics_lock:
        return dict(_metrics)


# ---------------------------------------------------------------------------
# Internal helpers (single source — no duplication)
# ---------------------------------------------------------------------------

def _iter_dicts(value: Any):
    """Recursively yield every dict found inside nested dicts / lists."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_dicts(child)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_dicts(item)


def _get_value(obj: Any, *names: str, default: Any = None) -> Any:
    """Try multiple attribute / key names on *obj*, return first non-empty."""
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value not in (None, ""):
                return value

    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value not in (None, ""):
                return value

    return default


def _find_first_value(value: Any, *names: str, default: Any = None) -> Any:
    """Walk nested structure and return first non-empty match for *names*."""
    for item in _iter_dicts(value):
        found = _get_value(item, *names, default=None)
        if found not in (None, ""):
            return found
    return default


def _documented_order_groups(response: Any) -> list[dict[str, Any]]:
    """Validate the official open/history array-of-groups response shape."""
    if not isinstance(response, list):
        raise BrokerValidationError("Webull order query must return an array")
    groups: list[dict[str, Any]] = []
    for group in response:
        if not isinstance(group, dict):
            raise BrokerValidationError("Webull order query groups must be objects")
        group_client_order_id = group.get("client_order_id")
        if (
            not isinstance(group_client_order_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", group_client_order_id) is None
        ):
            raise BrokerValidationError(
                "Webull order query group has an invalid client_order_id"
            )
        orders = group.get("orders")
        if not isinstance(orders, list):
            raise BrokerValidationError("Webull order query group.orders must be an array")
        if any(not isinstance(order, dict) for order in orders):
            raise BrokerValidationError(
                "Webull order query group.orders must contain objects"
            )
        groups.append(group)
    return groups


def _normalize_symbol(value: Any) -> str:
    """Normalize Webull symbols while preserving exact ticker boundaries."""
    normalized = str(value or "").strip().upper()
    for suffix in (".US", ":US"):
        if normalized.endswith(suffix):
            return normalized[:-len(suffix)]
    return normalized


def _coerce_number(value: Any, field_name: str) -> float:
    """Convert a Webull numeric scalar, including comma-formatted strings."""
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple)):
        raise BrokerValidationError(f"Webull returned an invalid {field_name}: {value!r}")
    try:
        number = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise BrokerValidationError(
            f"Webull returned an invalid {field_name}: {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise BrokerValidationError(
            f"Webull returned a non-finite {field_name}: {value!r}"
        )
    return number


def _extract_symbol_scoped_number(
    response: Any,
    symbol: str,
    number_fields: tuple[str, ...],
    field_name: str,
) -> float | None:
    """Read a number from the smallest nested record containing *symbol*.

    Webull responses are usually flat, but some regional endpoints wrap the
    instrument and position values in sibling objects. Walking deepest records
    first keeps a symbol and its number in the same logical record and avoids
    accidentally pairing values from two different positions.
    """
    target = _normalize_symbol(symbol)
    matched_symbol = False
    records = list(_iter_dicts(response))

    for record in reversed(records):
        record_symbol = _find_first_value(record, *SYMBOL_FIELDS, default=None)
        if record_symbol in (None, "") or _normalize_symbol(record_symbol) != target:
            continue

        matched_symbol = True
        raw_number = _find_first_value(record, *number_fields, default=None)
        if raw_number in (None, ""):
            continue
        return _coerce_number(raw_number, field_name)

    if matched_symbol:
        raise BrokerValidationError(
            f"Webull position/quote for {target} did not contain {field_name}"
        )
    return None


def _call_gateway(fn, /, *args, **kwargs):
    """Invoke the sanitized Manual-compatible gateway.

    The gateway deliberately strips raw SDK messages/headers.  Translate its
    typed failures into the broker errors used by retry and HTTP orchestration
    without reintroducing the original exception as an inspectable cause.
    """
    from webull_api import (
        WebullApiProtocolError,
        WebullApiUpstreamError,
        WebullApiValidationError,
    )

    _record_metric("api_calls")
    try:
        return fn(*args, **kwargs)
    except WebullApiUpstreamError as exc:
        if exc.status_code is not None:
            code = f" code={exc.error_code}" if exc.error_code else ""
            failure: BrokerError = BrokerHTTPError(
                exc.status_code,
                f"sanitized upstream failure{code}",
            )
        else:
            failure = BrokerConnectionError(str(exc))
    except (WebullApiProtocolError, WebullApiValidationError) as exc:
        failure = BrokerValidationError(str(exc))
    raise failure from None


def _preview_rejection_reason(preview_response: Any) -> str | None:
    """Reason string if a preview response signals the order would be rejected.

    Webull answers an acceptable preview with cost/fee estimates (e.g.
    ``estimated_cost``, exactly what the Manual Test Lab shows on
    "Preview completed") and no terminal status. A rejected order — an invalid
    fractional quantity, a closed market, an unsellable position — comes back
    as an error envelope even under HTTP 200. Surfacing that reason lets the
    caller skip placement instead of logging a phantom submission.

    The official Thailand preview success schema contains both estimated cost
    and estimated transaction fee. Anything else is not authorization to place
    an order, even when the transport returned HTTP 200.
    """
    if not isinstance(preview_response, dict):
        return "preview response is not a JSON object"

    status = _get_value(preview_response, *ORDER_STATUS_FIELDS, default=None)
    if status not in (None, "") and str(status).strip().upper() in REJECTED_ORDER_STATUSES:
        return f"preview status {status}"

    # The Thailand OpenAPI success contract uses these exact snake_case,
    # top-level fields.  Do not accept aliases: a shape drift must stop before
    # placement instead of being mistaken for a successful preview.
    cost = preview_response.get("estimated_cost")
    fee = preview_response.get("estimated_transaction_fee")
    has_cost = isinstance(cost, str) and bool(cost.strip())
    has_fee = isinstance(fee, str) and bool(fee.strip())
    if not (has_cost and has_fee):
        code = _get_value(
            preview_response,
            "error_code",
            "errorCode",
            "code",
            default=None,
        )
        safe_code = (
            code.strip()
            if isinstance(code, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code.strip())
            else None
        )
        message = _get_value(
            preview_response,
            "error_msg",
            "errorMsg",
            "msg",
            "message",
            default=None,
        )
        safe_message = None
        if isinstance(message, str):
            safe_message = " ".join(message.split())[:160]
            safe_message = re.sub(r"\b\d{8,}\b", "[redacted-id]", safe_message)
            safe_message = re.sub(
                r"\b[A-Fa-f0-9]{24,}\b",
                "[redacted-token]",
                safe_message,
            )
        if safe_code and safe_message:
            return f"preview rejected ({safe_code}): {safe_message}"
        if safe_code:
            return f"preview rejected ({safe_code})"
        return "preview response is missing required cost/fee estimates"
    return None


def _format_order_quantity(quantity: float) -> str:
    """Round and format *quantity* for Webull order payload."""
    order_quantity = float(quantity)
    if not math.isfinite(order_quantity):
        raise BrokerValidationError("quantity must be finite")
    if order_quantity <= 0:
        raise BrokerValidationError("quantity must be greater than 0")

    return (
        f"{order_quantity:.{ORDER_QUANTITY_DECIMAL_PRECISION}f}"
        .rstrip("0")
        .rstrip(".")
    )


# ---------------------------------------------------------------------------
# Retry decorator — jittered exponential backoff (idempotent reads only)
# ---------------------------------------------------------------------------

# Webull rejects requests it considers a duplicate of a recent / in-flight
# one with HTTP 417 and this error code ("Please don't tap repeatedly.").
# Each SDK request is signed with a fresh nonce, so this is server-side
# idempotency keyed on request content, not a signing artefact.
REPEAT_REQUEST_CODE = "OPENAPI_REPEAT_REQUEST"


def _is_retryable_http(exc: BrokerHTTPError) -> bool:
    """Whether an HTTP error is a transient condition worth retrying.

    * 5xx — upstream server/gateway failure (e.g. 504 GATEWAY_TIMEOUT).
    * 417 OPENAPI_REPEAT_REQUEST — Webull throttling a request that arrived
      too close to an identical recent one. For an idempotent read this is a
      timing artefact (often provoked by our own retry), so we back off and
      try again rather than failing fast.

    Every other 4xx is a permanent client error and must not be retried.
    NOTE: only ever applied to reads — order placement is never retried,
    because there a repeat request means the order already reached Webull.
    """
    if exc.status_code >= 500:
        return True
    return exc.status_code == 417 and REPEAT_REQUEST_CODE in (exc.body or "")


def _retry(max_attempts: int = 3, base_delay: float = 1.0, max_jitter: float = 0.5):
    """Decorator: retry an *idempotent* call with jittered exponential backoff.

    Retries transient ``BrokerHTTPError`` (see ``_is_retryable_http``) and
    network errors. Jitter spreads retries out so they do not re-arrive
    inside Webull's repeat-request window and trip the 417 throttle — the
    very failure a naive fixed backoff caused in production.
    """

    def _backoff(attempt: int) -> float:
        return base_delay * (2 ** attempt) + random.uniform(0.0, max_jitter)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except BrokerHTTPError as exc:
                    last_exc = exc
                    if not _is_retryable_http(exc):
                        _record_metric("errors")
                        raise  # permanent 4xx = don't retry
                    if attempt < max_attempts - 1:
                        delay = _backoff(attempt)
                        _record_metric("retries")
                        logger.warning(
                            "Retry %d/%d for %s after %.1fs (HTTP %d)",
                            attempt + 1, max_attempts, fn.__name__,
                            delay, exc.status_code,
                        )
                        time.sleep(delay)
                except (BrokerConnectionError, ConnectionError, TimeoutError, OSError) as exc:
                    last_exc = (
                        exc if isinstance(exc, BrokerConnectionError)
                        else BrokerConnectionError(str(exc))
                    )
                    if attempt < max_attempts - 1:
                        delay = _backoff(attempt)
                        _record_metric("retries")
                        logger.warning(
                            "Retry %d/%d for %s after %.1fs (network)",
                            attempt + 1, max_attempts, fn.__name__, delay,
                        )
                        time.sleep(delay)
            _record_metric("errors")
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Connection cache — warm start reuse (thread-safe)
# ---------------------------------------------------------------------------

_broker_lock = threading.Lock()
_cached_broker: "WebullBroker | None" = None


def get_broker(config) -> "WebullBroker":
    """Return a cached ``WebullBroker`` or create one for this config.

    On Cloud Functions warm starts the cached client is reused, avoiding
    the ~300 ms SDK initialisation cost. Configs are compared by value
    (frozen dataclass equality), so a freshly loaded but identical config
    still hits the cache.
    """
    global _cached_broker

    # Cache hits are the normal warm-start path. Only lock when the broker
    # is absent or its configuration changed.
    cached = _cached_broker
    if cached is not None and cached.config == config:
        return cached

    with _broker_lock:
        if _cached_broker is not None and _cached_broker.config == config:
            return _cached_broker

        broker = WebullBroker(config)
        _cached_broker = broker
        return broker


# ---------------------------------------------------------------------------
# WebullBroker — unified broker class
# ---------------------------------------------------------------------------

class WebullBroker:
    """Single, consolidated interface to the Webull trading API.

    Delegates every Webull operation to the Manual-compatible gateway, while
    adding strategy-level retry, parsing, caching, and lifecycle semantics.
    """

    def __init__(self, config):
        from webull_api import WebullApiGateway

        self.config = config
        self.account_id = config.account_id
        self.gateway = WebullApiGateway(config)
        self.validate_authenticated_account()

    # ---- public API -------------------------------------------------------

    @_retry()
    def get_account_list(self) -> Any:
        return _call_gateway(self.gateway.get_account_list)

    @_retry()
    def get_account_balance(self) -> Any:
        return _call_gateway(self.gateway.get_account_balance)

    @_retry()
    def get_positions(self) -> Any:
        return _call_gateway(self.gateway.get_positions)

    @_retry()
    def get_quote(self, symbol: str) -> Any:
        return _call_gateway(self.gateway.get_quote, symbol)

    @_retry()
    def get_open_orders(
        self,
        page_size: int = OPEN_ORDER_PAGE_SIZE,
        last_client_order_id: str | None = None,
    ) -> Any:
        return _call_gateway(
            self.gateway.get_open_orders,
            page_size=page_size,
            last_client_order_id=last_client_order_id,
        )

    @_retry()
    def get_order_history(
        self,
        page_size: int = 20,
        start_date: str | None = None,
        last_client_order_id: str | None = None,
    ) -> Any:
        return _call_gateway(
            self.gateway.get_order_history,
            page_size=page_size,
            start_date=start_date,
            last_client_order_id=last_client_order_id,
        )

    @_retry()
    def get_order_detail(self, client_order_id: str) -> Any:
        return _call_gateway(self.gateway.get_order_detail, client_order_id)

    def cancel_order(self, client_order_id: str) -> dict[str, Any]:
        """Cancel exactly once, correlate the flat reply, then confirm detail."""
        try:
            response = _call_gateway(self.gateway.cancel_order, client_order_id)
        except Exception:
            raise OrderCancellationUnknownError(
                "Webull order-cancellation outcome is unknown"
            ) from None

        if (
            not isinstance(response, dict)
            or response.get("client_order_id") != client_order_id
            or not isinstance(response.get("order_id"), str)
            or not response["order_id"].strip()
        ):
            raise OrderCancellationUnknownError(
                "Webull cancel response did not correlate the client_order_id"
            )

        latest: OrderStatus | None = None
        for attempt in range(1, CANCEL_CONFIRM_ATTEMPTS + 1):
            try:
                latest = self.get_order_status(client_order_id)
                if latest.normalized_status not in {"CANCELLED", "CANCELED"}:
                    corroborated = self.lookup_order_status(client_order_id)
                    if corroborated is not None:
                        latest = corroborated
            except BrokerError:
                latest = None
            else:
                if latest.normalized_status in {"CANCELLED", "CANCELED"}:
                    return {
                        "client_order_id": client_order_id,
                        "order_id": response["order_id"].strip(),
                        "status": latest.normalized_status,
                        "filled_quantity": latest.filled_quantity,
                    }
                if latest.is_terminal:
                    raise BrokerValidationError(
                        "Webull order reached a terminal state other than CANCELLED"
                    )
            if attempt < CANCEL_CONFIRM_ATTEMPTS:
                time.sleep(CANCEL_CONFIRM_INTERVAL_SECONDS)

        raise OrderCancellationUnknownError(
            "Webull cancellation was accepted but not confirmed by order detail"
        )

    @_retry()
    def preview_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
    ) -> Any:
        payload = self._build_market_order_payload(
            symbol.strip().upper(),
            side.strip().upper(),
            quantity,
            client_order_id,
        )
        return _call_gateway(self.gateway.preview_market_order, payload)

    def validate_authenticated_account(self) -> Any:
        """Prove the configured account belongs to the authenticated app."""
        response = self.get_account_list()
        account_ids = {
            str(_get_value(item, *ACCOUNT_ID_FIELDS, default="")).strip()
            for item in _iter_dicts(response)
        }
        if self.account_id not in account_ids:
            raise BrokerValidationError(
                "Configured WEBULL_ACCOUNT_ID is not present in account list"
            )
        return response

    def get_position_and_price(self, symbol: str) -> MarketState:
        """Fetch current position quantity and last price for *symbol*.

        Position and price are fetched sequentially, matching the proven
        Manual Test Lab flow. The SDK does not document one ``ApiClient`` as
        thread-safe, so concurrent signing through the same client is avoided.
        Raises ``BrokerValidationError`` when the upstream data is unusable
        (non-positive or non-finite price), so bad market data surfaces as
        a 502 instead of an internal error deeper in the strategy.
        """
        normalized = symbol.upper()

        quantity = float(self._fetch_quantity(normalized))
        last_price = float(self._fetch_last_price(normalized))

        if not math.isfinite(quantity) or quantity < 0:
            raise BrokerValidationError(
                f"Webull returned an invalid quantity for {normalized}: {quantity}"
            )
        if not math.isfinite(last_price) or last_price <= 0:
            raise BrokerValidationError(
                f"Webull returned an invalid last price for {normalized}: {last_price}"
            )

        return MarketState(quantity=quantity, last_price=last_price)

    def get_position_quantity(self, symbol: str) -> float:
        """Fetch and validate the latest position quantity for *symbol*.

        This mirrors the Manual Test Lab's explicit ``Positions`` read and is
        intentionally separate from ``get_position_and_price``: post-fill
        reconciliation needs the holding only and should not spend another
        market-data request merely to prove that the position moved.
        """
        normalized = symbol.upper()
        quantity = float(self._fetch_quantity(normalized))
        if not math.isfinite(quantity) or quantity < 0:
            raise BrokerValidationError(
                f"Webull returned an invalid quantity for {normalized}: {quantity}"
            )
        return quantity

    def has_open_order(self, symbol: str) -> bool:
        """Return whether Webull reports an active order for *symbol*.

        The order-open endpoint is the same account/order read used by the
        Manual Test Lab. This prevents another DNA tick from stacking a new
        order while a previously accepted order has not filled yet.
        """
        normalized = _normalize_symbol(symbol)
        cursor: str | None = None
        seen_cursors: set[str] = set()

        for _ in range(OPEN_ORDER_MAX_PAGES):
            response = self.get_open_orders(
                page_size=OPEN_ORDER_PAGE_SIZE,
                last_client_order_id=cursor,
            )
            groups = _documented_order_groups(response)
            for group in groups:
                for order in group["orders"]:
                    instrument_type = order.get("instrument_type")
                    order_symbol = order.get("symbol")
                    if not isinstance(instrument_type, str) or not instrument_type:
                        raise BrokerValidationError(
                            "Webull open order is missing instrument_type"
                        )
                    if not isinstance(order_symbol, str) or not order_symbol:
                        raise BrokerValidationError(
                            "Webull open order is missing symbol"
                        )
                    # The unified Webull schema names the request instrument
                    # type EQUITY, while its stock-response example also uses
                    # STOCK. Both represent the same US-equity risk here.
                    if (
                        instrument_type.strip().upper() in EQUITY_INSTRUMENT_TYPES
                        and _normalize_symbol(order_symbol) == normalized
                    ):
                        return True

            if len(groups) < OPEN_ORDER_PAGE_SIZE:
                return False

            next_cursor = groups[-1]["client_order_id"]
            if next_cursor in seen_cursors or next_cursor == cursor:
                raise BrokerValidationError(
                    "Webull open-order pagination repeated its cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        raise BrokerValidationError(
            "Webull open-order pagination exceeded the safety page limit"
        )

    @_retry()
    def get_order_status(self, client_order_id: str) -> OrderStatus:
        """Read an order's real status and filled quantity from Webull.

        Uses the same order-detail read the Manual Test Lab exposes, selected
        from the configured order API version.  This matters for Webull TH:
        the SDK's v3 order query supports TH whereas its legacy v2 query is
        documented for HK/US only.  The read is idempotent, so it is safe to
        retry transient 417/5xx/network errors. This is the verification the
        bot previously skipped: it distinguishes an order Webull merely
        ACCEPTED (returned an id) from one it actually FILLED, closing the gap
        that let a SELL log as submitted while the held quantity never moved.
        """
        response = _call_gateway(self.gateway.get_order_detail, client_order_id)
        if not isinstance(response, dict):
            raise BrokerValidationError(
                "Webull order detail must return one group object"
            )
        order = self._find_correlated_order(response, client_order_id)
        if order is None:
            raise BrokerValidationError(
                "Webull order detail did not contain the requested client_order_id"
            )
        return self._order_status_from_record(order, client_order_id)

    def lookup_order_status(self, client_order_id: str) -> OrderStatus | None:
        """Resolve an ambiguous submission via detail, open orders, and history.

        ``None`` means all fallback reads succeeded but none contained the exact
        client_order_id. It never authorizes a resubmission; the caller keeps a
        visible manual-review lifecycle until an operator resolves it.
        """
        statuses: list[OrderStatus] = []
        try:
            detail_response = self.get_order_detail(client_order_id)
        except BrokerHTTPError as exc:
            if exc.status_code != 404:
                raise
        else:
            if not isinstance(detail_response, dict):
                raise BrokerValidationError(
                    "Webull order detail must return one group object"
                )
            order = self._find_correlated_order(detail_response, client_order_id)
            if order is not None:
                statuses.append(self._order_status_from_record(order, client_order_id))

        for history in (False, True):
            groups = self._read_all_order_query_groups(history=history)
            order = self._find_correlated_order(groups, client_order_id)
            if order is not None:
                statuses.append(self._order_status_from_record(order, client_order_id))

        if not statuses:
            return None

        # UAT can leave detail at SUBMITTED after cancel while history already
        # reports CANCELLED and the order has disappeared from all open pages.
        # Prefer the most advanced corroborated lifecycle rather than the first
        # endpoint read, while cumulative fill remains the primary tiebreaker.
        terminal_rank = {
            "FILLED": 5,
            "CANCELLED": 4,
            "CANCELED": 4,
            "EXPIRED": 3,
            "REJECTED": 2,
            "FAILED": 1,
        }

        def lifecycle_key(status: OrderStatus) -> tuple[float, int, int]:
            normalized = status.normalized_status
            return (
                status.filled_quantity,
                1 if status.is_terminal else 0,
                terminal_rank.get(normalized, 0),
            )

        return max(statuses, key=lifecycle_key)

    def _read_all_order_query_groups(self, *, history: bool) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(OPEN_ORDER_MAX_PAGES):
            if history:
                response = self.get_order_history(
                    page_size=OPEN_ORDER_PAGE_SIZE,
                    last_client_order_id=cursor,
                )
            else:
                response = self.get_open_orders(
                    page_size=OPEN_ORDER_PAGE_SIZE,
                    last_client_order_id=cursor,
                )
            page = _documented_order_groups(response)
            groups.extend(page)
            if len(page) < OPEN_ORDER_PAGE_SIZE:
                return groups
            next_cursor = page[-1]["client_order_id"]
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise BrokerValidationError(
                    "Webull order-query pagination repeated its cursor"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise BrokerValidationError(
            "Webull order-query pagination exceeded the safety page limit"
        )

    @staticmethod
    def _find_correlated_order(
        response: Any,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        from webull_api import WebullApiProtocolError, find_order_by_client_order_id

        try:
            return find_order_by_client_order_id(response, client_order_id)
        except WebullApiProtocolError as exc:
            raise BrokerValidationError(str(exc)) from None

    @staticmethod
    def _order_status_from_record(
        order: dict[str, Any],
        client_order_id: str,
    ) -> OrderStatus:

        status = _find_first_value(order, *ORDER_STATUS_FIELDS, default="UNKNOWN")
        raw_filled = _find_first_value(order, *FILLED_QUANTITY_FIELDS, default=None)
        filled_quantity = (
            _coerce_number(raw_filled, "filled quantity")
            if raw_filled not in (None, "") else 0.0
        )
        raw_order_quantity = _find_first_value(
            order, *ORDER_QUANTITY_FIELDS, default=None
        )
        order_quantity = (
            _coerce_number(raw_order_quantity, "order quantity")
            if raw_order_quantity not in (None, "") else None
        )
        if filled_quantity < 0:
            raise BrokerValidationError(
                f"Webull returned a negative filled quantity: {filled_quantity}"
            )
        return OrderStatus(
            client_order_id=client_order_id,
            status=str(status),
            filled_quantity=filled_quantity,
            raw_response=order,
            order_quantity=order_quantity,
        )

    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
    ) -> OrderResult:
        """Place a market order and return a structured ``OrderResult``.

        Submitted exactly once — deliberately NOT wrapped in ``_retry``.
        Webull does not silently dedupe resubmissions of the same
        ``client_order_id``: it rejects them with HTTP 417
        OPENAPI_REPEAT_REQUEST ("Please don't tap repeatedly."), and a 5xx
        timeout on a submit is ambiguous (the order may already have
        landed). Resubmitting therefore risks either a hard 417 or a
        duplicate fill, so a transient failure here skips this tick's trade
        instead — consistent with the DNA step-per-time-slot model.
        """
        order_payload = self._build_market_order_payload(
            symbol=symbol.upper(),
            side=side.upper(),
            quantity=quantity,
            client_order_id=client_order_id,
        )

        # Preview-gate (the Manual Test Lab flow: preview, then submit only if
        # the preview is acceptable). This is what catches an order Webull will
        # not take — e.g. a fractional quantity or a closed market — *before* a
        # phantom "submitted" is logged against an unchanged position.
        try:
            preview_response = self._preview_market_order(order_payload)
        except BrokerHTTPError as exc:
            if exc.status_code < 500 and not _is_retryable_http(exc):
                _record_metric("errors")
                logger.warning(
                    "Order preview rejected (not placing): side=%s qty=%s "
                    "symbol=%s coid=%s http=%d",
                    side, quantity, symbol, client_order_id, exc.status_code,
                )
                return OrderResult(
                    client_order_id=client_order_id,
                    order_id=None,
                    status="PREVIEW_REJECTED",
                    preview={"accepted": False},
                    raw_response=None,
                    accepted=False,
                    reason=f"preview rejected (HTTP {exc.status_code})",
                )
            raise
        preview_reason = _preview_rejection_reason(preview_response)
        if preview_reason is not None:
            _record_metric("errors")
            logger.warning(
                "Order preview not acceptable (not placing): side=%s qty=%s "
                "symbol=%s coid=%s reason=%s",
                side, quantity, symbol, client_order_id, preview_reason,
            )
            return OrderResult(
                client_order_id=client_order_id,
                order_id=None,
                status="PREVIEW_REJECTED",
                preview=preview_response,
                raw_response=None,
                accepted=False,
                reason=preview_reason,
            )

        logger.info(
            "Placing order: side=%s qty=%s symbol=%s coid=%s",
            side, quantity, symbol, client_order_id,
        )
        try:
            response = _call_gateway(self.gateway.place_market_order, order_payload)
        except Exception:
            _record_metric("errors")
            # Once the place call begins, a timeout/5xx/protocol failure cannot
            # prove whether Webull booked the order. Preserve the intent and
            # reconcile this exact client_order_id; never submit a replacement.
            raise OrderSubmissionUnknownError(
                "Webull market-order submission outcome is unknown"
            ) from None

        # Unlike list/detail responses, the official Thailand place response
        # is one flat object. Correlate direct scalar fields only; recursive
        # lookup could borrow an unrelated nested id.
        if not isinstance(response, dict):
            _record_metric("errors")
            raise OrderSubmissionUnknownError(
                "Webull place response did not correlate the submitted client_order_id"
            )
        response_client_order_id = response.get("client_order_id")
        if response_client_order_id != client_order_id:
            _record_metric("errors")
            raise OrderSubmissionUnknownError(
                "Webull place response did not correlate the submitted client_order_id"
            )

        raw_order_id = response.get("order_id")
        order_id = (
            raw_order_id.strip()
            if isinstance(raw_order_id, str) and raw_order_id.strip()
            else None
        )
        raw_status = response.get("status")
        if raw_status not in (None, "") and not (
            isinstance(raw_status, str) and raw_status.strip()
        ):
            _record_metric("errors")
            raise OrderSubmissionUnknownError(
                "Webull place response contained an invalid status"
            )
        status = (
            raw_status.strip()
            if isinstance(raw_status, str) and raw_status.strip()
            else ("SUBMITTED" if order_id else "UNKNOWN")
        )
        rejected = str(status).strip().upper() in REJECTED_ORDER_STATUSES
        if order_id is None and not rejected:
            _record_metric("errors")
            raise OrderSubmissionUnknownError(
                "Webull correlated place response did not contain an order_id"
            )
        accepted = order_id is not None and not rejected
        # Only a genuinely accepted order counts toward the placed metric —
        # otherwise the counter would imply trades that never hit the book.
        if accepted:
            _record_metric("orders_placed")
        else:
            _record_metric("errors")
            logger.warning(
                "Order not accepted by Webull: side=%s qty=%s symbol=%s "
                "coid=%s status=%s",
                side, quantity, symbol, client_order_id, status,
            )

        return OrderResult(
            client_order_id=client_order_id,
            order_id=order_id,
            status=str(status),
            preview=preview_response,
            raw_response=response,
            accepted=accepted,
            reason=None if accepted else "Webull rejected the correlated order",
        )

    # ---- private: data fetching -------------------------------------------

    @_retry()
    def _fetch_quantity(self, symbol: str) -> float:
        """Fetch position quantity for *symbol*."""
        positions_response = _call_gateway(self.gateway.get_positions)
        return self._extract_quantity(positions_response, symbol)

    @_retry()
    def _fetch_last_price(self, symbol: str) -> float:
        """Fetch last traded price for *symbol*."""
        quote_response = _call_gateway(self.gateway.get_quote, symbol)
        return self._extract_last_price(
            quote_response,
            symbol,
            self.config.support_trading_session,
        )

    @_retry()
    def _preview_market_order(self, order_payload: Any) -> Any:
        """Preview is idempotent and can safely recover from 417/5xx errors."""
        return _call_gateway(self.gateway.preview_market_order, order_payload)

    # ---- private: order building ------------------------------------------

    def _build_market_order_payload(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
    ) -> list[dict[str, str]]:
        if side not in {"BUY", "SELL"}:
            raise BrokerValidationError("side must be BUY or SELL")
        formatted_quantity = _format_order_quantity(quantity)

        return [
            {
                "combo_type": "NORMAL",
                "client_order_id": client_order_id,
                "symbol": symbol,
                # US stock order for a Webull TH account: instrument_type
                # "EQUITY", market "US". The bot places orders through the
                # v3 order API (WEBULL_API_VERSION defaults to "v3") — per
                # the SDK docstrings the v2 order endpoints support only
                # Webull HK/US, while v3 explicitly supports Webull TH. The
                # v3 SDK derives the `category` header from the body as
                # f"{market}_{instrument_type}" = "US_EQUITY", which the TH
                # endpoint accepts (verified by the Manual Test Lab, which
                # previews/places through order_v3 with this exact payload).
                "instrument_type": "EQUITY",
                "market": "US",
                "order_type": "MARKET",
                "quantity": formatted_quantity,
                "support_trading_session": self.config.support_trading_session,
                "side": side,
                "time_in_force": "DAY",
                "entrust_type": "QTY",
            }
        ]

    # ---- private: response parsing ----------------------------------------

    @staticmethod
    def _extract_quantity(response: Any, symbol: str) -> float:
        quantity = _extract_symbol_scoped_number(
            response,
            symbol,
            POSITION_QUANTITY_FIELDS,
            "position quantity",
        )
        if quantity is None:
            return 0.0
        if quantity < 0:
            raise BrokerValidationError(
                f"Webull returned a negative position quantity for {symbol}: {quantity}"
            )
        return quantity

    @staticmethod
    def _extract_last_price(
        response: Any,
        symbol: str,
        trading_session: str = "CORE",
    ) -> float:
        fields = SNAPSHOT_PRICE_FIELDS_BY_SESSION.get(trading_session.upper())
        if fields is None:
            raise BrokerValidationError("Unsupported trading session for quote parsing")

        # Explicit priority is session-aware and never falls back to the prior
        # close. During CORE, only the official live `price` can authorize a
        # trade; extended/overnight modes use their documented fields.
        matching_records = [
            record
            for record in _iter_dicts(response)
            if _normalize_symbol(record.get("symbol")) == _normalize_symbol(symbol)
        ]
        if len(matching_records) > 1:
            raise BrokerValidationError(
                f"Webull returned multiple snapshots for {_normalize_symbol(symbol)}"
            )
        for field_name in fields:
            if matching_records:
                raw_price = matching_records[0].get(field_name)
                if raw_price not in (None, ""):
                    return _coerce_number(raw_price, "last price")
                continue

            # Some SDK snapshots omit symbol because the request identifies it.
            candidates: list[float] = []
            for record in _iter_dicts(response):
                raw_price = record.get(field_name)
                if raw_price not in (None, ""):
                    candidates.append(_coerce_number(raw_price, "last price"))
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise BrokerValidationError(
                    f"Webull returned ambiguous {field_name} values"
                )
        return 0.0
