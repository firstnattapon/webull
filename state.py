"""Simplified Firestore state management.

Public API:
    reserve_step()      — transactional read-and-increment of the DNA step
    read_step()         — plain read (kept for local scripts / back-compat)
    increment_step()    — plain increment (kept for back-compat)
    write_order_lifecycle() — synchronous, durable order-lifecycle upsert
    read_pending_order()    — read the strategy's active order, if any
    write_trade_log()   — fire-and-forget background log write
    flush_trade_logs()  — bounded wait for pending log writes

Includes connection caching for Cloud Function warm starts, a background
executor so trade logging never blocks the main flow, and Firestore batch
commits so the trade log and the state-document status mirror land in a
single RPC.
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import logging
import math
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

logger = logging.getLogger("shannon_demon_dna.state")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class StateError(RuntimeError):
    """Base exception for state-related failures."""
    pass


class StepReadError(StateError):
    """Failed to read the current DNA step from Firestore."""
    pass


class StepWriteError(StateError):
    """Failed to increment the DNA step in Firestore."""
    pass


class LogWriteError(StateError):
    """Failed to write a trade log entry (non-fatal)."""
    pass


class LifecycleReadError(StateError):
    """Failed to read a pending order lifecycle from Firestore."""
    pass


class LifecycleWriteError(StateError):
    """Failed to durably write an order lifecycle to Firestore."""
    pass


class LifecycleConflictError(LifecycleWriteError):
    """Another nonterminal client_order_id already owns this strategy."""
    pass


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepReservation:
    """Result of reserving a DNA step — contains step index and signal.

    ``duplicate`` is True when the invocation landed in a schedule slot whose
    step was already reserved (a console Force run, a duplicate scheduler
    fire, a retried request). No step was consumed and ``dna_signal`` is 0;
    the caller must not trade on it.
    """
    dna_step: int
    dna_signal: int
    duplicate: bool = False

    def to_dict(self) -> dict[str, int]:
        return {"dna_step": self.dna_step, "dna_signal": self.dna_signal}


# ---------------------------------------------------------------------------
# Connection cache — singleton Firestore client (thread-safe)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached_db: Any = None
_cached_firestore_module: Any = None
_cached_project_id: str | None = None


def _get_firestore(project_id: str) -> tuple[Any, Any]:
    """Return (db, firestore_module), reusing cached connections."""
    global _cached_db, _cached_firestore_module, _cached_project_id

    if (
        _cached_db is not None
        and _cached_firestore_module is not None
        and _cached_project_id == project_id
    ):
        return _cached_db, _cached_firestore_module

    with _cache_lock:
        if (
            _cached_db is not None
            and _cached_firestore_module is not None
            and _cached_project_id == project_id
        ):
            return _cached_db, _cached_firestore_module

        from google.cloud import firestore

        db = firestore.Client(project=project_id)
        _cached_firestore_module = firestore
        _cached_db = db
        _cached_project_id = project_id
        return db, firestore


# ---------------------------------------------------------------------------
# Public API — step state
# ---------------------------------------------------------------------------

def _parse_step(raw_step: Any) -> int:
    try:
        dna_step = int(raw_step)
    except (TypeError, ValueError) as exc:
        raise StepReadError("Firestore dna_step must be an integer") from exc
    if dna_step < 0:
        raise StepReadError("Firestore dna_step cannot be negative")
    return dna_step


def reserve_step(
    project_id: str,
    collection: str,
    document: str,
    strategy_id: str,
    symbol: str,
    dna_length: int,
    signal_of: Callable[[int], int],
    slot_seconds: int = 0,
) -> StepReservation:
    """Atomically read-and-increment the DNA step inside a transaction.

    Two concurrent invocations (duplicate scheduler fire, overlapping
    instances) can no longer both observe the same step: the transaction
    guarantees each caller reserves a distinct index.

    When the stored step is already past the end of the DNA timeline
    (``dna_step >= dna_length``) nothing is written and the returned
    reservation carries signal 0 — the caller detects timeline end by
    comparing ``dna_step`` against the DNA length.

    ``slot_seconds`` > 0 makes the reservation idempotent per schedule slot
    (wall-clock buckets of that width, matching the Cloud Scheduler cron
    interval). A second invocation inside an already-reserved slot — a
    console Force run, a duplicate fire, a retried request — returns
    ``duplicate=True`` and consumes nothing. Without this guard every extra
    invocation advances the pointer out of its trained slot and permanently
    shifts all remaining signals. 0 (default) preserves the historical
    one-step-per-invocation behavior.

    Intentional: a reservation is never rolled back, even when the trade
    that follows fails. Each DNA index is trained against a specific
    scheduler time slot, so the pointer must advance exactly once per tick
    — replaying a failed step at a later tick would shift every remaining
    signal off its trained slot. A failed execution therefore skips its
    signal by design (logged as status ERROR in the trade log).
    """
    try:
        db, firestore_module = _get_firestore(project_id)
        doc_ref = db.collection(collection).document(document)
        transaction = db.transaction()

        @firestore_module.transactional
        def _reserve(txn: Any) -> StepReservation:
            snapshot = doc_ref.get(transaction=txn)
            data = (snapshot.to_dict() or {}) if snapshot.exists else {}
            dna_step = _parse_step(data.get("dna_step", 0))

            slot_index: int | None = None
            if slot_seconds > 0:
                slot_index = int(time.time() // slot_seconds)
                last_slot = data.get("last_reserved_slot")
                if (
                    isinstance(last_slot, int)
                    and not isinstance(last_slot, bool)
                    and last_slot == slot_index
                ):
                    return StepReservation(
                        dna_step=dna_step,
                        dna_signal=0,
                        duplicate=True,
                    )

            if dna_step >= dna_length:
                return StepReservation(dna_step=dna_step, dna_signal=0)

            dna_signal = int(signal_of(dna_step))
            reservation_payload: dict[str, Any] = {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "dna_step": dna_step + 1,
                "last_reserved_step": dna_step,
                "last_signal": dna_signal,
                "last_reserved_at": firestore_module.SERVER_TIMESTAMP,
            }
            if slot_index is not None:
                reservation_payload["last_reserved_slot"] = slot_index
            txn.set(doc_ref, reservation_payload, merge=True)
            return StepReservation(dna_step=dna_step, dna_signal=dna_signal)

        return _reserve(transaction)

    except StateError:
        raise
    except Exception as exc:
        raise StepWriteError(f"Failed to reserve step: {exc}") from exc


def read_step(project_id: str, collection: str, document: str) -> int:
    """Read current DNA step index from Firestore.

    Returns 0 if the document does not exist yet.
    Raises ``StepReadError`` on invalid data.
    """
    try:
        db, _ = _get_firestore(project_id)
        snapshot = db.collection(collection).document(document).get()

        if not snapshot.exists:
            return 0

        data = snapshot.to_dict() or {}
        return _parse_step(data.get("dna_step", 0))

    except StepReadError:
        raise
    except Exception as exc:
        raise StepReadError(f"Failed to read state: {exc}") from exc


def increment_step(
    project_id: str,
    collection: str,
    document: str,
    strategy_id: str,
    symbol: str,
    dna_step: int,
    dna_signal: int,
) -> None:
    """Blindly increment the DNA step counter (non-transactional).

    Kept for backward compatibility with local scripts; the Cloud Function
    handler uses ``reserve_step`` which is race-free.
    """
    try:
        db, firestore_module = _get_firestore(project_id)
        state_doc = db.collection(collection).document(document)

        state_doc.set(
            {
                "strategy_id": strategy_id,
                "symbol": symbol,
                "dna_step": firestore_module.Increment(1),
                "last_reserved_step": dna_step,
                "last_signal": dna_signal,
                "last_reserved_at": firestore_module.SERVER_TIMESTAMP,
            },
            merge=True,
        )
    except Exception as exc:
        raise StepWriteError(f"Failed to increment step: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API — durable order lifecycle
# ---------------------------------------------------------------------------

# Webull statuses are not the only values persisted here: the handler also
# uses ORDER_* lifecycle labels. Unknown non-empty statuses remain nonterminal
# deliberately, because clearing an active order on an unfamiliar upstream
# value is less safe than reconciling it again on the next invocation.
TERMINAL_ORDER_LIFECYCLE_STATUSES = frozenset({
    "FILLED",
    "CANCELLED",
    "CANCELED",
    "FAILED",
    "REJECTED",
    "DENIED",
    "INVALID",
    "ERROR",
    "EXPIRED",
    "PLACE_FAILED",
    "PREVIEW_FAILED",
    "PREVIEW_REJECTED",
    "ORDER_FILLED",
    "ORDER_FILLED_POSITION_UNAVAILABLE",
    "ORDER_FILLED_POSITION_UNCONFIRMED",
    "ORDER_PARTIAL_FILLED_TERMINAL",
    "ORDER_PARTIAL_POSITION_UNAVAILABLE",
    "ORDER_PARTIAL_POSITION_UNCONFIRMED",
    "ORDER_NOT_FILLED",
    "ORDER_REJECTED",
    "ORDER_CANCELLED",
    "ORDER_CANCELED",
    "ORDER_FAILED",
    "ORDER_EXPIRED",
    "ORDER_PRE_SUBMIT_FAILED",
})

# Only compare statuses that this service emits (plus the Webull labels used by
# compatibility tests). Unknown statuses stay observable but never outrank a
# known later phase. Terminal states are handled separately and are absorbing.
_ORDER_LIFECYCLE_PROGRESS = {
    "CREATED": 10,
    "ORDER_CREATED": 10,
    "ORDER_SUBMIT_UNKNOWN": 20,
    "ORDER_SUBMIT_UNKNOWN_NOT_FOUND": 20,
    "SUBMITTED": 30,
    "ORDER_SUBMITTED": 30,
    "ORDER_STATUS_INCONSISTENT": 35,
    "PARTIAL_FILLED": 40,
    "PARTIALLY_FILLED": 40,
    "ORDER_PARTIAL_FILLED": 40,
    "ORDER_PARTIAL_POSITION_PENDING": 50,
    "ORDER_FILLED_POSITION_PENDING": 60,
}

# Minimal, non-secret context needed to reconcile the same intent on a later
# Cloud Run invocation.  The full lifecycle remains in the deterministic log
# document; the state mirror only carries fields required before that document
# is read by the dashboard.
_PENDING_ORDER_CONTEXT_FIELDS = (
    "order_id",
    "side",
    "order_quantity",
    "filled_quantity",
    "position_before",
    "last_price",
    "broker_environment",
    "broker_endpoint",
    "account_fingerprint",
    "dna_step",
    "dna_signal",
    "not_found_attempts",
    "submission_unknown_at",
    "position_reconcile_cycles",
)

_STATUS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_ -]{0,63}$")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MAX_CLIENT_ORDER_ID_LENGTH = 32
_MAX_LIFECYCLE_DEPTH = 16

# Lifecycle payloads may contain raw Webull *responses*, but never credentials
# or signed request material. Matching normalized key names recursively keeps a
# nested debug envelope from accidentally putting secrets in Firestore.
_SENSITIVE_KEY_PARTS = (
    "app_key",
    "app_secret",
    "account_id",
    "credential",
    "secret",
    "authorization",
    "access_token",
    "refresh_token",
    "token",
    "signature",
    "password",
    "private_key",
    "cookie",
)
_SIGNED_REQUEST_KEYS = frozenset({
    "headers",
    "request_headers",
    "request_metadata",
    "raw_request",
    "signed_request",
})
_COLLAPSED_SIGNED_REQUEST_KEYS = frozenset(
    key.replace("_", "") for key in _SIGNED_REQUEST_KEYS
)
_RESERVED_LIFECYCLE_KEYS = frozenset({
    "strategy_id",
    "symbol",
    "state_document",
    "created_at",
    "updated_at",
    "lifecycle_document_id",
    "pending_order",
})


def _required_lifecycle_text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{name} cannot contain control characters")
    return normalized


def _validate_client_order_id(client_order_id: Any) -> str:
    normalized = _required_lifecycle_text("client_order_id", client_order_id)
    if normalized != client_order_id:
        raise ValueError("client_order_id cannot have surrounding whitespace")
    if len(normalized) > _MAX_CLIENT_ORDER_ID_LENGTH:
        raise ValueError(
            f"client_order_id must contain 1-{_MAX_CLIENT_ORDER_ID_LENGTH} characters"
        )
    if not _CLIENT_ORDER_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "client_order_id may contain only letters, digits, hyphens, and underscores"
        )
    return normalized


def _normalized_payload_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _is_sensitive_payload_key(normalized_key: str) -> bool:
    collapsed_key = normalized_key.replace("_", "")
    return (
        normalized_key in _SIGNED_REQUEST_KEYS
        or collapsed_key in _COLLAPSED_SIGNED_REQUEST_KEYS
        or any(
            part in normalized_key or part.replace("_", "") in collapsed_key
            for part in _SENSITIVE_KEY_PARTS
        )
    )


def _validated_lifecycle_value(value: Any, path: str, depth: int = 0) -> Any:
    """Return a detached Firestore-safe value or reject unsafe metadata."""
    if depth > _MAX_LIFECYCLE_DEPTH:
        raise ValueError(f"{path} exceeds the maximum nesting depth")

    if value is None or isinstance(value, (str, bool, date, datetime)):
        return copy.deepcopy(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        return value
    if isinstance(value, (list, tuple)):
        return [
            _validated_lifecycle_value(item, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"{path} keys must be non-empty strings")
            normalized_key = _normalized_payload_key(key)
            if _is_sensitive_payload_key(normalized_key):
                raise ValueError(
                    f"{path}.{key} contains credentials or signed request metadata"
                )
            cleaned[key] = _validated_lifecycle_value(
                item,
                f"{path}.{key}",
                depth + 1,
            )
        return cleaned
    raise ValueError(f"{path} contains unsupported value type {type(value).__name__}")


def _validate_lifecycle_payload(
    payload: Any,
    client_order_id: str,
) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("payload must be a non-empty dict")

    overlap = _RESERVED_LIFECYCLE_KEYS.intersection(payload)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise ValueError(f"payload cannot override lifecycle fields: {names}")

    payload_client_order_id = payload.get("client_order_id")
    if payload_client_order_id is not None:
        if _validate_client_order_id(payload_client_order_id) != client_order_id:
            raise ValueError("payload client_order_id does not match client_order_id")

    raw_status = payload.get("status")
    if not isinstance(raw_status, str):
        raise ValueError("payload.status must be a string")
    status = raw_status.strip().upper()
    if not _STATUS_PATTERN.fullmatch(status):
        raise ValueError("payload.status is invalid")

    cleaned = _validated_lifecycle_value(payload, "payload")
    if "filled_quantity" in cleaned:
        filled_quantity = cleaned["filled_quantity"]
        if (
            isinstance(filled_quantity, bool)
            or not isinstance(filled_quantity, (int, float))
            or filled_quantity < 0
        ):
            raise ValueError("payload.filled_quantity must be a non-negative number")
    cleaned["client_order_id"] = client_order_id
    cleaned["status"] = status
    return cleaned, status


def _lifecycle_progress(status: str) -> int:
    if status in TERMINAL_ORDER_LIFECYCLE_STATUSES:
        return 1_000
    return _ORDER_LIFECYCLE_PROGRESS.get(status, 0)


def _lifecycle_filled_quantity(payload: dict[str, Any]) -> float:
    value = payload.get("filled_quantity", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else 0.0


def _order_lifecycle_document_id(client_order_id: str) -> str:
    digest = hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()
    return f"order_{digest}"


def write_order_lifecycle(
    project_id: str,
    trade_collection: str,
    state_collection: str,
    strategy_id: str,
    symbol: str,
    state_document: str,
    client_order_id: str,
    payload: dict[str, Any],
) -> str:
    """Synchronously upsert one durable order lifecycle and state mirror.

    The lifecycle document id is a stable SHA-256 derivative of
    ``client_order_id``. Every update therefore merges into the same document.
    The log update and the strategy state's ``pending_order`` update/clear are
    committed in one Firestore transaction.

    Returns the deterministic lifecycle document id. Validation errors raise
    ``ValueError``; Firestore failures raise ``LifecycleWriteError``.
    """
    normalized_project_id = _required_lifecycle_text("project_id", project_id)
    normalized_trade_collection = _required_lifecycle_text(
        "trade_collection", trade_collection
    )
    normalized_state_collection = _required_lifecycle_text(
        "state_collection", state_collection
    )
    normalized_strategy_id = _required_lifecycle_text("strategy_id", strategy_id)
    normalized_symbol = _required_lifecycle_text("symbol", symbol).upper()
    normalized_state_document = _required_lifecycle_text(
        "state_document", state_document
    )
    normalized_client_order_id = _validate_client_order_id(client_order_id)
    lifecycle_payload, status = _validate_lifecycle_payload(
        payload,
        normalized_client_order_id,
    )
    lifecycle_document_id = _order_lifecycle_document_id(
        normalized_client_order_id
    )

    try:
        db, firestore_module = _get_firestore(normalized_project_id)
        log_ref = db.collection(normalized_trade_collection).document(
            lifecycle_document_id
        )
        state_ref = db.collection(normalized_state_collection).document(
            normalized_state_document
        )
        transaction = db.transaction()

        @firestore_module.transactional
        def _upsert(txn: Any) -> None:
            snapshot = log_ref.get(transaction=txn)
            state_snapshot = state_ref.get(transaction=txn)
            existing_lifecycle = snapshot.to_dict() if snapshot.exists else {}
            if not isinstance(existing_lifecycle, dict):
                existing_lifecycle = {}
            existing_state = (
                state_snapshot.to_dict() if state_snapshot.exists else {}
            )
            if not isinstance(existing_state, dict):
                existing_state = {}

            existing_status = str(existing_lifecycle.get("status", "")).strip().upper()
            existing_filled = _lifecycle_filled_quantity(existing_lifecycle)
            incoming_filled = _lifecycle_filled_quantity(lifecycle_payload)

            # Firestore can retry a transaction and Cloud Run can finish two
            # observations out of order. Never let an older callback reopen a
            # terminal order, reduce its cumulative fill, regress its phase, or
            # replace a verified Positions observation with a stale snapshot.
            if existing_status in TERMINAL_ORDER_LIFECYCLE_STATUSES:
                return
            if existing_filled > incoming_filled:
                return
            if _lifecycle_progress(existing_status) > _lifecycle_progress(status):
                return
            if (
                existing_filled == incoming_filled
                and existing_lifecycle.get("position_reconciled") is True
                and lifecycle_payload.get("position_reconciled") is not True
            ):
                return

            existing_pending = existing_state.get("pending_order")
            if existing_pending is not None and not isinstance(existing_pending, dict):
                raise LifecycleConflictError(
                    "Existing Firestore pending_order is not a map"
                )
            existing_pending_id = (
                str(existing_pending.get("client_order_id", "")).strip()
                if existing_pending else ""
            )

            if (
                status not in TERMINAL_ORDER_LIFECYCLE_STATUSES
                and existing_pending_id
                and existing_pending_id != normalized_client_order_id
            ):
                raise LifecycleConflictError(
                    "Another pending order already owns this strategy state"
                )
            log_payload = {
                "strategy_id": normalized_strategy_id,
                "symbol": normalized_symbol,
                "state_document": normalized_state_document,
                "lifecycle_document_id": lifecycle_document_id,
                "updated_at": firestore_module.SERVER_TIMESTAMP,
                **lifecycle_payload,
            }
            if not snapshot.exists:
                log_payload["created_at"] = firestore_module.SERVER_TIMESTAMP

            txn.set(log_ref, log_payload, merge=True)

            state_payload: dict[str, Any] = {
                "last_status": status,
                "last_logged_at": firestore_module.SERVER_TIMESTAMP,
            }
            if status in TERMINAL_ORDER_LIFECYCLE_STATUSES:
                state_payload["pending_order"] = firestore_module.DELETE_FIELD
            else:
                pending_order = {
                    "client_order_id": normalized_client_order_id,
                    "status": status,
                    "strategy_id": normalized_strategy_id,
                    "symbol": normalized_symbol,
                    "trade_collection": normalized_trade_collection,
                    "state_document": normalized_state_document,
                    "lifecycle_document_id": lifecycle_document_id,
                    "updated_at": firestore_module.SERVER_TIMESTAMP,
                }
                for field_name in _PENDING_ORDER_CONTEXT_FIELDS:
                    if field_name in lifecycle_payload:
                        pending_order[field_name] = copy.deepcopy(
                            lifecycle_payload[field_name]
                        )
                    elif field_name in existing_lifecycle:
                        pending_order[field_name] = copy.deepcopy(
                            existing_lifecycle[field_name]
                        )
                state_payload["pending_order"] = pending_order
            # A delayed terminal callback for an older order must never clear
            # or overwrite a newer pending intent.
            if not (
                status in TERMINAL_ORDER_LIFECYCLE_STATUSES
                and existing_pending_id
                and existing_pending_id != normalized_client_order_id
            ):
                txn.set(state_ref, state_payload, merge=True)

        _upsert(transaction)
        return lifecycle_document_id
    except StateError:
        raise
    except Exception as exc:
        raise LifecycleWriteError(f"Failed to write order lifecycle: {exc}") from exc


def read_pending_order(
    project_id: str,
    state_collection: str,
    state_document: str,
) -> dict[str, Any] | None:
    """Return a detached copy of the strategy's pending order, if present."""
    normalized_project_id = _required_lifecycle_text("project_id", project_id)
    normalized_state_collection = _required_lifecycle_text(
        "state_collection", state_collection
    )
    normalized_state_document = _required_lifecycle_text(
        "state_document", state_document
    )
    try:
        db, _ = _get_firestore(normalized_project_id)
        snapshot = db.collection(normalized_state_collection).document(
            normalized_state_document
        ).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        pending_order = data.get("pending_order")
        if pending_order is None:
            return None
        if not isinstance(pending_order, dict):
            raise LifecycleReadError("Firestore pending_order must be a map")
        return copy.deepcopy(pending_order)
    except LifecycleReadError:
        raise
    except Exception as exc:
        raise LifecycleReadError(f"Failed to read pending order: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API — fire-and-forget trade logging
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_log_executor: ThreadPoolExecutor | None = None
_log_futures: list[Future] = []


def _get_log_executor() -> ThreadPoolExecutor:
    global _log_executor
    if _log_executor is None:
        with _log_lock:
            if _log_executor is None:
                _log_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="trade-log",
                )
                atexit.register(flush_trade_logs)
    return _log_executor


