from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.db.models import Order


@dataclass(frozen=True)
class ReturnDecision:
    eligible: bool
    reason: str
    needs_human_approval: bool


def evaluate_return(
    order: Order,
    now: datetime | None = None,
    *,
    window_days: int = 30,
    require_human_approval: bool = True,
) -> ReturnDecision:
    current_time = now or datetime.now(UTC)
    if order.status != "delivered":
        return ReturnDecision(
            eligible=False,
            reason="订单尚未签收，不能发起退货；如需拦截或取消，请转人工处理。",
            needs_human_approval=order.status in {"processing", "shipped"},
        )
    if order.delivered_at is None:
        return ReturnDecision(False, "订单缺少签收时间，需要人工核验。", True)

    delivered_at = order.delivered_at
    if delivered_at.tzinfo is None:
        delivered_at = delivered_at.replace(tzinfo=UTC)
    days_since_delivery = (current_time - delivered_at).days
    if days_since_delivery > window_days:
        return ReturnDecision(False, f"该订单已超过 {window_days} 天退货期限。", False)
    return ReturnDecision(
        eligible=True,
        reason=f"订单在 {window_days} 天退货期内（签收后 {days_since_delivery} 天）。",
        needs_human_approval=require_human_approval,
    )
