"""Shannon Demon DNA — Cloud Function entry point.

This is a slim orchestrator: all business logic lives in dedicated modules.
The handler chains early-exit checks from cheapest to most expensive:

    timestamp → market hours → DNA step → DNA signal → broker trade

Each exit path returns a structured JSON response with an HTTP status code.

Additions over v2:
  - GET /health (or ?health=1) — config validation + metrics, no trading
  - per-status invocation metrics, exposed on the health endpoint
  - transactional step reservation (race-free across concurrent instances)
  - background trade logging with a bounded flush before every response,
    so log writes survive Cloud Run's post-response CPU throttling
  - numpy/dna_engine loaded lazily, off the early-exit path
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import functions_framework
from flask import jsonify

from broker import BrokerError, get_broker, get_broker_metrics
from config import AppConfig, load_app_config, load_broker_config, validate_startup
from market_utils import is_us_market_open
from state import (
    StepReservation,
    flush_trade_logs,
    reserve_step,
    write_trade_log,
)
from strategy import (
    RebalanceDecision,
    calculate_shannon_decision,
    generate_client_order_id,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shannon_demon_dna")

BOT_VERSION = "3.0.0"
_started_at = time.time()

# ---------------------------------------------------------------------------
# DNA cache — decoded once per cold start, reused on warm starts
# ---------------------------------------------------------------------------

_dna_lock = threading.Lock()
_cached_dna_code: str | None = None
_cached_dna_array: Any = None


def _get_dna_array(dna_code: str):
    """Return the decoded DNA array, caching across warm starts."""
    global _cached_dna_code, _cached_dna_array
    if _cached_dna_code == dna_code and _cached_dna_array is not None:
        return _cached_dna_array

    with _dna_lock:
        if _cached_dna_code == dna_code and _cached_dna_array is not None:
            return _cached_dna_array

        # Lazy import keeps numpy off the early-exit path (gates 1-2).
        from dna_engine import decode_dna

        array = decode_dna(dna_code)
        _cached_dna_array = array
        _cached_dna_code = dna_code
        return array


# ---------------------------------------------------------------------------
# Metrics — per-status counters for the health endpoint
# ---------------------------------------------------------------------------

_metrics_lock = threading.Lock()
_invocations = 0
_errors = 0
_status_counts: dict[str, int] = {}


def _record_status(status: str, is_error: bool = False) -> None:
    global _invocations, _errors
    with _metrics_lock:
        _invocations += 1
        if is_error:
            _errors += 1
        _status_counts[status] = _status_counts.get(status, 0) + 1


def get_handler_metrics() -> dict[str, Any]:
    """Snapshot of handler counters since this instance cold-started."""
    with _metrics_lock:
        return {
            "invocations": _invocations,
            "errors": _errors,
            "statuses": dict(_status_counts),
        }


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(status: str, **details: Any):
    _record_status(status)
    return jsonify({"status": status, **details}), 200


def _error(status: str, http_status: int, **details: Any):
    _record_status(status, is_error=True)
    return jsonify({"status": status, **details}), http_status


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _is_health_request(request) -> bool:
    path = (getattr(request, "path", "") or "").rstrip("/")
    if path.endswith("/health"):
        return True
    return request.args.get("health", "").lower() in {"1", "true"}


def _health_response():
    """Config validation + metrics snapshot. Never touches broker or state."""
    checks = validate_startup()

    try:
        config = load_app_config()
        dna_array = _get_dna_array(config.dna_code)
        checks["dna"] = f"ok (length={len(dna_array)})"
    except Exception as exc:
        checks["dna"] = f"error: {exc}"

    healthy = all(value.startswith("ok") for value in checks.values())
    body = {
        "status": "HEALTHY" if healthy else "UNHEALTHY",
        "version": BOT_VERSION,
        "uptime_seconds": round(time.time() - _started_at, 1),
        "market_open": is_us_market_open(),
        "checks": checks,
        "metrics": {
            "handler": get_handler_metrics(),
            "broker": get_broker_metrics(),
        },
    }
    return jsonify(body), (200 if healthy else 503)


# ---------------------------------------------------------------------------
# Trade log helper
# ---------------------------------------------------------------------------

def _log_trade(config: AppConfig, payload: dict[str, Any]) -> None:
    """Fire-and-forget trade log — never raises."""
    write_trade_log(
        project_id=config.project_id,
        trade_collection=config.firestore_trade_collection,
        strategy_id=config.strategy_id,
        symbol=config.symbol,
        state_document=config.firestore_state_document,
        payload=payload,
        state_collection=config.firestore_state_collection,
    )


# ---------------------------------------------------------------------------
# Decision payload helper
# ---------------------------------------------------------------------------

def _decision_payload(decision: RebalanceDecision) -> dict[str, Any]:
    return decision.to_dict()


# ---------------------------------------------------------------------------
# Signal execution
# ---------------------------------------------------------------------------

def _execute_signal(config: AppConfig, reserved: StepReservation):
    """Execute one reserved DNA signal and preserve the public response shape."""
    dna_step = reserved.dna_step
    current_signal = reserved.dna_signal

    if current_signal == 0:
        return _ok(
            "PASS_DNA_ZERO",
            dna_step=dna_step,
            dna_signal=current_signal,
        )

    broker_config = load_broker_config(config.project_id)
    broker = get_broker(broker_config)
    market_state = broker.get_position_and_price(config.symbol)

    decision = calculate_shannon_decision(
        quantity=market_state.quantity,
        last_price=market_state.last_price,
        fix_c=config.fix_c,
        p0=config.p0,
        diff=config.diff,
    )
    decision_data = _decision_payload(decision)

    if decision.action == "PASS":
        _log_trade(config, {
            **reserved.to_dict(),
            "status": "PASS_THRESHOLD",
            "market_state": market_state.to_dict(),
            "decision": decision_data,
            "baseline_pnl": decision.baseline_pnl,
        })
        return _ok(
            "PASS_THRESHOLD",
            dna_step=dna_step,
            dna_signal=current_signal,
            decision=decision_data,
        )

    client_order_id = generate_client_order_id(
        config.strategy_id,
        config.symbol,
        dna_step,
    )
    order_result = broker.place_market_order(
        symbol=config.symbol,
        side=decision.side or decision.action,
        quantity=decision.order_quantity,
        client_order_id=client_order_id,
    )

    _log_trade(config, {
        **reserved.to_dict(),
        "status": "ORDER_SUBMITTED",
        "client_order_id": client_order_id,
        "market_state": market_state.to_dict(),
        "decision": decision_data,
        "order_result": order_result.to_dict(),
        "baseline_pnl": decision.baseline_pnl,
    })

    return _ok(
        "OK",
        dna_step=dna_step,
        dna_signal=current_signal,
        decision=decision_data,
        order=order_result.to_dict(),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@functions_framework.http
def rebalance_trigger(request):
    """HTTP Cloud Function — Shannon Demon DNA rebalance trigger.

    Early-exit chain (cheapest first):
    1. start_timestamp — pure math, no I/O
    2. market hours — pure math, no I/O
    3. DNA step — 1 Firestore transaction (race-free reservation)
    4. DNA signal — array lookup
    5. broker trade — Webull API calls (most expensive)
    """
    if _is_health_request(request):
        return _health_response()

    config: AppConfig | None = None
    reserved: StepReservation | None = None

    try:
        # -- Gate 1: timestamp (zero I/O) ----------------------------------
        config = load_app_config()
        if time.time() < config.start_timestamp:
            return _ok(
                "PASS_WAITING_TO_START",
                start_timestamp=config.start_timestamp,
            )

        # -- Gate 2: market hours (zero I/O) --------------------------------
        if not is_us_market_open():
            return _ok("PASS_MARKET_CLOSED")

        # -- Gate 3: DNA step (1 Firestore transaction) ----------------------
        dna_array = _get_dna_array(config.dna_code)
        reservation = reserve_step(
            project_id=config.project_id,
            collection=config.firestore_state_collection,
            document=config.firestore_state_document,
            strategy_id=config.strategy_id,
            symbol=config.symbol,
            dna_length=len(dna_array),
            signal_of=lambda step: int(dna_array[step]),
        )
        dna_step = reservation.dna_step

        if dna_step >= len(dna_array):
            return _ok(
                "TIMELINE_ENDED",
                dna_step=dna_step,
                dna_length=int(len(dna_array)),
            )

        reserved = reservation

        return _execute_signal(config, reserved)




    except BrokerError as exc:
        logger.exception("Broker error during rebalance")
        _try_log_error(config, reserved, exc)
        return _error(
            "BROKER_ERROR",
            http_status=502,
            error_type=exc.__class__.__name__,
            message=str(exc),
        )

    except Exception as exc:
        logger.exception("Rebalance trigger failed")
        _try_log_error(config, reserved, exc)
        return _error(
            "ERROR",
            http_status=500,
            error_type=exc.__class__.__name__,
            message=str(exc),
        )

    finally:
        # Graceful shutdown guard: Cloud Run throttles CPU once the response
        # is sent, so give background log writes a bounded window to land.
        # No-op (returns immediately) when nothing is pending.
        flush_trade_logs(timeout=3.0)


def _try_log_error(
    config: AppConfig | None,
    reserved: StepReservation | None,
    exc: Exception,
) -> None:
    """Best-effort error logging — never raises."""
    if config is None or reserved is None:
        return
    try:
        _log_trade(config, {
            **reserved.to_dict(),
            "status": "ERROR",
            "error_type": exc.__class__.__name__,
            "error_message": str(exc)[:1000],
        })
    except Exception:
        logger.exception("Failed to write error log")
