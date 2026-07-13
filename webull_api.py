"""Small, official-SDK gateway for the Webull Thailand OpenAPI.

The gateway deliberately exposes the same primitive operations as the manual
test lab.  It does not retry order submissions, cache response bodies, or log
SDK exceptions.  Callers receive the JSON returned by Webull and can make
their own policy decisions above this transport boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from config import WEBULL_TRADING_SESSIONS
from webull.core.client import ApiClient
from webull.core.exception.exceptions import ClientException, ServerException
from webull.data.quotes.market_data import MarketData
from webull.trade.trade_client import TradeClient


US_STOCK_CATEGORY = "US_STOCK"
MIN_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100
TRADING_SESSION_SNAPSHOT_FLAGS = {
    "CORE": ("false", "false"),
    "ALL": ("true", "false"),
    "NIGHT": ("false", "true"),
    "ALL_DAY": ("true", "true"),
}

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SAFE_ERROR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class WebullApiError(RuntimeError):
    """Base class for errors emitted by :class:`WebullApiGateway`."""


class WebullApiValidationError(WebullApiError, ValueError):
    """A caller supplied an invalid config value or API argument."""


class WebullApiUpstreamError(WebullApiError):
    """The SDK or Webull rejected an otherwise valid gateway operation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


class WebullApiProtocolError(WebullApiError):
    """Webull returned a response that cannot be treated as JSON safely."""


def _required_config_text(config: Any, name: str) -> str:
    value = getattr(config, name, None)
    if not isinstance(value, str) or not value or value != value.strip():
        raise WebullApiValidationError(f"config.{name} must be a non-empty string")
    return value


def _validate_endpoint(value: str) -> str:
    if (
        "://" in value
        or "/" in value
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise WebullApiValidationError("config.endpoint must be a bare host name")
    return value


def _validate_page_size(page_size: int) -> int:
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE
    ):
        raise WebullApiValidationError("page_size must be an integer from 10 to 100")
    return page_size


def _validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise WebullApiValidationError("symbol must be a string")
    normalized = symbol.strip().upper()
    if not _SYMBOL_RE.fullmatch(normalized):
        raise WebullApiValidationError("symbol is invalid")
    return normalized


def _validate_client_order_id(client_order_id: str) -> str:
    if (
        not isinstance(client_order_id, str)
        or not _CLIENT_ORDER_ID_RE.fullmatch(client_order_id)
    ):
        raise WebullApiValidationError(
            "client_order_id must be 1-32 letters, digits, hyphens, or underscores"
        )
    return client_order_id


def _validate_start_date(start_date: str) -> str:
    if not isinstance(start_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date):
        raise WebullApiValidationError("start_date must use YYYY-MM-DD")
    try:
        parsed = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        raise WebullApiValidationError("start_date must be a real calendar date") from None
    today = date.today()
    cutoff_month = today.month - 6
    cutoff_year = today.year
    if cutoff_month <= 0:
        cutoff_month += 12
        cutoff_year -= 1
    cutoff = date(
        cutoff_year,
        cutoff_month,
        min(today.day, monthrange(cutoff_year, cutoff_month)[1]),
    )
    if parsed < cutoff or parsed > today:
        raise WebullApiValidationError(
            "start_date must be within the last six months and not in the future"
        )
    return start_date


def _validate_trading_session(value: Any) -> str:
    if not isinstance(value, str):
        raise WebullApiValidationError("config.support_trading_session is invalid")
    session = value.strip().upper()
    if session not in WEBULL_TRADING_SESSIONS:
        raise WebullApiValidationError(
            "config.support_trading_session must be NIGHT, ALL, CORE, or ALL_DAY"
        )
    return session