def write_trade_log(
    project_id: str,
    trade_collection: str,
    strategy_id: str,
    symbol: str,
    state_document: str,
    payload: dict[str, Any],
    state_collection: str | None = None,
) -> None:
    """Write a trade log entry — fire-and-forget (logs error, never raises).

    The write runs on a background thread so it never blocks the main
    rebalance flow. Callers that need durability before the instance is
    frozen (Cloud Run throttles CPU after the response) should call
    ``flush_trade_logs`` before returning.

    When ``state_collection`` is provided, the log document and a status
    mirror on the state document are committed in a single Firestore batch.
    """
    try:
        executor = _get_log_executor()
        future = executor.submit(
            _write_trade_log_sync,
            project_id,
            trade_collection,
            strategy_id,
            symbol,
            state_document,
            dict(payload),
            state_collection,
        )
        with _log_lock:
            _log_futures[:] = [f for f in _log_futures if not f.done()]
            _log_futures.append(future)
    except Exception as exc:
        logger.error("Trade log dispatch failed (non-fatal): %s", exc)


def flush_trade_logs(timeout: float = 5.0) -> None:
    """Wait (bounded) for pending trade log writes — never raises."""
    try:
        with _log_lock:
            pending = [f for f in _log_futures if not f.done()]
        if not pending:
            return

        _, not_done = wait(pending, timeout=timeout)
        if not_done:
            logger.warning(
                "%d trade log write(s) still pending after %.1fs flush",
                len(not_done),
                timeout,
            )
        with _log_lock:
            _log_futures[:] = [f for f in _log_futures if not f.done()]
    except Exception as exc:
        logger.error("Trade log flush failed (non-fatal): %s", exc)


def _write_trade_log_sync(
    project_id: str,
    trade_collection: str,
    strategy_id: str,
    symbol: str,
    state_document: str,
    payload: dict[str, Any],
    state_collection: str | None,
) -> None:
    """Background worker for ``write_trade_log`` — swallows all errors."""
    try:
        db, firestore_module = _get_firestore(project_id)

        log_payload = {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "state_document": state_document,
            "created_at": firestore_module.SERVER_TIMESTAMP,
            **payload,
        }
        log_ref = db.collection(trade_collection).document()

        if state_collection:
            batch = db.batch()
            batch.set(log_ref, log_payload)
            batch.set(
                db.collection(state_collection).document(state_document),
                {
                    "last_status": str(payload.get("status", "UNKNOWN")),
                    "last_logged_at": firestore_module.SERVER_TIMESTAMP,
                },
                merge=True,
            )
            batch.commit()
        else:
            log_ref.set(log_payload)

    except Exception as exc:
        logger.error("Trade log write failed (non-fatal): %s", exc)
