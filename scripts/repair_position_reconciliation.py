#!/usr/bin/env python3
"""Plan or apply a safe repair for legacy unverified position lifecycles.

Dry-run is the default.  The script never calls Webull and never infers a
holding from order history.  It can requeue only the latest filled lifecycle
when no later fill exists; older ambiguous rows are marked LEGACY_UNVERIFIED.
The bot's next invocation performs the real Webull Positions read.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import state  # noqa: E402


REQUEUE_FIELDS = (
    "order_id",
    "side",
    "order_quantity",
    "filled_quantity",
    "position_before",
    "expected_position_after",
    "last_price",
    "broker_environment",
    "broker_endpoint",
    "account_fingerprint",
    "dna_step",
    "dna_signal",
    "position_reconcile_cycles",
)
REQUIRED_REQUEUE_FIELDS = (
    "client_order_id",
    "side",
    "order_quantity",
    "filled_quantity",
    "position_before",
    "last_price",
    "broker_environment",
    "broker_endpoint",
    "account_fingerprint",
    "strategy_id",
    "symbol",
    "state_document",
)


def _finite_positive(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _sort_key(row: dict[str, Any]) -> tuple[float, str]:
    created_at = row.get("created_at")
    if isinstance(created_at, datetime):
        timestamp = created_at.timestamp()
    elif isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
        timestamp = float(created_at)
    elif isinstance(created_at, str):
        try:
            timestamp = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            timestamp = 0.0
    else:
        timestamp = 0.0
    return timestamp, str(row.get("document_id", ""))


def build_repair_plan(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic repair actions without mutating input or Firestore."""
    ordered = sorted((dict(row) for row in rows), key=_sort_key)
    filled_rows = [
        row for row in ordered if _finite_positive(row.get("filled_quantity"))
    ]
    latest_filled_document = (
        str(filled_rows[-1].get("document_id", "")) if filled_rows else ""
    )

    plan: list[dict[str, Any]] = []
    for row in ordered:
        status = str(row.get("status", "")).strip().upper()
        if status not in state.LEGACY_UNVERIFIED_POSITION_STATUSES:
            continue
        if row.get("position_reconciled") is True:
            continue
        if _finite_positive(row.get("filled_quantity")) is None:
            continue

        document_id = str(row.get("document_id", "")).strip()
        missing = [name for name in REQUIRED_REQUEUE_FIELDS if row.get(name) in (None, "")]
        latest = bool(document_id and document_id == latest_filled_document)
        if latest and not missing:
            action = "REQUEUE"
            reason = "latest filled lifecycle; exact Positions proof still required"
        else:
            action = "MARK_LEGACY_UNVERIFIED"
            if not latest:
                reason = "a subsequent fill prevents exact historical position proof"
            else:
                reason = "missing safe requeue context: " + ", ".join(missing)

        plan.append({
            "document_id": document_id,
            "client_order_id": str(row.get("client_order_id", "")),
            "status": status,
            "action": action,
            "reason": reason,
        })
    return plan


def _pending_status(legacy_status: str) -> str:
    if "PARTIAL" in legacy_status:
        return "ORDER_PARTIAL_POSITION_PENDING"
    return "ORDER_FILLED_POSITION_PENDING"


def _load_rows(db: Any, collection: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    query = db.collection(collection).order_by("created_at", direction="DESCENDING")
    snapshots = list(query.limit(limit).stream())
    rows = []
    for snapshot in snapshots:
        value = snapshot.to_dict() or {}
        if isinstance(value, dict):
            rows.append({"document_id": snapshot.id, **value})
    return rows, len(snapshots) == limit


def _apply_plan(
    *,
    db: Any,
    rows: list[dict[str, Any]],
    plan: list[dict[str, Any]],
    project_id: str,
    trade_collection: str,
    state_collection: str,
    state_document: str,
) -> None:
    by_id = {str(row.get("document_id", "")): row for row in rows}
    requeues = [item for item in plan if item["action"] == "REQUEUE"]
    if len(requeues) > 1:
        raise RuntimeError("repair plan contains more than one requeue")
    if requeues:
        existing_pending = state.read_pending_order(
            project_id,
            state_collection,
            state_document,
        )
        if existing_pending is not None:
            raise RuntimeError("strategy already has a pending order; refusing repair")

    for item in plan:
        row = by_id[item["document_id"]]
        if str(row.get("state_document", "")) != state_document:
            continue
        if item["action"] == "MARK_LEGACY_UNVERIFIED":
            db.collection(trade_collection).document(item["document_id"]).set(
                {
                    "position_sync_status": "LEGACY_UNVERIFIED",
                    "manual_resolution_required": True,
                    "legacy_repair_reason": item["reason"],
                },
                merge=True,
            )
            continue

        payload = {
            name: row[name]
            for name in REQUEUE_FIELDS
            if name in row and row[name] is not None
        }
        payload.update({
            "status": _pending_status(item["status"]),
            "execution_terminal": True,
            "position_reconciled": False,
            "position_sync_status": "PENDING",
            "manual_resolution_required": True,
            "legacy_repair_reason": item["reason"],
        })
        state.write_order_lifecycle(
            project_id=project_id,
            trade_collection=trade_collection,
            state_collection=state_collection,
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            state_document=state_document,
            client_order_id=str(row["client_order_id"]),
            payload=payload,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--trade-collection", default="shannon_demon_trades")
    parser.add_argument("--state-collection", default="shannon_demon_state")
    parser.add_argument("--state-document", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the printed plan. Default is a read-only dry-run.",
    )
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 10_000:
        parser.error("--limit must be between 1 and 10000")

    from google.cloud import firestore

    db = firestore.Client(project=args.project_id)
    rows, truncated = _load_rows(db, args.trade_collection, args.limit)
    rows = [
        row for row in rows
        if str(row.get("state_document", "")) == args.state_document
    ]
    plan = build_repair_plan(rows)
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "truncated": truncated,
        "actions": plan,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.apply:
        if truncated:
            raise RuntimeError(
                "query reached --limit; refusing apply because later-fill proof is incomplete"
            )
        _apply_plan(
            db=db,
            rows=rows,
            plan=plan,
            project_id=args.project_id,
            trade_collection=args.trade_collection,
            state_collection=args.state_collection,
            state_document=args.state_document,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
