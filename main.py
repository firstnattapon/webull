"""Shannon Demon DNA — Cloud Function entry point.

This is a slim orchestrator: all business logic lives in dedicated modules.
The handler chains early-exit checks from cheapest to most expensive:

    pending order → timestamp → market hours → DNA step → DNA signal → broker trade

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
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

import functions_framework
from flask import jsonify

from broker import (
    BrokerError,
    OrderSubmissionUnknownError,
    get_broker,
    get_broker_metrics,
)
from config import AppConfig, load_app_config, load_broker_config, validate_startup
from market_utils import is_us_market_open
from state import (
    LifecycleConflictError,
    StepReservation,
    flush_trade_logs,
    read_pending_order,
    reserve_step,
    write_order_lifecycle,
    write_trade_log,
)
from strategy import (
    RebalanceDecision,
    calculate_shannon_decision,
    generate_client_order_id,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("shannon_demon_dna")

BOT_VERSION = "4.0.1"
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
# Reconciliation stays pending until Webull Positions confirms the fill.  The
# cycle threshold is diagnostic only: it raises a manual-review flag but never
# releases the safety gate or authorizes another order.
POSITION_RECONCILIATION_ALERT_CYCLES = 5
# Orders are formatted to five decimal places. Keep tolerance below the
# smallest meaningful 0.00001-share movement so an unchanged snapshot can
# never validate a fractional fill.
POSITION_RECONCILIATION_EPSILON = 0.000001

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
            f"trading_environment={trading_environment}: UAT uses simulated/shared "
            "account data and cannot prove production cash or position movement"
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
        except Exception as exc:
            # SDK/network failures should already be BrokerError subclasses,
            # but this guard keeps verification genuinely best-effort even if
            # an unexpected response shape slips through.
            if isinstance(exc, BrokerError):
                # get_order_status already exhausted the broker retry policy.
                # Re-entering it from this poll loop would multiply one
                # upstream outage into as many as nine Order Detail calls.
                logger.warning(
                    "Could not verify fill for order %s (%s)",
                    client_order_id,
                    exc.__class__.__name__,
                )
                break
            logger.warning(
                "Could not verify fill for order %s (attempt %d/%d)",
                client_order_id,
                attempt,
                FILL_VERIFICATION_ATTEMPTS,
                exc_info=True,
            )
        else:
            if (
                latest.is_filled
                or latest.is_terminal_unfilled
                or (latest.is_terminal and latest.has_fill)
            ):
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
        "position_sync_status": "PENDING",
        "position_reconcile_epsilon": POSITION_RECONCILIATION_EPSILON,
    }

    last_error: Exception | None = None
    for attempt in range(1, POSITION_RECONCILIATION_ATTEMPTS + 1):
        try:
            position_after = float(broker.get_position_quantity(symbol))
        except Exception as exc:
            last_error = exc
            if isinstance(exc, BrokerError):
                # The broker read already exhausted its own retry policy. Do
                # not multiply three broker attempts by three reconciliation
                # attempts, and do not send an expected transient traceback to
                # Cloud Error Reporting.
                logger.warning(
                    "Could not reconcile position for %s (%s)",
                    symbol,
                    exc.__class__.__name__,
                )
            else:
                logger.warning(
                    "Could not reconcile position for %s",
                    symbol,
                    exc_info=True,
                )
            break
        else:
            last_error = None
            result["position_after"] = position_after
            result["position_delta"] = position_after - position_before
            actual_delta = position_after - position_before
            moved_in_expected_direction = direction * actual_delta > 0
            if (
                moved_in_expected_direction
                and abs(actual_delta - direction * filled_quantity)
                <= POSITION_RECONCILIATION_EPSILON
            ):
                result["position_reconciled"] = True
                result["position_sync_status"] = "CONFIRMED"
                return result

        if attempt < POSITION_RECONCILIATION_ATTEMPTS:
            time.sleep(POSITION_RECONCILIATION_INTERVAL_SECONDS)

    if result["position_after"] is None and last_error is not None:
        # Persist only the exception class. Even unexpected library errors may
        # carry response bodies or signed request context in their message.
        result["position_reconcile_error"] = last_error.__class__.__name__
        result["position_sync_status"] = "UNAVAILABLE"
    else:
        result["position_reconcile_note"] = (
            "Order detail reports a fill, but the latest Positions snapshot "
            "has not reached the expected quantity yet"
        )
        result["position_sync_status"] = "MISMATCH"
    return result


def _terminal_fill_lifecycle_status(
    reconciliation: dict[str, Any],
    *,
    partial: bool,
    previous_cycles: int = 0,
) -> tuple[str, bool, int]:
    """Keep a verified fill pending until Positions confirms its exact delta.

    Order detail can prove that execution is terminal, but it cannot prove the
    current holding.  Unavailable or stale Positions observations therefore
    remain nonterminal for the strategy gate and are retried on later scheduler
    invocations.  ``cycles`` is used only for observability/manual escalation.
    """
    cycles = max(0, previous_cycles) + 1
    if reconciliation.get("position_reconciled") is True:
        status = "ORDER_PARTIAL_FILLED_TERMINAL" if partial else "ORDER_FILLED"
        return status, True, cycles
    pending = (
        "ORDER_PARTIAL_POSITION_PENDING"
        if partial
        else "ORDER_FILLED_POSITION_PENDING"
    )
    return pending, False, cycles


def _pending_number(pending: dict[str, Any], name: str) -> float:
    try:
        value = float(pending[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"pending_order.{name} must be a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"pending_order.{name} must be finite")
    return value


def _reconcile_pending_lifecycle(
    config: AppConfig,
    broker_config,
    broker,
    pending: dict[str, Any],
) -> dict[str, Any]:
    """Refresh one durable order through detail + positions.

    This is the automated form of the Manual Lab sequence.  It runs on later
    scheduler invocations until an order reaches a terminal state and at least
    one post-order Positions snapshot is observable.  Partial fills remain
    pending; no replacement order is submitted.
    """
    client_order_id = str(pending.get("client_order_id", "")).strip()
    if not client_order_id:
        raise ValueError("pending_order.client_order_id is required")

    expected_environment = str(pending.get("broker_environment", "")).strip()
    if not expected_environment:
        raise BrokerError("Pending order identity is missing broker_environment")
    if expected_environment != broker_config.environment_label:
        raise BrokerError(
            "Pending order environment does not match the current Webull environment"
        )
    expected_account = str(pending.get("account_fingerprint", "")).strip()
    current_account = str(
        getattr(broker_config, "account_fingerprint", "")
    ).strip()
    if not expected_account or not current_account:
        raise BrokerError("Pending order identity is missing account_fingerprint")
    if expected_account != current_account:
        raise BrokerError(
            "Pending order account does not match the current Webull account"
        )
    expected_endpoint = str(pending.get("broker_endpoint", "")).strip()
    if not expected_endpoint:
        raise BrokerError("Pending order identity is missing broker_endpoint")
    if expected_endpoint != broker_config.endpoint:
        raise BrokerError(
            "Pending order broker_endpoint does not match the current Webull endpoint"
        )

    pending_identity = {
        "strategy_id": str(pending.get("strategy_id", "")).strip(),
        "symbol": str(pending.get("symbol", "")).strip().upper(),
        "trade_collection": str(pending.get("trade_collection", "")).strip(),
        "state_document": str(pending.get("state_document", "")).strip(),
    }
    runtime_identity = {
        "strategy_id": config.strategy_id,
        "symbol": config.symbol.upper(),
        "trade_collection": config.firestore_trade_collection,
        "state_document": config.firestore_state_document,
    }
    for identity_name, runtime_value in runtime_identity.items():
        pending_value = pending_identity[identity_name]
        if not pending_value:
            raise BrokerError(
                f"Pending order identity is missing {identity_name}"
            )
        if pending_value != runtime_value:
            raise BrokerError(
                f"Pending order {identity_name} does not match runtime configuration"
            )

    side = str(pending.get("side", "")).strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("pending_order.side must be BUY or SELL")
    position_before = _pending_number(pending, "position_before")
    order_quantity = _pending_number(pending, "order_quantity")
    last_price = _pending_number(pending, "last_price")
    if position_before < 0:
        raise ValueError("pending_order.position_before cannot be negative")
    if order_quantity <= 0:
        raise ValueError("pending_order.order_quantity must be greater than zero")
    if last_price <= 0:
        raise ValueError("pending_order.last_price must be greater than zero")

    pending_status = str(pending.get("status", "")).strip().upper()
    if pending_status in {
        "ORDER_CREATED",
        "ORDER_SUBMIT_UNKNOWN",
        "ORDER_SUBMIT_UNKNOWN_NOT_FOUND",
    }:
        detail = broker.lookup_order_status(client_order_id)
        if detail is None:
            try:
                not_found_attempts = int(pending.get("not_found_attempts", 0)) + 1
            except (TypeError, ValueError):
                not_found_attempts = 1
            unresolved_payload = {
                "status": "ORDER_SUBMIT_UNKNOWN_NOT_FOUND",
                "lifecycle_outcome": "ORDER_SUBMISSION_UNRESOLVED",
                "manual_resolution_required": True,
                "not_found_attempts": not_found_attempts,
                "submission_unknown_at": pending.get("submission_unknown_at"),
                "order_id": pending.get("order_id"),
                "side": side,
                "order_quantity": order_quantity,
                "filled_quantity": float(pending.get("filled_quantity", 0) or 0),
                "position_before": position_before,
                "last_price": last_price,
                "broker_environment": broker_config.environment_label,
                "broker_endpoint": broker_config.endpoint,
                "account_fingerprint": current_account,
            }
            _write_lifecycle(config, client_order_id, unresolved_payload)
            return {
                "client_order_id": client_order_id,
                "status": "ORDER_SUBMIT_UNKNOWN_NOT_FOUND",
                "outcome": "ORDER_SUBMISSION_UNRESOLVED",
                "terminal": False,
                "filled_quantity": unresolved_payload["filled_quantity"],
                "position_after": None,
                "position_reconciled": False,
                "manual_resolution_required": True,
                "not_found_attempts": not_found_attempts,
            }
    else:
        detail = broker.get_order_status(client_order_id)
    upstream_status = detail.normalized_status or "SUBMITTED"
    lifecycle_status = upstream_status
    outcome = "ORDER_PENDING"
    terminal = False
    reconciliation: dict[str, Any] = {}

    if detail.has_fill:
        reconciliation = _reconcile_position(
            broker,
            symbol=config.symbol,
            side=side,
            position_before=position_before,
            filled_quantity=detail.filled_quantity,
        )

    try:
        previous_reconcile_cycles = int(
            pending.get("position_reconcile_cycles", 0)
        )
    except (TypeError, ValueError):
        previous_reconcile_cycles = 0

    if detail.is_filled:
        outcome = "ORDER_FILLED"
        lifecycle_status, terminal, reconcile_cycles = (
            _terminal_fill_lifecycle_status(
                reconciliation,
                partial=False,
                previous_cycles=previous_reconcile_cycles,
            )
        )
        reconciliation["position_reconcile_cycles"] = reconcile_cycles
        reconciliation["manual_resolution_required"] = (
            reconcile_cycles >= POSITION_RECONCILIATION_ALERT_CYCLES
        )
    elif detail.is_terminal_unfilled:
        outcome = "ORDER_NOT_FILLED"
        terminal = True
        lifecycle_status = "ORDER_NOT_FILLED"
    elif detail.is_terminal and detail.has_fill:
        outcome = "ORDER_PARTIAL_FILLED"
        lifecycle_status, terminal, reconcile_cycles = (
            _terminal_fill_lifecycle_status(
                reconciliation,
                partial=True,
                previous_cycles=previous_reconcile_cycles,
            )
        )
        reconciliation["position_reconcile_cycles"] = reconcile_cycles
        reconciliation["manual_resolution_required"] = (
            reconcile_cycles >= POSITION_RECONCILIATION_ALERT_CYCLES
        )
    elif detail.is_partial_fill:
        outcome = "ORDER_PARTIAL_FILLED"
        lifecycle_status = "ORDER_PARTIAL_FILLED"
    elif detail.is_terminal:
        # For example FILLED with filled_quantity=0: do not clear the durable
        # intent on an internally inconsistent response; observe it again.
        lifecycle_status = "ORDER_STATUS_INCONSISTENT"
    else:
        lifecycle_status = "ORDER_SUBMITTED"

    lifecycle_payload: dict[str, Any] = {
        "status": lifecycle_status,
        "lifecycle_outcome": outcome,
        "order_id": pending.get("order_id"),
        "side": side,
        "order_quantity": order_quantity,
        "filled_quantity": detail.filled_quantity,
        "position_before": position_before,
        "last_price": last_price,
        "broker_environment": broker_config.environment_label,
        "broker_endpoint": broker_config.endpoint,
        "account_fingerprint": current_account,
        "order_status": detail.to_dict(),
        "execution_terminal": detail.is_terminal,
        **reconciliation,
    }

    position_after = reconciliation.get("position_after")
    if position_after is not None:
        lifecycle_payload.update({
            "quantity": position_after,
            "market_state": {
                "quantity": position_after,
                "last_price": last_price,
            },
            "post_order_market_state": {
                "quantity": position_after,
                "last_price": last_price,
            },
        })

    _write_lifecycle(config, client_order_id, lifecycle_payload)
    return {
        "client_order_id": client_order_id,
        "status": lifecycle_status,
        "outcome": outcome,
        "terminal": terminal,
        "filled_quantity": detail.filled_quantity,
        "position_after": position_after,
        "position_reconciled": reconciliation.get("position_reconciled"),
        "position_sync_status": reconciliation.get("position_sync_status"),
        "manual_resolution_required": reconciliation.get(
            "manual_resolution_required", False
        ),
    }


def _reconcile_pending_before_signal(config: AppConfig):
    """Resume a pending order before market/DNA gates; return a response or None."""
    pending = read_pending_order(
        project_id=config.project_id,
        state_collection=config.firestore_state_collection,
        state_document=config.firestore_state_document,
    )
    if pending is None:
        return None

    broker_config = load_broker_config(config.project_id)
    broker = get_broker(broker_config)
    result = _reconcile_pending_lifecycle(
        config,
        broker_config,
        broker,
        pending,
    )
    body_status = "ORDER_RECONCILED" if result["terminal"] else "ORDER_PENDING"
    return _ok(body_status, order_lifecycle=result)


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
        # Record which environment the order is routed to. The documented UAT
        # account is shared/simulated, so its fills cannot be treated as proof
        # of production cash or position movement.
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
        time.time_ns(),
    )
    side = (decision.side or decision.action).upper()
    order_log = {
        **trade_log_base,
        "client_order_id": client_order_id,
        "side": side,
        "order_quantity": decision.order_quantity,
        "position_before": market_state.quantity,
        "pre_order_market_state": market_state.to_dict(),
        "account_fingerprint": str(
            getattr(broker_config, "account_fingerprint", "")
        ),
        "execution_terminal": False,
    }

    # Persist the intent before preview/submission.  If the process dies after
    # Webull accepts the request, the next invocation resumes this exact
    # client_order_id and never creates a replacement order blindly.
    _write_lifecycle(config, client_order_id, {
        **order_log,
        "status": "ORDER_CREATED",
    })

    try:
        order_result = broker.place_market_order(
            symbol=config.symbol,
            side=side,
            quantity=decision.order_quantity,
            client_order_id=client_order_id,
        )
    except OrderSubmissionUnknownError:
        # A submit timeout is ambiguous. Keep the intent nonterminal so detail
        # plus open/history can resolve it later; never resubmit automatically.
        try:
            _write_lifecycle(config, client_order_id, {
                **order_log,
                "status": "ORDER_SUBMIT_UNKNOWN",
                "submission_unknown_at": datetime.now(timezone.utc),
            })
        except Exception:
            logger.exception("Could not persist ambiguous order state")
        raise
    except Exception:
        # Preview/build failed before any place call was attempted. This outcome
        # is definitive and must clear the durable intent instead of blocking
        # every future scheduler tick as an allegedly ambiguous submission.
        try:
            _write_lifecycle(config, client_order_id, {
                **order_log,
                "status": "ORDER_PRE_SUBMIT_FAILED",
                "lifecycle_outcome": "ORDER_NOT_SUBMITTED",
            })
        except Exception:
            logger.exception("Could not persist pre-submit failure")
        raise

    # Log the real outcome. Webull can answer HTTP 200 without booking the
    # order (rejects, or a UAT sandbox echo); marking those ORDER_SUBMITTED is
    # what made a sell appear in the log while the held quantity never changed.
    order_accepted = getattr(
        order_result, "accepted", order_result.order_id is not None
    )

    order_log["order_result"] = order_result.to_dict()
    order_log["order_id"] = order_result.order_id

    if not order_accepted:
        order_log["status"] = "ORDER_REJECTED"
        _write_lifecycle(config, client_order_id, order_log)
        return _ok(
            "ORDER_REJECTED",
            dna_step=dna_step,
            dna_signal=current_signal,
            decision=decision_data,
            order=order_result.to_dict(),
        )

    _write_lifecycle(config, client_order_id, {
        **order_log,
        "status": "ORDER_SUBMITTED",
    })

    # Accepted is NOT the same as filled. This is the crux of the reported bug:
    # a (fractional) order is accepted with a real id, logged as submitted, then
    # cancelled/expired unfilled — so the held quantity never moves. Verify the
    # real fill with the same order-detail read the Manual Test Lab exposes, and
    # log ORDER_FILLED / ORDER_NOT_FILLED accordingly instead of a blind
    # ORDER_SUBMITTED. Verification is diagnostic only — a failure to read it
    # back must never fail the tick or roll back the DNA step.
    fill = _verify_fill(broker, client_order_id)
    if fill is None:
        lifecycle_status = "ORDER_SUBMITTED"
        order_log["order_detail_verified"] = False
        order_log["fill_verified"] = False
    else:
        order_log["order_status"] = fill.to_dict()
        order_log["filled_quantity"] = fill.filled_quantity
        order_log["order_detail_verified"] = True
        order_log["fill_verified"] = fill.is_filled
        order_log["execution_terminal"] = fill.is_terminal
        if fill.has_fill:
            reconciliation = _reconcile_position(
                broker,
                symbol=config.symbol,
                side=side,
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

        if fill.is_filled:
            lifecycle_status, _, reconcile_cycles = (
                _terminal_fill_lifecycle_status(
                    reconciliation,
                    partial=False,
                )
            )
            order_log["position_reconcile_cycles"] = reconcile_cycles
            order_log["manual_resolution_required"] = (
                reconcile_cycles >= POSITION_RECONCILIATION_ALERT_CYCLES
            )
        elif fill.is_terminal_unfilled:
            lifecycle_status = "ORDER_NOT_FILLED"
            order_log["non_fill_verified"] = True
            order_log["not_filled_reason"] = (
                f"Webull accepted the order but it reached status "
                f"{fill.status!r} with 0 filled — the held quantity did not move"
            )
        elif fill.is_terminal and fill.has_fill:
            lifecycle_status, _, reconcile_cycles = (
                _terminal_fill_lifecycle_status(
                    reconciliation,
                    partial=True,
                )
            )
            order_log["position_reconcile_cycles"] = reconcile_cycles
            order_log["manual_resolution_required"] = (
                reconcile_cycles >= POSITION_RECONCILIATION_ALERT_CYCLES
            )
            order_log["partial_fill_terminal_status"] = fill.status
        elif fill.is_partial_fill:
            lifecycle_status = "ORDER_PARTIAL_FILLED"
            order_log["non_fill_verified"] = False
        elif fill.is_terminal:
            lifecycle_status = "ORDER_STATUS_INCONSISTENT"
        else:
            # Accepted and resting (working / pending), not yet executed.
            lifecycle_status = "ORDER_SUBMITTED"
            order_log["non_fill_verified"] = False

    order_log["status"] = lifecycle_status

    # The documented UAT credentials are shared test data. UAT can prove the
    # signed API contract, but its balance/position changes are not evidence of
    # production routing and may be affected by other testers.
    if not broker_config.is_production:
        order_log["sandbox_note"] = (
            "UAT shared test account: simulated balances/positions may change "
            "concurrently and do not prove production trading"
        )

    _write_lifecycle(config, client_order_id, order_log)

    # A definitive non-fill surfaces in the HTTP status too; a fill or a
    # still-working order is reported as OK.
    body_status = (
        "ORDER_NOT_FILLED"
        if lifecycle_status == "ORDER_NOT_FILLED"
        else "OK"
    )
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
        order_log_status=lifecycle_status,
        **response_observations,
    )


def _write_lifecycle(
    config: AppConfig,
    client_order_id: str,
    payload: dict[str, Any],
) -> str:
    """Durably upsert one correlated order row before returning.

    Unlike ordinary PASS/error telemetry, order lifecycle state cannot be
    fire-and-forget: a later scheduler invocation must be able to resume the
    exact accepted/partial order without ever submitting it again.
    """
    return write_order_lifecycle(
        project_id=config.project_id,
        trade_collection=config.firestore_trade_collection,
        state_collection=config.firestore_state_collection,
        strategy_id=config.strategy_id,
        symbol=config.symbol,
        state_document=config.firestore_state_document,
        client_order_id=client_order_id,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@functions_framework.http
def rebalance_trigger(request):
    """HTTP Cloud Function — Shannon Demon DNA rebalance trigger.

    Early-exit chain (cheapest first):
    1. pending lifecycle — one Firestore read; Webull only when an order exists
    2. start_timestamp — pure math
    3. market hours — pure math
    4. DNA step — one Firestore transaction (race-free reservation)
    5. DNA signal / broker trade
    """
    if _is_health_request(request):
        return _health_response()

    config: AppConfig | None = None
    reserved: StepReservation | None = None

    try:
        # -- Gate 1: timestamp (zero I/O) ----------------------------------
        config = load_app_config()
        # Durable lifecycle gate: an accepted/partial order is refreshed on
        # every scheduler invocation, even before a future start timestamp or
        # outside market hours. Existing risk must be observed before gates
        # that apply only to *new* strategy actions.
        pending_response = _reconcile_pending_before_signal(config)
        if pending_response is not None:
            return pending_response

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
            slot_seconds=config.schedule_slot_seconds,
        )
        dna_step = reservation.dna_step

        # A console Force run or duplicate scheduler fire inside an
        # already-reserved slot must not trade: consuming a second step would
        # shift every remaining DNA signal off its trained time slot.
        if reservation.duplicate:
            return _ok("PASS_DUPLICATE_TICK", dna_step=dna_step)

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
    except LifecycleConflictError:
        logger.warning(
            "A different order lifecycle won the concurrent intent reservation"
        )
        return _ok("PASS_PENDING_ORDER_RACE")

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