def _validate_positive_number(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or value in (None, ""):
        raise WebullApiValidationError(f"{field_name} must be greater than zero")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise WebullApiValidationError(f"{field_name} must be greater than zero") from None
    if not number.is_finite() or number <= 0:
        raise WebullApiValidationError(f"{field_name} must be greater than zero")


def _validate_order_payload(
    payload: Any,
    *,
    expected_order_type: str | None = None,
) -> list[dict[str, Any]]:
    """Validate one NORMAL US equity order used by preview/place."""
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise WebullApiValidationError("payload must contain exactly one order object")

    order = payload[0]
    _validate_client_order_id(order.get("client_order_id"))
    _validate_symbol(order.get("symbol"))

    fixed_fields = {
        "combo_type": "NORMAL",
        "instrument_type": "EQUITY",
        "market": "US",
    }
    for field_name, expected in fixed_fields.items():
        if order.get(field_name) != expected:
            raise WebullApiValidationError(f"{field_name} must be {expected}")

    if order.get("side") not in {"BUY", "SELL"}:
        raise WebullApiValidationError("side must be BUY or SELL")
    order_type = order.get("order_type")
    if order_type not in {"MARKET", "LIMIT"}:
        raise WebullApiValidationError("order_type must be MARKET or LIMIT")
    if expected_order_type is not None and order_type != expected_order_type:
        raise WebullApiValidationError(f"order_type must be {expected_order_type}")
    if order.get("time_in_force") not in {"DAY", "GTC"}:
        raise WebullApiValidationError("time_in_force must be DAY or GTC")
    if order.get("support_trading_session") not in WEBULL_TRADING_SESSIONS:
        raise WebullApiValidationError("support_trading_session is invalid")

    entrust_type = order.get("entrust_type")
    if entrust_type == "QTY":
        _validate_positive_number(order.get("quantity"), "quantity")
    elif entrust_type == "AMOUNT":
        _validate_positive_number(order.get("total_cash_amount"), "total_cash_amount")
    else:
        raise WebullApiValidationError("entrust_type must be QTY or AMOUNT")

    if order_type == "MARKET":
        if (
            order.get("limit_price") not in (None, "")
            or order.get("stop_price") not in (None, "")
        ):
            raise WebullApiValidationError(
                "market orders must not include limit_price or stop_price"
            )
    else:
        _validate_positive_number(order.get("limit_price"), "limit_price")
        if order.get("stop_price") not in (None, ""):
            raise WebullApiValidationError("limit orders must not include stop_price")

    # Validation is intentionally non-mutating.  The exact object used for
    # preview can therefore be passed unchanged to place_order.
    return payload


def _safe_error_token(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_ERROR_TOKEN_RE.fullmatch(value):
        return value
    return None


def _sanitized_sdk_error(operation: str, exc: Exception) -> WebullApiUpstreamError:
    """Build an error without copying SDK messages, bodies, headers, or secrets."""
    if isinstance(exc, ServerException):
        try:
            status = int(exc.http_status)
        except (TypeError, ValueError):
            status = 502
        code = _safe_error_token(getattr(exc, "error_code", None))
        suffix = f", code={code}" if code else ""
        return WebullApiUpstreamError(
            f"{operation} failed (HTTP {status}{suffix})",
            status_code=status,
            error_code=code,
        )

    if isinstance(exc, ClientException):
        code = _safe_error_token(getattr(exc, "error_code", None))
        suffix = f" ({code})" if code else ""
        return WebullApiUpstreamError(
            f"{operation} failed: SDK connection error{suffix}",
            error_code=code,
        )

    return WebullApiUpstreamError(f"{operation} failed")


def _json_value_or_error(operation: str, value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise WebullApiProtocolError(f"{operation} returned non-JSON data") from None
    return value


def _response_json(operation: str, response: Any) -> Any:
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        if isinstance(status_code, bool):
            raise WebullApiProtocolError(f"{operation} returned an invalid HTTP status")
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            raise WebullApiProtocolError(f"{operation} returned an invalid HTTP status") from None
        if not 200 <= status_code < 300:
            # Never include response.text: business errors can echo request
            # bodies or headers and therefore credentials.
            raise WebullApiUpstreamError(
                f"{operation} failed (HTTP {status_code})",
                status_code=status_code,
            )

        try:
            value = response.json()
        except Exception:
            failure = WebullApiProtocolError(
                f"{operation} returned invalid JSON (HTTP {status_code})"
            )
        else:
            failure = None
        if failure is not None:
            # Raise outside the except block so the original decoder error is
            # not retained as an inspectable exception context.
            raise failure
        return _json_value_or_error(operation, value)

    return _json_value_or_error(operation, response)


def find_order_by_client_order_id(
    response: Any,
    client_order_id: str,
) -> dict[str, Any] | None:
    """Return the one exact match found inside documented ``orders`` arrays.

    Group-level IDs are deliberately ignored.  Matching is case-sensitive and
    uses equality, never prefixes or fuzzy traversal.  Duplicate exact matches
    are rejected because selecting either could reconcile the wrong fill.
    """
    target = _validate_client_order_id(client_order_id)
    if isinstance(response, dict):
        groups = [response]
    elif isinstance(response, list):
        groups = response
    else:
        raise WebullApiProtocolError(
            "order response must be one group object or an array of groups"
        )

    matches: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise WebullApiProtocolError("order groups must be objects")
        nested_orders = group.get("orders")
        if not isinstance(nested_orders, list):
            raise WebullApiProtocolError("orders must be an array")
        for order in nested_orders:
            if not isinstance(order, dict):
                raise WebullApiProtocolError("orders must contain objects")
            if order.get("client_order_id") == target:
                matches.append(order)

    if len(matches) > 1:
        raise WebullApiProtocolError("multiple orders matched client_order_id")
    return matches[0] if matches else None


class WebullApiGateway:
    """Low-level JSON gateway built on Webull SDK 2.x for Thailand."""

    # Expose the correlation helper on the gateway without binding ``self``.
    find_order_by_client_order_id = staticmethod(find_order_by_client_order_id)

    def __init__(self, config: Any):
        app_key = _required_config_text(config, "app_key")
        app_secret = _required_config_text(config, "app_secret")
        account_id = _required_config_text(config, "account_id")
        region = _required_config_text(config, "region").lower()
        endpoint = _validate_endpoint(_required_config_text(config, "endpoint"))
        support_trading_session = _validate_trading_session(
            getattr(config, "support_trading_session", None)
        )
        token_dir = getattr(config, "token_dir", None)
        if token_dir is not None and (not isinstance(token_dir, str) or not token_dir.strip()):
            raise WebullApiValidationError("config.token_dir must be a non-empty path or None")

        failure: WebullApiUpstreamError | None = None
        try:
            api_client = ApiClient(app_key, app_secret, region)

            # Endpoint selection must happen before TradeClient's sole
            # ClientInitializer call, otherwise /openapi/config can hit prod.
            api_client.add_endpoint(region, endpoint)
            if token_dir is None:
                binding = hashlib.sha256(
                    f"{region}:{endpoint}:{app_key}".encode("utf-8")
                ).hexdigest()[:16]
                token_dir = os.path.join(
                    tempfile.gettempdir(),
                    "webull-openapi",
                    binding,
                )
            api_client.set_token_dir(token_dir)

            # TradeClient otherwise installs console and rotating file handlers
            # before initialization.  The SDK has no public "disable logging"
            # switch; these are the two flags its _init_logger checks.  Set both
            # before construction so no logger or webull_trade_sdk.log is made.
            api_client._stream_logger_set = True
            api_client._file_logger_set = True

            # The SDK's module loggers propagate to the application's root
            # logger even when its own auto handlers are disabled.  Signed
            # request headers can otherwise reach Cloud Logging.  Silence the
            # SDK namespace and surface only sanitized gateway exceptions.
            for logger_name in ("webull", "webull.core"):
                sdk_logger = logging.getLogger(logger_name)
                sdk_logger.handlers.clear()
                sdk_logger.addHandler(logging.NullHandler())
                sdk_logger.setLevel(logging.CRITICAL + 1)
                sdk_logger.propagate = False

            # Exactly one SDK initializer call.  Constructing DataClient here
            # would invoke it a second time, so expose MarketData directly.
            trade_client = TradeClient(api_client)
            market_data = MarketData(api_client)
        except Exception as exc:
            failure = _sanitized_sdk_error("Webull SDK initialization", exc)

        if failure is not None:
            raise failure

        self.api_client = api_client
        self.trade_client = trade_client
        self.market_data = market_data
        self.account_id = account_id
        self.support_trading_session = support_trading_session
        self._call_lock = threading.RLock()

    def _call_json(
        self,
        operation: str,
        function: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        # Cloud Run can execute concurrent requests in one process while the
        # cached broker shares one SDK ApiClient. The SDK does not document
        # request signing as thread-safe, so serialize all calls per gateway.
        with self._call_lock:
            failure: WebullApiUpstreamError | None = None
            try:
                response = function(*args, **kwargs)
            except Exception as exc:
                failure = _sanitized_sdk_error(operation, exc)
                response = None

            if failure is not None:
                # Raising after leaving the except block prevents the raw SDK
                # exception (and its potentially sensitive message) being chained.
                raise failure
            return _response_json(operation, response)

    def get_account_list(self) -> Any:
        return self._call_json(
            "get_account_list",
            self.trade_client.account_v2.get_account_list,
        )

    def get_account_balance(self) -> Any:
        return self._call_json(
            "get_account_balance",
            self.trade_client.account_v2.get_account_balance,
            self.account_id,
        )

    def get_positions(self) -> Any:
        return self._call_json(
            "get_positions",
            self.trade_client.account_v2.get_account_position,
            self.account_id,
        )

    def get_quote(self, symbol: str) -> Any:
        normalized = _validate_symbol(symbol)
        extend_hour_required, overnight_required = (
            TRADING_SESSION_SNAPSHOT_FLAGS[self.support_trading_session]
        )
        return self._call_json(
            "get_quote",
            self.market_data.get_snapshot,
            normalized,
            US_STOCK_CATEGORY,
            extend_hour_required=extend_hour_required,
            overnight_required=overnight_required,
        )

    def get_position_and_quote(self, symbol: str) -> dict[str, Any]:
        normalized = _validate_symbol(symbol)
        return {
            "positions": self.get_positions(),
            "quote": self.get_quote(normalized),
        }

    def get_open_orders(
        self,
        page_size: int = 20,
        last_client_order_id: str | None = None,
    ) -> Any:
        page_size = _validate_page_size(page_size)
        kwargs: dict[str, Any] = {"page_size": page_size}
        if last_client_order_id is not None:
            kwargs["last_client_order_id"] = _validate_client_order_id(
                last_client_order_id
            )
        return self._call_json(
            "get_open_orders",
            self.trade_client.order_v3.get_order_open,
            self.account_id,
            **kwargs,
        )

    def get_order_history(
        self,
        page_size: int = 20,
        start_date: str | None = None,
        last_client_order_id: str | None = None,
    ) -> Any:
        page_size = _validate_page_size(page_size)
        kwargs: dict[str, Any] = {"page_size": page_size}
        if start_date is not None:
            kwargs["start_date"] = _validate_start_date(start_date)
        if last_client_order_id is not None:
            kwargs["last_client_order_id"] = _validate_client_order_id(
                last_client_order_id
            )

        # Thailand's official schema has no end_date.  Do not pass the generic
        # SDK argument, even as None.
        return self._call_json(
            "get_order_history",
            self.trade_client.order_v3.get_order_history,
            self.account_id,
            **kwargs,
        )

    def get_order_detail(self, client_order_id: str) -> Any:
        client_order_id = _validate_client_order_id(client_order_id)
        return self._call_json(
            "get_order_detail",
            self.trade_client.order_v3.get_order_detail,
            self.account_id,
            client_order_id,
        )

    def preview_market_order(self, payload: Any) -> Any:
        orders = _validate_order_payload(payload, expected_order_type="MARKET")
        return self._call_json(
            "preview_market_order",
            self.trade_client.order_v3.preview_order,
            self.account_id,
            orders,
        )

    def place_market_order(self, payload: Any) -> Any:
        orders = _validate_order_payload(payload, expected_order_type="MARKET")
        return self._call_json(
            "place_market_order",
            self.trade_client.order_v3.place_order,
            self.account_id,
            orders,
        )

    def preview_order(self, payload: Any) -> Any:
        orders = _validate_order_payload(payload)
        return self._call_json(
            "preview_order",
            self.trade_client.order_v3.preview_order,
            self.account_id,
            orders,
        )

    def place_order(self, payload: Any) -> Any:
        orders = _validate_order_payload(payload)
        return self._call_json(
            "place_order",
            self.trade_client.order_v3.place_order,
            self.account_id,
            orders,
        )

    def cancel_order(self, client_order_id: str) -> Any:
        client_order_id = _validate_client_order_id(client_order_id)
        return self._call_json(
            "cancel_order",
            self.trade_client.order_v3.cancel_order,
            self.account_id,
            client_order_id,
        )
