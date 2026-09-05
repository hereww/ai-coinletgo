from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

HftAction = Literal["BUY", "SELL", "HOLD"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class HftBookMetrics(BaseModel):
    symbol: str
    best_bid: Decimal = Field(gt=0)
    best_ask: Decimal = Field(gt=0)
    mid_price: Decimal = Field(gt=0)
    microprice: Decimal = Field(gt=0)
    spread_pct: Decimal = Field(ge=0)
    bid_depth_usdt: Decimal = Field(ge=0)
    ask_depth_usdt: Decimal = Field(ge=0)
    imbalance: Decimal
    last_update_id: int = Field(ge=0)
    event_time_ms: int = Field(ge=0)
    received_at: datetime = Field(default_factory=utc_now)
    received_monotonic: float = Field(ge=0)

    @property
    def min_depth_usdt(self) -> Decimal:
        return min(self.bid_depth_usdt, self.ask_depth_usdt)


class HftSignal(BaseModel):
    symbol: str
    action: HftAction
    score: Decimal = Field(ge=0, le=1)
    reason: str
    book: HftBookMetrics
    created_at: datetime = Field(default_factory=utc_now)


class HftRiskDecision(BaseModel):
    allowed: bool
    quantity: Decimal = Field(default=Decimal("0"), ge=0)
    reason: str


class HftFill(BaseModel):
    fill_id: str = Field(default_factory=lambda: uuid4().hex)
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    notional_usdt: Decimal = Field(gt=0)
    fee_usdt: Decimal = Field(ge=0)
    closed_quantity: Decimal = Field(ge=0)
    gross_realized_pnl_usdt: Decimal
    realized_pnl_usdt: Decimal
    inventory_before: Decimal
    inventory_after: Decimal
    average_entry_after: Decimal = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class HftExecutionSnapshot(BaseModel):
    inventory_qty: Decimal
    inventory_notional_usdt: Decimal = Field(ge=0)
    average_entry_price: Decimal = Field(ge=0)
    realized_pnl_usdt: Decimal
    unrealized_pnl_usdt: Decimal
    fees_usdt: Decimal = Field(ge=0)
    total_pnl_usdt: Decimal
    fills: int = Field(ge=0)


class HftStatus(BaseModel):
    state: Literal["IDLE", "CONNECTING", "READY", "RUNNING", "BLOCKED", "DEGRADED"]
    detail: str
    dry_run: bool
    symbols: list[str]
    last_update_id: dict[str, int] = Field(default_factory=dict)
    fills: int = 0
    realized_pnl_usdt: Decimal = Decimal("0")
    unrealized_pnl_usdt: Decimal = Decimal("0")
    fees_usdt: Decimal = Decimal("0")
    consecutive_losses: int = 0
    updated_at: datetime = Field(default_factory=utc_now)
