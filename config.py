from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import Any


WEBULL_TRADING_ENDPOINTS: dict[str, str] = {
    "uat": "th-api.uat.webullbroker.com",
    "prod": "api.webull.co.th",
}

# Map WEBULL_ENV to the SDK region_id used in HMAC signature computation.
# Both UAT and prod targets are Thailand endpoints, so the region is "th".
WEBULL_ENV_TO_REGION: dict[str, str] = {
    "uat": "th",
    "prod": "th",
}

DEFAULT_DNA_CODE = "26021034252903219354832053493"
DEFAULT_START_TIMESTAMP = 0  # 0 = start immediately; set START_TIMESTAMP env var for future date


@dataclass(frozen=True)
class AppConfig:
    project_id: str
    strategy_id: str
    symbol: str
    fix_c: float
    p0: float
    diff: float
    dna_code: str
    start_timestamp: int
    firestore_state_collection: str
    firestore_trade_collection: str
    firestore_state_document: str

    @property
    def dna_string(self) -> str:
        return self.dna_code


def _mask_secret(value: str) -> str:
    """Redact a credential, keeping the last 4 chars for identification."""
    if len(value) <= 8:
        return "***"
    return f"***{value[-4:]}"


@dataclass(frozen=True)
class BrokerConfig:
    # Credentials are excluded from repr so the dataclass can never leak
    # secrets through logging / error formatting.
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_id: str = field(repr=False)
    region: str
    endpoint: str
    token_dir: str | None
    api_version: str
    support_trading_session: str
    preview_orders: bool

    @property
    def is_production(self) -> bool:
        """Whether orders are routed to the live production endpoint.

        The bot defaults ``WEBULL_ENV`` to ``uat``, whose sandbox accepts
        orders (returning an id) but never mutates the real position — a SELL
        then shows in the trade log while the held quantity stays put. Surfacing
        this on every order log makes a sandbox no-op impossible to mistake for
        a real fill.
        """
        return self.endpoint == WEBULL_TRADING_ENDPOINTS["prod"]

    @property
    def environment_label(self) -> str:
        return "prod" if self.is_production else "uat"

    def safe_dict(self) -> dict[str, Any]:
        """Loggable view with credentials redacted."""
        return {
            "app_key": _mask_secret(self.app_key),
            "app_secret": "***",
            "account_id": _mask_secret(self.account_id),
            "region": self.region,
            "endpoint": self.endpoint,
            "token_dir": self.token_dir,
            "api_version": self.api_version,
            "support_trading_session": self.support_trading_session,
            "preview_orders": self.preview_orders,
        }


