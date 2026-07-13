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

BOT_VERSION = "3.1.0"
_started_at = time.time()

# Webull limits Order Detail and Account Positions to two requests per two
# seconds. Three bounded reads, spaced just over one second apart, give a
# market order time to leave SUBMITTED and give the position snapshot time to
# catch up without violating that documented rate limit. All reads are
# best-effort and order placement is never repeated.
FILL_VERIFICATION_ATTEMPTS = 3
FILL_VERIFICATION_INTERVAL_SECONDS = 1.05
POSITION_RECONCILIATION_ATTEMPTS = 3
POSITION_RECONCILIATION_INTERVAL_SECONDS = 1.05
POSITION_RECONCILIATION_EPSILON = 0.02

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

    # Surface the resolved trading environment without touching credentials, so
    # a UAT deployment (which accepts orders but never moves a real position) is
    # obvious at a glance rather than a surprise in the trade log.
    try:
        broker_config = load_broker_config(load_app_config().project_id)
        trading_environment = broker_config.environment_label
        is_production = broker_config.is_production
    except Exception:
        trading_environment = "unknown"
        is_production = False

    healthy = all(value.startswith("ok") for value in checks.values())
    body = {
        "status": "HEALTHY" if healthy else "UNHEALTHY",
        "version": BOT_VERSION,
        "uptime_seconds": round(time.time() - _started_at, 1),
        "market_open": is_us_market_open(),
        "trading_environment": trading_environment,
        "checks": checks,
        "metrics": {
            "handler": get_handler_metrics(),
            "broker": get_broker_metrics(),
        },
    }
    if not is_production:
        body["warning"] = (
            f"trading_environment={trading_environment}: orders are accepted but "
            "do not affect a real position; set WEBULL_ENV=prod to trade for real"
        )
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


def _verify_fill(broker, client_order_id: str):
    """Poll an order's real fill state on a bounded, best-effort basis.

    Verification is diagnostic, not part of placing the order, so a failure to
    read the order back must not fail the tick or roll back the reserved DNA
    step. A market order commonly appears as SUBMITTED immediately after
    placement, so one instantaneous read is not enough to distinguish "still
    processing" from "never filled". Returns the latest ``OrderStatus`` read,
    or ``None`` when no read could be completed.
    """
    latest = None
    for attempt in range(1, FILL_VERIFICATION_ATTEMPTS + 1):
        try:
            latest = broker.get_order_status(client_order_id)
        except Exception:
            # SDK/network failures should already be BrokerError subclasses,
            # but this guard keeps verification genuinely best-effort even if
            # an unexpected response shape slips through.
            logger.warning(
                "Could not verify fill for order %s (attempt %d/%d)",
                client_order_id,
                attempt,
                FILL_VERIFICATION_ATTEMPTS,
                exc_info=True,
            )
        else:
            if latest.is_filled or latest.is_terminal_unfilled:
                return latest

        if attempt < FILL_VERIFICATION_ATTEMPTS:
            time.sleep(FILL_VERIFICATION_INTERVAL_SECONDS)

    return latest


