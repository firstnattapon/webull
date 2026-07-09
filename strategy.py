from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass


DEFAULT_ORDER_DECIMAL_PRECISION = 5


@dataclass(frozen=True)
class RebalanceDecision:
    action: str
    side: str | None
    order_quantity: float
    value_now_usd: float
    rebalance_amount: float
    baseline_pnl: float
    reason: str

    def to_dict(self) -> dict[str, float | str | None]:
        payload = asdict(self)
        payload["order_qty"] = self.order_quantity
        payload["rebalance"] = self.rebalance_amount
        payload["baseline"] = self.baseline_pnl
        return payload


def calculate_shannon_decision(
    quantity: float,
    last_price: float,
    fix_c: float,
    p0: float,
    diff: float,
    decimal_precision: int = DEFAULT_ORDER_DECIMAL_PRECISION,
) -> RebalanceDecision:
    """Calculate Shannon's Demon fixed-capital rebalance decision."""
    if quantity < 0:
        raise ValueError("Negative positions are not supported by this strategy")
    if last_price <= 0:
        raise ValueError("last_price must be greater than 0")
    if fix_c <= 0:
        raise ValueError("fix_c must be greater than 0")
    if p0 <= 0:
        raise ValueError("p0 must be greater than 0")
    if diff < 0:
        raise ValueError("diff must be greater than or equal to 0")
    if decimal_precision < 0:
        raise ValueError("decimal_precision must be greater than or equal to 0")

    value_now_usd = quantity * last_price
    rebalance_amount = abs(fix_c - value_now_usd)
    baseline_pnl = fix_c * math.log(last_price / p0)

    if rebalance_amount <= diff:
        return RebalanceDecision(
            action="PASS",
            side=None,
            order_quantity=0.0,
            value_now_usd=value_now_usd,
            rebalance_amount=rebalance_amount,
            baseline_pnl=baseline_pnl,
            reason="WITHIN_THRESHOLD",
        )

    order_quantity = round(rebalance_amount / last_price, decimal_precision)

    if value_now_usd < (fix_c - diff):
        if order_quantity <= 0:
            return RebalanceDecision(
                action="PASS",
                side=None,
                order_quantity=0.0,
                value_now_usd=value_now_usd,
                rebalance_amount=rebalance_amount,
                baseline_pnl=baseline_pnl,
                reason="BUY_QTY_ZERO_AFTER_ROUND",
            )
        return RebalanceDecision(
            action="BUY",
            side="BUY",
            order_quantity=float(order_quantity),
            value_now_usd=value_now_usd,
            rebalance_amount=rebalance_amount,
            baseline_pnl=baseline_pnl,
            reason="BELOW_TARGET",
        )

    if value_now_usd > (fix_c + diff):
        if order_quantity <= 0:
            return RebalanceDecision(
                action="PASS",
                side=None,
                order_quantity=0.0,
                value_now_usd=value_now_usd,
                rebalance_amount=rebalance_amount,
                baseline_pnl=baseline_pnl,
                reason="SELL_QTY_ZERO_AFTER_ROUND",
            )
        return RebalanceDecision(
            action="SELL",
            side="SELL",
            order_quantity=float(order_quantity),
            value_now_usd=value_now_usd,
            rebalance_amount=rebalance_amount,
            baseline_pnl=baseline_pnl,
            reason="ABOVE_TARGET",
        )

    return RebalanceDecision(
        action="PASS",
        side=None,
        order_quantity=0.0,
        value_now_usd=value_now_usd,
        rebalance_amount=rebalance_amount,
        baseline_pnl=baseline_pnl,
        reason="NO_RULE_MATCH",
    )


class ShannonDemon:
    def __init__(
        self,
        fix_c: float,
        p0: float,
        diff: float,
        decimal_precision: int = DEFAULT_ORDER_DECIMAL_PRECISION,
    ):
        self.fix_c = fix_c
        self.p0 = p0
        self.diff = diff
        self.decimal_precision = decimal_precision

    def calculate_action(self, quantity: float, last_price: float) -> dict:
        return calculate_shannon_decision(
            quantity=quantity,
            last_price=last_price,
            fix_c=self.fix_c,
            p0=self.p0,
            diff=self.diff,
            decimal_precision=self.decimal_precision,
        ).to_dict()


def generate_client_order_id(strategy_id: str, symbol: str, *parts: object) -> str:
    raw = ":".join([strategy_id, symbol.upper(), *[str(part) for part in parts]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
