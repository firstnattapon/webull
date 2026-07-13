"""Unified Webull broker — single source of truth for all API interactions.

Single ``WebullBroker`` class with:
  - retry for idempotent reads only (3x jittered backoff on HTTP 5xx,
    network errors, and Webull's 417 OPENAPI_REPEAT_REQUEST throttle);
    order placement is submitted exactly once and never resubmitted
  - shared I/O thread pool (position + price fetched concurrently,
    pool reused across invocations instead of rebuilt per call)
  - connection caching (module-level singleton, thread-safe)
  - lightweight metrics counters exposed via ``get_broker_metrics``
"""

from __future__ import annotations

import atexit
import functools
import logging
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("shannon_demon_dna.broker")

US_STOCK_CATEGORY = "US_STOCK"
ORDER_QUANTITY_DECIMAL_PRECISION = 5
OPEN_ORDER_PAGE_SIZE = 100

SYMBOL_FIELDS = (
    "symbol", "ticker", "instrument_symbol", "instrumentSymbol",
)
POSITION_QUANTITY_FIELDS = (
    "quantity", "qty", "position_quantity", "positionQuantity",
    "position_qty", "positionQty", "holding_quantity", "holdingQuantity",
    "net_quantity", "netQuantity", "available_qty", "availableQty",
)
LAST_PRICE_FIELDS = (
    "last_price", "lastPrice", "last", "price", "close",
    "close_price", "closePrice", "pPrice",
)
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
# A successful preview (the Manual Test Lab's "Preview completed" path) returns
# cost/fee estimates. Their presence is what distinguishes an acceptable order
# from an error envelope returned under HTTP 200.
PREVIEW_ESTIMATE_FIELDS = (
    "estimated_cost", "estimatedCost", "estimated_amount", "estimatedAmount",
    "estimated_transaction_fee", "estimatedTransactionFee",
    "estimated_quantity", "estimatedQuantity", "buying_power", "buyingPower",
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
        return asdict(self)


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

    @property
    def is_filled(self) -> bool:
        """Whether Webull reports a positive executed quantity.

        A status label alone is not enough for the trading log: the contract
        exposes ``filled_quantity`` specifically so an accepted order can be
        distinguished from one that actually changed the position.  If a
        transient/inconsistent response says ``FILLED`` with zero quantity,
        the caller keeps polling instead of recording a phantom fill.
        """
        return self.filled_quantity > 0

    @property
    def is_terminal_unfilled(self) -> bool:
        """Whether the order reached a dead end without executing anything."""
        return (
            self.filled_quantity <= 0
            and self.status.strip().upper() in REJECTED_ORDER_STATUSES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "is_filled": self.is_filled,
            "is_terminal_unfilled": self.is_terminal_unfilled,
            "raw_response": self.raw_response,
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


def _response_json_or_raise(response: Any) -> Any:
    """Unwrap an SDK response — raise ``BrokerHTTPError`` on failure."""
    _record_metric("api_calls")
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return response

    if status_code < 200 or status_code >= 300:
        text = getattr(response, "text", repr(response))
        raise BrokerHTTPError(status_code, text)

    try:
        return response.json()
    except Exception as exc:
        raise BrokerValidationError(
            f"Webull HTTP {status_code} returned invalid JSON"
        ) from exc


def _call_sdk(fn, /, *args, **kwargs):
    """Invoke a Webull SDK method, translating SDK exceptions to broker errors.

    The SDK never returns a non-2xx response object — ``client.get_response``
    raises ``ServerException`` (any upstream HTTP error, e.g. 504
    GATEWAY_TIMEOUT) or ``ClientException`` (network-level failure) instead.
    Without this translation those exceptions bypass ``_retry`` (which only
    understands broker exceptions) and surface as generic 500s in the handler
    instead of retryable ``BROKER_ERROR`` 502s.
    """
    from webull.core.exception.exceptions import ClientException, ServerException

    try:
        return fn(*args, **kwargs)
    except ServerException as exc:
        try:
            status_code = int(exc.http_status)
        except (TypeError, ValueError):
            status_code = 502  # unknown upstream failure — treat as retryable
        raise BrokerHTTPError(status_code, str(exc)) from exc
    except ClientException as exc:
        raise BrokerConnectionError(str(exc)) from exc


def _preview_rejection_reason(preview_response: Any) -> str | None:
    """Reason string if a preview response signals the order would be rejected.

    Webull answers an acceptable preview with cost/fee estimates (e.g.
    ``estimated_cost``, exactly what the Manual Test Lab shows on
    "Preview completed") and no terminal status. A rejected order — an invalid
    fractional quantity, a closed market, an unsellable position — comes back
    as an error envelope even under HTTP 200. Surfacing that reason lets the
    caller skip placement instead of logging a phantom submission.

    Conservative by design: only flags a rejection on an explicit terminal
    status, or an error message that arrives *without* any cost estimate, so a
    normal successful preview is never mistaken for a reject.
    """
    if preview_response is None:
        return None

    status = _find_first_value(preview_response, *ORDER_STATUS_FIELDS, default=None)
    if status not in (None, "") and str(status).strip().upper() in REJECTED_ORDER_STATUSES:
        return f"preview status {status}"

    has_estimate = _find_first_value(
        preview_response, *PREVIEW_ESTIMATE_FIELDS, default=None
    ) not in (None, "")
    if not has_estimate:
        reason = _find_first_value(preview_response, *ORDER_REASON_FIELDS, default=None)
        if reason not in (None, ""):
            return str(reason)
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
# Connection pool — shared I/O executor, reused across invocations
# ---------------------------------------------------------------------------

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=4,
                    thread_name_prefix="broker-io",
                )
                atexit.register(_shutdown_executor)
    return _executor


def _shutdown_executor() -> None:
    """Graceful shutdown — release worker threads at instance teardown."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
            _executor = None


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

    Supports configurable API versions (v2 / v3) and optional order preview.
    Includes retry logic, connection caching, and parallel data fetching.
    """

    def __init__(self, config):
        from webull.core.client import ApiClient
        from webull.data.data_client import DataClient
        from webull.trade.trade_client import TradeClient

        self.config = config
        self.account_id = config.account_id
        self.api_client = ApiClient(config.app_key, config.app_secret, config.region)
        self.api_client.add_endpoint(config.region, config.endpoint)
        if config.token_dir:
            self.api_client.set_token_dir(config.token_dir)

        self.data_client = DataClient(self.api_client)
        self.trade_client = TradeClient(self.api_client)

    # ---- public API -------------------------------------------------------

    def get_position_and_price(self, symbol: str) -> MarketState:
        """Fetch current position quantity and last price for *symbol*.

        Position and price are fetched concurrently on the shared pool.
        Raises ``BrokerValidationError`` when the upstream data is unusable
        (non-positive or non-finite price), so bad market data surfaces as
        a 502 instead of an internal error deeper in the strategy.
        """
        normalized = symbol.upper()

        pool = _get_executor()
        future_qty = pool.submit(self._fetch_quantity, normalized)
        future_price = pool.submit(self._fetch_last_price, normalized)

        quantity = float(future_qty.result())
        last_price = float(future_price.result())

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

    @_retry()
    def has_open_order(self, symbol: str) -> bool:
        """Return whether Webull reports an active order for *symbol*.

        The order-open endpoint is the same account/order read used by the
        Manual Test Lab. This prevents another DNA tick from stacking a new
        order while a previously accepted order has not filled yet.
        """
        normalized = _normalize_symbol(symbol)
        order_api = self._order_api()
        response = _response_json_or_raise(
            _call_sdk(
                order_api.get_order_open,
                self.account_id,
                page_size=OPEN_ORDER_PAGE_SIZE,
            )
        )
        return any(
            _normalize_symbol(_get_value(item, *SYMBOL_FIELDS, default=""))
            == normalized
            for item in _iter_dicts(response)
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
        order_api = self._order_api()
        response = _response_json_or_raise(
            _call_sdk(
                order_api.get_order_detail,
                self.account_id,
                client_order_id,
            )
        )
        status = _find_first_value(response, *ORDER_STATUS_FIELDS, default="UNKNOWN")
        raw_filled = _find_first_value(response, *FILLED_QUANTITY_FIELDS, default=None)
        filled_quantity = (
            _coerce_number(raw_filled, "filled quantity")
            if raw_filled not in (None, "") else 0.0
        )
        if filled_quantity < 0:
            raise BrokerValidationError(
                f"Webull returned a negative filled quantity: {filled_quantity}"
            )
        return OrderStatus(
            client_order_id=client_order_id,
            status=str(status),
            filled_quantity=filled_quantity,
            raw_response=response,
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
        order_api = self._order_api()

        # Preview-gate (the Manual Test Lab flow: preview, then submit only if
        # the preview is acceptable). This is what catches an order Webull will
        # not take — e.g. a fractional quantity or a closed market — *before* a
        # phantom "submitted" is logged against an unchanged position.
        preview_response = None
        if self.config.preview_orders:
            try:
                preview_response = self._preview_market_order(order_api, order_payload)
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
                        preview={"error": exc.body},
                        raw_response=None,
                        accepted=False,
                        reason=f"preview rejected (HTTP {exc.status_code}): "
                        f"{str(exc.body)[:300]}",
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
        response = _response_json_or_raise(
            _call_sdk(order_api.place_order, self.account_id, order_payload)
        )

        order_id = _find_first_value(response, *ORDER_ID_FIELDS)
        status = _find_first_value(
            response, *ORDER_STATUS_FIELDS,
            default="SUBMITTED" if order_id else "UNKNOWN",
        )
        accepted = order_id is not None and str(status).strip().upper() not in (
            REJECTED_ORDER_STATUSES
        )
        # Only a genuinely accepted order counts toward the placed metric —
        # otherwise the counter would imply trades that never hit the book.
        if accepted:
            _record_metric("orders_placed")
        else:
            _record_metric("errors")
            logger.warning(
                "Order not accepted by Webull: side=%s qty=%s symbol=%s "
                "coid=%s status=%s response=%r",
                side, quantity, symbol, client_order_id, status, response,
            )

        return OrderResult(
            client_order_id=client_order_id,
            order_id=order_id,
            status=str(status),
            preview=preview_response,
            raw_response=response,
            accepted=accepted,
            reason=None if accepted else _find_first_value(
                response, *ORDER_REASON_FIELDS,
                default="Webull returned no order id",
            ),
        )

    # ---- private: data fetching -------------------------------------------

    @_retry()
    def _fetch_quantity(self, symbol: str) -> float:
        """Fetch position quantity for *symbol*."""
        positions_response = _response_json_or_raise(
            _call_sdk(
                self.trade_client.account_v2.get_account_position,
                self.account_id,
            )
        )
        return self._extract_quantity(positions_response, symbol)

    @_retry()
    def _fetch_last_price(self, symbol: str) -> float:
        """Fetch last traded price for *symbol*."""
        quote_response = _response_json_or_raise(
            _call_sdk(
                self.data_client.market_data.get_snapshot,
                symbol,
                US_STOCK_CATEGORY,
                extend_hour_required=False,
                overnight_required=False,
            )
        )
        return self._extract_last_price(quote_response, symbol)

    @_retry()
    def _preview_market_order(self, order_api: Any, order_payload: Any) -> Any:
        """Preview is idempotent and can safely recover from 417/5xx errors."""
        return _response_json_or_raise(
            _call_sdk(order_api.preview_order, self.account_id, order_payload)
        )

    # ---- private: order building ------------------------------------------

    def _order_api(self) -> Any:
        api_name = f"order_{self.config.api_version}"
        order_api = getattr(self.trade_client, api_name, None)
        if order_api is None:
            raise BrokerError(f"Webull SDK does not expose {api_name}")
        return order_api

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
    def _extract_last_price(response: Any, symbol: str) -> float:
        price = _extract_symbol_scoped_number(
            response,
            symbol,
            LAST_PRICE_FIELDS,
            "last price",
        )
        if price is not None:
            return price

        # Snapshot responses from some SDK versions omit the symbol because
        # the request already identifies it. Accept only an unambiguous price.
        prices: list[float] = []
        for record in reversed(list(_iter_dicts(response))):
            raw_price = _get_value(record, *LAST_PRICE_FIELDS, default=None)
            if raw_price not in (None, ""):
                prices.append(_coerce_number(raw_price, "last price"))
        return prices[0] if len(prices) == 1 else 0.0
