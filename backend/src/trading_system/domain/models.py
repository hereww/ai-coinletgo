from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from trading_system.domain.enums import (
    DecisionStatus,
    HealthState,
    OrderStatus,
    PortfolioPlanActionType,
    PortfolioPlanStatus,
    PortfolioTargetSide,
    PositionSide,
    ReviewAction,
    SignalAction,
    SystemMode,
)

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Candle(BaseModel):
    open_time: datetime
    close_time: datetime
    open: PositiveDecimal
    high: PositiveDecimal
    low: PositiveDecimal
    close: PositiveDecimal
    volume: NonNegativeDecimal


class MarketSnapshot(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{5,20}$")
    timestamp: datetime = Field(default_factory=utc_now)
    mark_price: PositiveDecimal
    index_price: PositiveDecimal
    best_bid: PositiveDecimal
    best_ask: PositiveDecimal
    spread_pct: NonNegativeDecimal
    quote_volume_24h: NonNegativeDecimal
    funding_rate: Decimal
    basis_pct: Decimal = Decimal("0")
    book_depth_usdt: NonNegativeDecimal = Decimal("0")
    open_interest: NonNegativeDecimal
    open_interest_change_pct: Decimal = Decimal("0")
    atr_15m: PositiveDecimal
    adx_1h: NonNegativeDecimal
    trend_1h: Literal[-1, 0, 1]
    trend_4h: Literal[-1, 0, 1]
    breakout_15m: Literal[-1, 0, 1]
    pullback_15m: Literal[-1, 0, 1]
    volume_zscore: Decimal
    volatility_percentile: Decimal = Field(ge=0, le=1)
    listing_days: int = Field(ge=0)
    status: str = "TRADING"
    score: Decimal = Decimal("0")
    recent_returns_1h: list[Decimal] = Field(default_factory=list, exclude=True)

    @property
    def mid_price(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")


class UniverseSymbol(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{5,20}$")
    status: str
    listing_days: int = Field(ge=0)
    quote_volume_24h: NonNegativeDecimal
    best_bid: PositiveDecimal
    best_ask: PositiveDecimal
    mark_price: PositiveDecimal
    index_price: PositiveDecimal
    funding_rate: Decimal

    @property
    def spread_pct(self) -> Decimal:
        midpoint = (self.best_bid + self.best_ask) / Decimal("2")
        return (self.best_ask - self.best_bid) / midpoint


class TradeSignal(BaseModel):
    signal_id: UUID = Field(default_factory=uuid4)
    symbol: str = Field(pattern=r"^[A-Z0-9]{5,20}$")
    action: SignalAction
    confidence: Decimal = Field(ge=0, le=1)
    entry_min: Decimal | None = Field(default=None, gt=0)
    entry_max: Decimal | None = Field(default=None, gt=0)
    invalidation_price: Decimal | None = Field(default=None, gt=0)
    target_price: Decimal | None = Field(default=None, gt=0)
    horizon_minutes: int = Field(default=240, ge=15, le=1_440)
    thesis: str = Field(max_length=500)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)
    risk_flags: list[str] = Field(default_factory=list, max_length=8)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_trade_fields(self) -> TradeSignal:
        if self.action == SignalAction.NO_TRADE:
            return self
        required = (
            self.entry_min,
            self.entry_max,
            self.invalidation_price,
            self.target_price,
        )
        if any(value is None for value in required):
            raise ValueError("open signals require entry range, invalidation, and target")
        if self.entry_min > self.entry_max:  # type: ignore[operator]
            raise ValueError("entry_min cannot exceed entry_max")
        midpoint = (self.entry_min + self.entry_max) / Decimal("2")  # type: ignore[operator]
        if self.action == SignalAction.OPEN_LONG:
            if not self.invalidation_price < midpoint < self.target_price:  # type: ignore[operator]
                raise ValueError("long price ordering is invalid")
        elif not self.target_price < midpoint < self.invalidation_price:  # type: ignore[operator]
            raise ValueError("short price ordering is invalid")
        return self


class PositionReview(BaseModel):
    position_id: str
    symbol: str
    side: PositionSide
    action: ReviewAction
    confidence: Decimal = Field(ge=0, le=1)
    close_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    tightened_stop: Decimal | None = Field(default=None, gt=0)
    rationale: str = Field(max_length=500)

    @model_validator(mode="after")
    def validate_review_action(self) -> PositionReview:
        if self.action == ReviewAction.PARTIAL_CLOSE:
            if self.close_fraction not in {Decimal("0.25"), Decimal("0.5")}:
                raise ValueError("partial close fraction must be 0.25 or 0.5")
        elif self.close_fraction is not None:
            raise ValueError("close_fraction is only valid for PARTIAL_CLOSE")
        if self.action == ReviewAction.TIGHTEN_STOP and self.tightened_stop is None:
            raise ValueError("tighten stop review requires tightened_stop")
        return self


class AIAnalysisResponse(BaseModel):
    signals: list[TradeSignal] = Field(default_factory=list, max_length=5)
    position_reviews: list[PositionReview] = Field(default_factory=list, max_length=3)
    market_regime: Literal["TRENDING", "RANGING", "VOLATILE", "UNCERTAIN"]
    summary: str = Field(max_length=500)


class ManualEntryAdvice(BaseModel):
    reply: str = Field(min_length=1, max_length=1200)
    action: Literal["NO_TRADE", "OPEN_LONG", "OPEN_SHORT"]
    confidence: Decimal = Field(ge=0, le=1)
    stop_distance_pct: Decimal | None = Field(default=None, gt=0, le=10)
    tp1_r: Decimal | None = Field(default=None, ge=Decimal("0.5"), le=10)
    tp2_r: Decimal | None = Field(default=None, ge=Decimal("2"), le=12)
    leverage: int | None = Field(default=None, ge=1, le=30)
    risk_notes: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_targets(self) -> ManualEntryAdvice:
        if self.tp1_r is not None and self.tp2_r is not None and self.tp2_r < self.tp1_r:
            raise ValueError("tp2_r cannot be below tp1_r")
        return self


class PositionState(BaseModel):
    position_id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str
    side: PositionSide
    quantity: PositiveDecimal
    initial_quantity: PositiveDecimal | None = None
    entry_price: PositiveDecimal
    mark_price: PositiveDecimal
    stop_price: PositiveDecimal
    # The exchange-side protection monitor can recover both take-profit
    # tranches.  Keeping these values with the position lets the portfolio
    # compiler detect a model target change and refresh protection instead of
    # treating the allocation as a no-op.
    tp1_price: PositiveDecimal | None = None
    tp2_price: PositiveDecimal | None = None
    original_stop_price: PositiveDecimal | None = None
    initial_risk_usdt: PositiveDecimal
    unrealized_pnl: Decimal = Decimal("0")
    margin_used: NonNegativeDecimal = Decimal("0")
    current_r: Decimal = Decimal("0")
    opened_at: datetime = Field(default_factory=utc_now)
    protected: bool = True

    @model_validator(mode="after")
    def fill_initial_risk_metadata(self) -> PositionState:
        if self.initial_quantity is None:
            self.initial_quantity = self.quantity
        if self.original_stop_price is None:
            self.original_stop_price = self.stop_price
        return self


class AccountState(BaseModel):
    equity: PositiveDecimal
    available_balance: NonNegativeDecimal
    realized_pnl_today: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_today: NonNegativeDecimal = Decimal("0")
    funding_today: Decimal = Decimal("0")
    day_start_equity: PositiveDecimal
    high_water_mark: PositiveDecimal
    total_margin_used: NonNegativeDecimal = Decimal("0")

    @property
    def daily_equity_loss_pct(self) -> Decimal:
        change = self.equity - self.day_start_equity
        if change >= 0:
            return Decimal("0")
        return -change / self.day_start_equity

    @property
    def drawdown_pct(self) -> Decimal:
        if self.equity >= self.high_water_mark:
            return Decimal("0")
        return (self.high_water_mark - self.equity) / self.high_water_mark


class ExchangeFilters(BaseModel):
    tick_size: PositiveDecimal
    step_size: PositiveDecimal
    min_quantity: PositiveDecimal
    min_notional: PositiveDecimal
    max_quantity: PositiveDecimal | None = None
    market_step_size: PositiveDecimal | None = None
    market_min_quantity: PositiveDecimal | None = None
    market_max_quantity: PositiveDecimal | None = None


class RiskLimits(BaseModel):
    capital_limit_usdt: PositiveDecimal = Decimal("1000")
    single_trade_risk_pct: PositiveDecimal = Decimal("0.0025")
    portfolio_risk_pct: PositiveDecimal = Decimal("0.0075")
    daily_loss_pct: PositiveDecimal = Decimal("0.01")
    max_drawdown_pct: PositiveDecimal = Decimal("0.05")
    max_leverage: int = Field(default=3, ge=1, le=30)
    max_margin_pct: PositiveDecimal = Decimal("0.20")
    max_positions: int = Field(default=3, ge=1)
    max_same_direction: int = Field(default=2, ge=1)
    correlation_limit: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    min_stop_atr: PositiveDecimal = Decimal("0.80")
    max_stop_atr: PositiveDecimal = Decimal("2.50")
    min_confidence: Decimal = Field(default=Decimal("0.75"), ge=0, le=1)
    min_net_reward_risk: PositiveDecimal = Decimal("2.0")
    entry_direction: Literal["both", "long_only", "short_only"] = "both"
    portfolio_rebalance_deadband_fraction: Decimal = Field(
        default=Decimal("0.10"), ge=0, le=1
    )

    @model_validator(mode="after")
    def validate_stop_range(self) -> RiskLimits:
        if self.min_stop_atr > self.max_stop_atr:
            raise ValueError("min_stop_atr cannot exceed max_stop_atr")
        return self


class RiskContext(BaseModel):
    mode: SystemMode
    account: AccountState
    positions: list[PositionState] = Field(default_factory=list)
    filters: ExchangeFilters
    limits: RiskLimits = Field(default_factory=RiskLimits)
    correlations: dict[str, Decimal] = Field(default_factory=dict)
    estimated_fee_rate: Decimal = Decimal("0.0005")
    estimated_slippage_rate: Decimal = Decimal("0.0005")


class RiskDecision(BaseModel):
    decision_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    status: DecisionStatus
    reasons: list[str]
    capital_base: NonNegativeDecimal = Decimal("0")
    risk_amount_usdt: NonNegativeDecimal = Decimal("0")
    quantity: NonNegativeDecimal = Decimal("0")
    entry_price: NonNegativeDecimal = Decimal("0")
    stop_price: NonNegativeDecimal = Decimal("0")
    target_price: NonNegativeDecimal = Decimal("0")
    leverage: int = 1
    estimated_margin: NonNegativeDecimal = Decimal("0")
    net_reward_risk: Decimal = Decimal("0")
    decided_at: datetime = Field(default_factory=utc_now)


class PortfolioAllocation(BaseModel):
    """A model-proposed target allocation expressed as a share of portfolio risk."""

    allocation_id: UUID = Field(default_factory=uuid4)
    symbol: str = Field(pattern=r"^[A-Z0-9]{5,20}$")
    target_side: PortfolioTargetSide
    allocation_fraction: Decimal = Field(ge=0, le=1)
    priority: int = Field(ge=1, le=100)
    confidence: Decimal = Field(ge=0, le=1)
    entry_min: Decimal | None = Field(
        default=None,
        gt=0,
        description="允许成交的最低绝对价格；与 entry_max 构成入场区间",
    )
    entry_max: Decimal | None = Field(
        default=None,
        gt=0,
        description="允许成交的最高绝对价格；与 entry_min 构成入场区间",
    )
    stop_price: Decimal | None = Field(
        default=None,
        gt=0,
        description="绝对止损价；LONG 必须低于整个入场区间，SHORT 必须高于整个入场区间",
    )
    target_price: Decimal | None = Field(
        default=None,
        gt=0,
        description="绝对目标价；LONG 必须高于整个入场区间，SHORT 必须低于整个入场区间",
    )
    thesis: str = Field(max_length=500)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)
    risk_flags: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_target_shape(self) -> PortfolioAllocation:
        if self.target_side == PortfolioTargetSide.FLAT:
            if self.allocation_fraction != 0:
                raise ValueError("flat allocation must have zero allocation_fraction")
            return self
        if self.allocation_fraction <= 0:
            raise ValueError("non-flat allocation must have positive allocation_fraction")
        # A non-flat target is executable intent, not a watch-list item. Keep
        # the contract strict so a model cannot silently request exposure while
        # omitting the prices required to prove bounded risk.
        missing = [
            name
            for name, value in (
                ("entry_min", self.entry_min),
                ("entry_max", self.entry_max),
                ("stop_price", self.stop_price),
                ("target_price", self.target_price),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                "non-flat allocation requires entry_min, entry_max, stop_price, "
                "and target_price (missing: " + ", ".join(missing) + ")"
            )
        if self.entry_min is not None and self.entry_max is not None:
            if self.entry_min > self.entry_max:
                raise ValueError("entry_min cannot exceed entry_max")
            assert self.stop_price is not None
            assert self.target_price is not None
            if self.target_side == PortfolioTargetSide.LONG:
                if not (self.stop_price < self.entry_min <= self.entry_max < self.target_price):
                    raise ValueError(
                        "LONG geometry requires stop_price < entry_min <= "
                        "entry_max < target_price"
                    )
            elif self.target_side == PortfolioTargetSide.SHORT:
                if not (self.target_price < self.entry_min <= self.entry_max < self.stop_price):
                    raise ValueError(
                        "SHORT geometry requires target_price < entry_min <= "
                        "entry_max < stop_price"
                    )
        return self


class PortfolioDecision(BaseModel):
    """The complete, bounded AI portfolio intent for one strategy cycle."""

    decision_id: UUID = Field(default_factory=uuid4)
    market_regime: Literal["TRENDING", "RANGING", "VOLATILE", "UNCERTAIN"]
    portfolio_risk_budget_fraction: Decimal = Field(ge=0, le=1)
    allocations: list[PortfolioAllocation] = Field(default_factory=list, max_length=32)
    summary: str = Field(max_length=500)
    model_name: str = Field(default="", max_length=120)
    prompt_version: str = Field(default="", max_length=80)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_allocations(self) -> PortfolioDecision:
        if self.expires_at <= self.created_at:
            raise ValueError("portfolio decision expires_at must be after created_at")
        symbols = [item.symbol for item in self.allocations]
        if len(symbols) != len(set(symbols)):
            raise ValueError("portfolio allocations must have unique symbols")
        total = sum(
            (item.allocation_fraction for item in self.allocations), Decimal("0")
        )
        if total > Decimal("1"):
            raise ValueError("portfolio allocation fractions cannot exceed one")
        if self.portfolio_risk_budget_fraction == 0 and any(
            item.allocation_fraction > 0 for item in self.allocations
        ):
            raise ValueError("zero risk budget cannot contain non-flat allocations")
        return self


class PortfolioPlanAction(BaseModel):
    action_id: UUID = Field(default_factory=uuid4)
    allocation_id: UUID | None = None
    symbol: str = Field(pattern=r"^[A-Z0-9]{5,20}$")
    action: PortfolioPlanActionType
    side: PositionSide | None = None
    current_quantity: NonNegativeDecimal = Decimal("0")
    target_quantity: NonNegativeDecimal = Decimal("0")
    quantity_delta: NonNegativeDecimal = Decimal("0")
    target_risk_usdt: NonNegativeDecimal = Decimal("0")
    entry_min: Decimal | None = None
    entry_max: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    priority: int = Field(default=100, ge=1, le=100)
    action_sequence: int = Field(default=1, ge=1)
    reasons: list[str] = Field(default_factory=list)


class PortfolioPlan(BaseModel):
    plan_id: UUID = Field(default_factory=uuid4)
    decision_id: UUID
    status: PortfolioPlanStatus
    capital_base: NonNegativeDecimal = Decimal("0")
    risk_cap_usdt: NonNegativeDecimal = Decimal("0")
    requested_risk_usdt: NonNegativeDecimal = Decimal("0")
    approved_risk_usdt: NonNegativeDecimal = Decimal("0")
    actions: list[PortfolioPlanAction] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    compiled_at: datetime = Field(default_factory=utc_now)


class ExecutionIntent(BaseModel):
    intent_id: UUID = Field(default_factory=uuid4)
    signal_id: UUID
    symbol: str
    side: PositionSide
    quantity: PositiveDecimal
    limit_price: PositiveDecimal
    entry_min: PositiveDecimal
    entry_max: PositiveDecimal
    stop_price: PositiveDecimal
    tp1_price: PositiveDecimal
    tp2_price: PositiveDecimal
    trailing_atr_multiple: Decimal = Decimal("1.5")
    leverage: int = Field(ge=1, le=30)
    expires_at: datetime

    @model_validator(mode="after")
    def validate_entry_guard(self) -> ExecutionIntent:
        if not self.entry_min <= self.limit_price <= self.entry_max:
            raise ValueError("limit price must be inside the approved entry range")
        return self


class OrderState(BaseModel):
    client_order_id: str
    exchange_order_id: str | None = None
    symbol: str
    side: str
    position_side: PositionSide
    order_type: str
    quantity: NonNegativeDecimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    filled_quantity: NonNegativeDecimal = Decimal("0")
    average_price: NonNegativeDecimal = Decimal("0")
    status: OrderStatus = OrderStatus.CREATED
    portfolio_decision_id: UUID | None = None
    portfolio_allocation_id: UUID | None = None
    action_sequence: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HealthComponent(BaseModel):
    name: str
    state: HealthState
    latency_ms: int | None = None
    detail: str | None = None
    checked_at: datetime = Field(default_factory=utc_now)


class HealthReport(BaseModel):
    ready: bool
    components: list[HealthComponent]
    checked_at: datetime = Field(default_factory=utc_now)
