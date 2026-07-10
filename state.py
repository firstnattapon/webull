"""Simplified Firestore state management.

Public API:
    reserve_step()      — transactional read-and-increment of the DNA step
    read_step()         — plain read (kept for local scripts / back-compat)
    increment_step()    — plain increment (kept for back-compat)
    write_trade_log()   — fire-and-forget background log write
    flush_trade_logs()  — bounded wait for pending log writes

Includes connection caching for Cloud Function warm starts, a background
executor so trade logging never blocks the main flow, and Firestore batch
commits so the trade log and the state-document status mirror land in a
single RPC.
"""

from __future__ import annotations

import atexit
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepReservation:
    """Result of reserving a DNA step — contains step index and signal."""
    dna_step: int
    dna_signal: int

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
) -> StepReservation:
    """Atomically read-and-increment the DNA step inside a transaction.

    Two concurrent invocations (duplicate scheduler fire, overlapping
    instances) can no longer both observe the same step: the transaction
    guarantees each caller reserves a distinct index.

    When the stored step is already past the end of the DNA timeline
    (``dna_step >= dna_length``) nothing is written and the returned
    reservation carries signal 0 — the caller detects timeline end by
    comparing ``dna_step`` against the DNA length.
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

            if dna_step >= dna_length:
                return StepReservation(dna_step=dna_step, dna_signal=0)

            dna_signal = int(signal_of(dna_step))
            txn.set(
                doc_ref,
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "dna_step": dna_step + 1,
                    "last_reserved_step": dna_step,
                    "last_signal": dna_signal,
                    "last_reserved_at": firestore_module.SERVER_TIMESTAMP,
                },
                merge=True,
            )
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
