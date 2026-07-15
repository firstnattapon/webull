from scripts.repair_position_reconciliation import build_repair_plan


def legacy_row(document_id, created_at, **overrides):
    row = {
        "document_id": document_id,
        "created_at": created_at,
        "status": "ORDER_FILLED_POSITION_UNAVAILABLE",
        "client_order_id": document_id,
        "strategy_id": "strategy",
        "symbol": "SMR",
        "state_document": "strategy_SMR",
        "side": "BUY",
        "order_quantity": 1.0,
        "filled_quantity": 1.0,
        "position_before": 5.0,
        "last_price": 100.0,
        "broker_environment": "prod",
        "broker_endpoint": "api.webull.co.th",
        "account_fingerprint": "fingerprint",
        "position_reconciled": False,
    }
    row.update(overrides)
    return row


def test_only_latest_filled_legacy_row_is_requeued():
    plan = build_repair_plan([
        legacy_row("old", "2026-07-14T18:00:00Z"),
        legacy_row("latest", "2026-07-14T18:30:00Z"),
    ])

    assert [item["action"] for item in plan] == [
        "MARK_LEGACY_UNVERIFIED",
        "REQUEUE",
    ]
    assert "subsequent fill" in plan[0]["reason"]


def test_legacy_row_is_not_requeued_when_a_later_verified_fill_exists():
    plan = build_repair_plan([
        legacy_row("legacy", "2026-07-14T18:00:00Z"),
        legacy_row(
            "verified",
            "2026-07-14T18:30:00Z",
            status="ORDER_FILLED",
            position_reconciled=True,
        ),
    ])

    assert plan == [{
        "document_id": "legacy",
        "client_order_id": "legacy",
        "status": "ORDER_FILLED_POSITION_UNAVAILABLE",
        "action": "MARK_LEGACY_UNVERIFIED",
        "reason": "a subsequent fill prevents exact historical position proof",
    }]


def test_missing_context_never_requeues_latest_row():
    plan = build_repair_plan([
        legacy_row("latest", "2026-07-14T18:30:00Z", account_fingerprint=""),
    ])

    assert plan[0]["action"] == "MARK_LEGACY_UNVERIFIED"
    assert "account_fingerprint" in plan[0]["reason"]


def test_nonfilled_and_already_reconciled_rows_are_ignored():
    plan = build_repair_plan([
        legacy_row("zero", 1, filled_quantity=0.0),
        legacy_row("done", 2, position_reconciled=True),
    ])

    assert plan == []