def _reconcile_position(
    broker,
    *,
    symbol: str,
    side: str,
    position_before: float,
    filled_quantity: float,
) -> dict[str, Any]:
    """Read Positions until it reflects a verified fill, without raising.

    This is the automated equivalent of the Manual Test Lab's second check:
    after inspecting Order detail, read Positions and compare the actual
    holding with the expected BUY/SELL delta. The final observed quantity is
    returned even when the snapshot is still lagging, so the log never invents
    a position movement.
    """
    normalized_side = side.strip().upper()
    direction = 1.0 if normalized_side == "BUY" else -1.0
    expected_after = position_before + direction * filled_quantity
    result: dict[str, Any] = {
        "position_before": position_before,
        "expected_position_after": expected_after,
        "position_after": None,
        "position_delta": None,
        "position_reconciled": False,
        "position_reconcile_epsilon": POSITION_RECONCILIATION_EPSILON,
    }

    last_error: Exception | None = None
    for attempt in range(1, POSITION_RECONCILIATION_ATTEMPTS + 1):
        try:
            position_after = float(broker.get_position_quantity(symbol))
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Could not reconcile position for %s (attempt %d/%d)",
                symbol,
                attempt,
                POSITION_RECONCILIATION_ATTEMPTS,
                exc_info=True,
            )
        else:
            last_error = None
            result["position_after"] = position_after
            result["position_delta"] = position_after - position_before
            if abs(position_after - expected_after) <= POSITION_RECONCILIATION_EPSILON:
                result["position_reconciled"] = True
                return result

        if attempt < POSITION_RECONCILIATION_ATTEMPTS:
            time.sleep(POSITION_RECONCILIATION_INTERVAL_SECONDS)

    if result["position_after"] is None and last_error is not None:
        result["position_reconcile_error"] = str(last_error)[:300]
    else:
        result["position_reconcile_note"] = (
            "Order detail reports a fill, but the latest Positions snapshot "
            "has not reached the expected quantity yet"
        )
    return result


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

    # last_price / quantity are duplicated at the top level because the
    # dashboard reads the trade log through pd.json_normalize(sep="_") and
    # looks the price up by column name — nested-only fields would surface
    # as market_state_last_price and miss its TRADE_PRICE_COLUMNS lookup.
    trade_log_base = {
        **reserved.to_dict(),
        "last_price": market_state.last_price,
        "quantity": market_state.quantity,
        "market_state": market_state.to_dict(),
        "decision": decision_data,
        "baseline_pnl": decision.baseline_pnl,
        # Record which environment the order is routed to. A UAT sandbox
        # accepts orders but never moves the real position, so without this
        # a sell logged here looks identical to a real fill that did nothing.
        "broker_environment": broker_config.environment_label,
        "broker_endpoint": broker_config.endpoint,
        "is_production": broker_config.is_production,
    }

    if decision.action == "PASS":
        _log_trade(config, {
            **trade_log_base,
            "status": "PASS_THRESHOLD",
        })
        return _ok(
            "PASS_THRESHOLD",
            dna_step=dna_step,
            dna_signal=current_signal,
            decision=decision_data,
        )

    # Account / Orders guard: position snapshots can lag an accepted order.
    # Do not stack another rebalance while Webull still reports an open order
    # for this symbol; the next DNA tick will recalculate from a fresh position.
    if broker.has_open_order(config.symbol):
        _log_trade(config, {
            **trade_log_base,
            "status": "PASS_OPEN_ORDER",
        })
        return _ok(
            "PASS_OPEN_ORDER",
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

    # Log the real outcome. Webull can answer HTTP 200 without booking the
    # order (rejects, or a UAT sandbox echo); marking those ORDER_SUBMITTED is
    # what made a sell appear in the log while the held quantity never changed.
    order_accepted = getattr(
        order_result, "accepted", order_result.order_id is not None
    )

    order_log = {
        **trade_log_base,
        "client_order_id": client_order_id,
        "order_result": order_result.to_dict(),
    }

    if not order_accepted:
        order_log["status"] = "ORDER_REJECTED"
        _log_trade(config, order_log)
        return _ok(
            "ORDER_REJECTED",
            dna_step=dna_step,
            dna_signal=current_signal,
            decision=decision_data,
            order=order_result.to_dict(),
        )

    # Accepted is NOT the same as filled. This is the crux of the reported bug:
    # a (fractional) order is accepted with a real id, logged as submitted, then
    # cancelled/expired unfilled — so the held quantity never moves. Verify the
    # real fill with the same order-detail read the Manual Test Lab exposes, and
    # log ORDER_FILLED / ORDER_NOT_FILLED accordingly instead of a blind
    # ORDER_SUBMITTED. Verification is diagnostic only — a failure to read it
    # back must never fail the tick or roll back the DNA step.
    fill = _verify_fill(broker, client_order_id)
    if fill is None:
        log_status = "ORDER_SUBMITTED"
        order_log["order_detail_verified"] = False
        order_log["fill_verified"] = False
    else:
        order_log["order_status"] = fill.to_dict()
        order_log["filled_quantity"] = fill.filled_quantity
        order_log["order_detail_verified"] = True
        order_log["fill_verified"] = fill.is_filled
        if fill.is_filled:
            log_status = "ORDER_FILLED"
            reconciliation = _reconcile_position(
                broker,
                symbol=config.symbol,
                side=decision.side or decision.action,
                position_before=market_state.quantity,
                filled_quantity=fill.filled_quantity,
            )
            order_log.update(reconciliation)

            # The existing Webull_Dashboard reads
            # ``market_state_quantity`` as "จำนวนถือครอง (หุ้น)". Once the
            # Manual-style Positions read succeeds, publish that latest real
            # snapshot in the legacy field so the same order row updates on
            # the dashboard. Preserve the decision-time snapshot separately;
            # strategy calculations above always use this pre-order value.
            position_after = reconciliation["position_after"]
            if position_after is not None:
                order_log["pre_order_market_state"] = market_state.to_dict()
                post_order_market_state = {
                    "quantity": position_after,
                    "last_price": market_state.last_price,
                }
                order_log["post_order_market_state"] = post_order_market_state
                order_log["quantity"] = position_after
                order_log["market_state"] = post_order_market_state
        elif fill.is_terminal_unfilled:
            log_status = "ORDER_NOT_FILLED"
            order_log["non_fill_verified"] = True
            order_log["not_filled_reason"] = (
                f"Webull accepted the order but it reached status "
                f"{fill.status!r} with 0 filled — the held quantity did not move"
            )
        else:
            # Accepted and resting (working / pending), not yet executed.
            log_status = "ORDER_SUBMITTED"
            order_log["non_fill_verified"] = False

    order_log["status"] = log_status

    # A UAT sandbox accepts orders but never fills against a real position, so
    # an accepted (or even "filled") order there still leaves the real holding
    # untouched. Spell that out on every non-production order so the log is
    # unambiguous about why the quantity may not move.
    if not broker_config.is_production:
        order_log["sandbox_note"] = (
            "UAT sandbox: order accepted but the real position is not affected; "
            "set WEBULL_ENV=prod to trade a real position"
        )

    _log_trade(config, order_log)

    # A definitive non-fill surfaces in the HTTP status too; a fill or a
    # still-working order is reported as OK.
    body_status = "OK" if log_status != "ORDER_NOT_FILLED" else "ORDER_NOT_FILLED"
    response_observations = {
        key: order_log[key]
        for key in (
            "filled_quantity",
            "position_before",
            "position_after",
            "position_delta",
            "expected_position_after",
            "position_reconciled",
            "sandbox_note",
        )
        if key in order_log
    }
    return _ok(
        body_status,
        dna_step=dna_step,
        dna_signal=current_signal,
        decision=decision_data,
        order=order_result.to_dict(),
        order_log_status=log_status,
        **response_observations,
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




    # Failure semantics: the reserved DNA step is intentionally NOT rolled
    # back here. DNA indices are trained per scheduler time slot, so the
    # pointer must advance exactly once per tick; a failed execution skips
    # its signal rather than replaying it out of its slot (see reserve_step).
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