def _get_project_id() -> str:
    project_id = (
        os.environ.get("GCP_PROJECT_ID", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    )
    if not project_id:
        raise ValueError("Missing required env var: GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT")
    return project_id


def _get_float(name: str, default: str) -> float:
    raw_value = os.environ.get(name, default).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    return value


def _get_int(name: str, default: str) -> int:
    raw_value = os.environ.get(name, default).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _required_text(name: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"Missing required env var: {name}")
    return cleaned


def _is_valid_dna_code(value: str) -> bool:
    if value.isdigit():
        return True
    if value.lower().startswith("bypass:"):
        raw_length = value.split(":", 1)[1].strip()
        try:
            return int(raw_length) > 0
        except ValueError:
            return False
    if value.startswith("["):
        try:
            values = json.loads(value)
        except json.JSONDecodeError:
            return False
        return (
            isinstance(values, list)
            and len(values) == 2
            and type(values[0]) is int
            and type(values[1]) is int
            and values[0] == 1
            and values[1] > 0
        )
    return False


def _get_direct_credential(env_name: str, legacy_env_name: str) -> str:
    direct_value = os.environ.get(env_name, "").strip()
    if direct_value:
        return direct_value

    legacy_value = os.environ.get(legacy_env_name, "").strip()
    if legacy_value:
        return legacy_value

    raise ValueError(f"Missing required env var: {env_name} or {legacy_env_name}")


def _get_webull_endpoint(webull_env: str) -> str:
    endpoint = os.environ.get("WEBULL_TRADING_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    if webull_env not in WEBULL_TRADING_ENDPOINTS:
        valid_envs = ", ".join(sorted(WEBULL_TRADING_ENDPOINTS))
        raise ValueError(
            "WEBULL_ENV must be one of "
            f"{valid_envs}, or WEBULL_TRADING_ENDPOINT must be set"
        )
    return WEBULL_TRADING_ENDPOINTS[webull_env]


# ---------------------------------------------------------------------------
# Config cache — env vars are fixed per deployment, so both configs are
# built once per cold start and reused on every warm invocation.
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()
_cached_app_config: AppConfig | None = None
_cached_broker_config: BrokerConfig | None = None


def load_app_config(use_cache: bool = True) -> AppConfig:
    """Load non-secret runtime config for the early-exit path (cached)."""
    global _cached_app_config
    if use_cache and _cached_app_config is not None:
        return _cached_app_config

    with _config_lock:
        if use_cache and _cached_app_config is not None:
            return _cached_app_config
        config = _build_app_config()
        _cached_app_config = config
        return config


def _build_app_config() -> AppConfig:
    project_id = _get_project_id()
    strategy_id = os.environ.get("STRATEGY_ID", "SHANNON_DEMON_DNA").strip()
    symbol = os.environ.get("SYMBOL", "AAPL").strip().upper()
    dna_code = (
        os.environ.get("DNA_STRING", "").strip()
        or os.environ.get("DNA_CODE", DEFAULT_DNA_CODE).strip()
    )
    start_timestamp = _get_int("START_TIMESTAMP", str(DEFAULT_START_TIMESTAMP))

    fix_c = _get_float("FIX_C", "1500.0")
    p0 = _get_float("P0", "6.88")
    diff = _get_float("DIFF", "60.0")

    if fix_c <= 0:
        raise ValueError("FIX_C must be greater than 0")
    if p0 <= 0:
        raise ValueError("P0 must be greater than 0")
    if diff < 0:
        raise ValueError("DIFF must be greater than or equal to 0")
    if not _is_valid_dna_code(dna_code):
        raise ValueError("DNA_STRING or DNA_CODE must be digits, bypass:N, or [1,N]")
    if start_timestamp < 0:
        raise ValueError("START_TIMESTAMP must be greater than or equal to 0")

    state_collection = os.environ.get(
        "FIRESTORE_STATE_COLLECTION",
        "shannon_demon_state",
    ).strip()
    trade_collection = os.environ.get(
        "FIRESTORE_TRADE_COLLECTION",
        "shannon_demon_trades",
    ).strip()
    state_document = os.environ.get("FIRESTORE_STATE_DOCUMENT", "").strip()
    if not state_document:
        state_document = f"{strategy_id}_{symbol}"

    return AppConfig(
        project_id=project_id,
        strategy_id=_required_text("STRATEGY_ID", strategy_id),
        symbol=_required_text("SYMBOL", symbol),
        fix_c=fix_c,
        p0=p0,
        diff=diff,
        dna_code=dna_code,
        start_timestamp=start_timestamp,
        firestore_state_collection=_required_text(
            "FIRESTORE_STATE_COLLECTION",
            state_collection,
        ),
        firestore_trade_collection=_required_text(
            "FIRESTORE_TRADE_COLLECTION",
            trade_collection,
        ),
        firestore_state_document=_required_text(
            "FIRESTORE_STATE_DOCUMENT",
            state_document,
        ),
    )


def load_broker_config(
    project_id: str | None = None,
    use_cache: bool = True,
) -> BrokerConfig:
    """Load broker config only after DNA permits execution.

    Cached per cold start so warm-start trades reuse the same parsed
    environment values. Updating credentials takes effect on the next
    cold start or redeploy, which is the standard Cloud Run model.
    """
    global _cached_broker_config
    if use_cache and _cached_broker_config is not None:
        return _cached_broker_config

    with _config_lock:
        if use_cache and _cached_broker_config is not None:
            return _cached_broker_config
        config = _build_broker_config(project_id)
        _cached_broker_config = config
        return config


def _build_broker_config(project_id: str | None = None) -> BrokerConfig:
    webull_env = os.environ.get("WEBULL_ENV", "uat").strip().lower()
    # Default to the v3 order API. Per the SDK docstrings the v2 order
    # endpoints are supported only for Webull HK/US, while v3 explicitly
    # supports Webull TH (this deployment's region). v3 derives the
    # `category` header ("US_EQUITY") from the order body, which the TH
    # endpoint accepts — verified working against th-api UAT by the
    # Manual Test Lab dashboard. Overridable via WEBULL_API_VERSION.
    api_version = os.environ.get("WEBULL_API_VERSION", "v3").strip().lower()
    if api_version not in {"v2", "v3"}:
        raise ValueError("WEBULL_API_VERSION must be v2 or v3")

    return BrokerConfig(
        app_key=_get_direct_credential(
            "WEBULL_APP_KEY",
            "WEBULL_APP_KEY_SECRET_ID",
        ),
        app_secret=_get_direct_credential(
            "WEBULL_APP_SECRET",
            "WEBULL_APP_SECRET_ID",
        ),
        account_id=_get_direct_credential(
            "WEBULL_ACCOUNT_ID",
            "WEBULL_ACCOUNT_ID_SECRET_ID",
        ),
        region=os.environ.get("WEBULL_REGION", "").strip().lower()
        or WEBULL_ENV_TO_REGION.get(webull_env, "us"),
        endpoint=_get_webull_endpoint(webull_env),
        token_dir=os.environ.get("WEBULL_TOKEN_DIR", "").strip() or None,
        api_version=api_version,
        support_trading_session=os.environ.get(
            "WEBULL_SUPPORT_TRADING_SESSION",
            "CORE",
        ).strip().upper(),
        # Preview-then-place by default (the Manual Test Lab flow): the preview
        # catches an order Webull would reject — e.g. a fractional quantity or a
        # closed market — before a phantom "submitted" is logged. Set
        # WEBULL_PREVIEW_ORDERS=false to place without previewing.
        preview_orders=_get_bool("WEBULL_PREVIEW_ORDERS", default=True),
    )


def validate_startup() -> dict[str, str]:
    """Validate the deployed configuration without touching external services.

    Used by the health check endpoint: verifies the app config parses,
    every broker credential has a direct environment value,
    and the Webull endpoint / API version resolve. Each check maps to
    either ``"ok"``/``"ok (...)"`` or ``"error: ..."``. Credential values
    are never read or included.
    """
    checks: dict[str, str] = {}

    try:
        load_app_config()
        checks["app_config"] = "ok"
    except Exception as exc:
        checks["app_config"] = f"error: {exc}"

    credential_sources = (
        ("webull_app_key", "WEBULL_APP_KEY", "WEBULL_APP_KEY_SECRET_ID"),
        ("webull_app_secret", "WEBULL_APP_SECRET", "WEBULL_APP_SECRET_ID"),
        ("webull_account_id", "WEBULL_ACCOUNT_ID", "WEBULL_ACCOUNT_ID_SECRET_ID"),
    )
    for check_name, env_name, secret_env_name in credential_sources:
        has_source = bool(
            os.environ.get(env_name, "").strip()
            or os.environ.get(secret_env_name, "").strip()
        )
        checks[check_name] = (
            "ok" if has_source
            else f"error: set {env_name} or {secret_env_name}"
        )

    webull_env = os.environ.get("WEBULL_ENV", "uat").strip().lower()
    try:
        checks["webull_endpoint"] = f"ok ({_get_webull_endpoint(webull_env)})"
    except Exception as exc:
        checks["webull_endpoint"] = f"error: {exc}"

    api_version = os.environ.get("WEBULL_API_VERSION", "v3").strip().lower()
    checks["webull_api_version"] = (
        "ok" if api_version in {"v2", "v3"}
        else "error: WEBULL_API_VERSION must be v2 or v3"
    )

    return checks


def get_config() -> dict[str, Any]:
    """Backward-compatible merged config for local scripts/tests."""
    app_config = load_app_config()
    broker_config = load_broker_config(app_config.project_id)
    return {
        **asdict(app_config),
        "dna_string": app_config.dna_string,
        **asdict(broker_config),
    }
