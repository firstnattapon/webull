"""Unified Webull broker — single source of truth for all API interactions.

Single ``WebullBroker`` class with:
  - retry (3x exponential backoff on HTTP 5xx / network errors)
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger("shannon_demon_dna.broker")

US_STOCK_CATEGORY = "US_STOCK"
ORDER_QUANTITY_DECIMAL_PRECISION = 5

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _response_json_or_raise(response: Any) -> Any:
    """Unwrap an SDK response — raise ``BrokerHTTPError`` on failure."""
    _record_metric("api_calls")
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return response

    if status_code < 200 or status_code >= 300:
        text = getattr(response, "text", repr(response))
        raise BrokerHTTPError(status_code, text)

    return response.json()


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
# Retry decorator — exponential backoff
# ---------------------------------------------------------------------------

def _retry(max_attempts: int = 3, base_delay: float = 0.5):
    """Decorator: retry on ``BrokerHTTPError`` with exponential backoff."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except BrokerHTTPError as exc:
                    last_exc = exc
                    if exc.status_code < 500:
                        _record_metric("errors")
                        raise  # 4xx = don't retry
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
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
                        delay = base_delay * (2 ** attempt)
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

    @_retry()
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

        if not math.isfinite(quantity):
            raise BrokerValidationError(
                f"Webull returned a non-finite quantity for {normalized}"
            )
        if not math.isfinite(last_price) or last_price <= 0:
            raise BrokerValidationError(
                f"Webull returned an invalid last price for {normalized}: {last_price}"
            )

        return MarketState(quantity=quantity, last_price=last_price)

    @_retry()
    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        client_order_id: str,
    ) -> OrderResult:
        """Place a market order and return a structured ``OrderResult``.

        Retrying a submit is safe because ``client_order_id`` is
        deterministic per (strategy, symbol, step) — Webull deduplicates
        resubmissions of the same client order id.
        """
        order_payload = self._build_market_order_payload(
            symbol=symbol.upper(),
            side=side.upper(),
            quantity=quantity,
            client_order_id=client_order_id,
        )
        order_api = self._order_api()

        preview_response = None
        if self.config.preview_orders:
            preview_response = _response_json_or_raise(
                _call_sdk(order_api.preview_order, self.account_id, order_payload)
            )

        logger.info(
            "Placing order: side=%s qty=%s symbol=%s coid=%s",
            side, quantity, symbol, client_order_id,
        )
        response = _response_json_or_raise(
            _call_sdk(order_api.place_order, self.account_id, order_payload)
        )
        _record_metric("orders_placed")

        return OrderResult(
            client_order_id=client_order_id,
            order_id=_find_first_value(response, "order_id", "orderId", "id"),
            status=_find_first_value(
                response, "status", "order_status", "orderStatus",
                default="UNKNOWN",
            ),
            preview=preview_response,
            raw_response=response,
        )

    # ---- private: data fetching -------------------------------------------

    def _fetch_quantity(self, symbol: str) -> float:
        """Fetch position quantity for *symbol*."""
        positions_response = _response_json_or_raise(
            _call_sdk(
                self.trade_client.account_v2.get_account_position,
                self.account_id,
            )
        )
        return self._extract_quantity(positions_response, symbol)

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
        for position in _iter_dicts(response):
            position_symbol = _get_value(
                position,
                "symbol", "ticker", "instrument_symbol", "instrumentSymbol",
            )
            if position_symbol is None or str(position_symbol).upper() != symbol:
                continue

            quantity = _get_value(
                position,
                "quantity", "qty", "position", "position_qty",
                "positionQty", "available_qty", "availableQty",
            )
            if quantity not in (None, ""):
                return float(quantity)

        return 0.0

    @staticmethod
    def _extract_last_price(response: Any, symbol: str) -> float:
        for quote in _iter_dicts(response):
            price = _get_value(
                quote,
                "last_price", "lastPrice", "last", "price",
                "close", "close_price", "closePrice", "pPrice",
            )
            if price in (None, ""):
                continue

            quote_symbol = _get_value(quote, "symbol", "ticker", default=symbol)
            if str(quote_symbol).upper() == symbol:
                return float(price)

        return 0.0
