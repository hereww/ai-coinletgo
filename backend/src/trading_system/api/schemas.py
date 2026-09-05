from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=256)


class PasswordActionRequest(BaseModel):
    """Request body for sensitive actions guarded by the operator password.

    Rejecting unknown fields is intentional: old clients must not be able to
    send deprecated confirmation fields that the server silently ignores.
    """

    model_config = ConfigDict(extra="forbid")

    password: str = Field(default="", max_length=256)


class IntegrationProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["testnet", "model"]


class PnlSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_date_range(self) -> PnlSyncRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot precede start_date")
        if (self.end_date - self.start_date).days > 365:
            raise ValueError("pnl range cannot exceed 366 days")
        return self


class ModelRelayUpdateRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    model_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:/-]+$")
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    timeout_seconds: float = Field(default=45.0, gt=1, le=120)
    strategy_profile: Literal[
        "conservative", "balanced", "trend_following", "scalping"
    ] = "trend_following"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("model relay base_url must use http:// or https://")
        return normalized


class ModelProfileSelectRequest(BaseModel):
    profile_id: Literal["relay", "vllm"]


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capital_limit_usdt: Decimal | None = Field(default=None, gt=0)
    single_trade_risk_pct: Decimal | None = Field(default=None, gt=0)
    portfolio_risk_pct: Decimal | None = Field(default=None, gt=0)
    daily_loss_pct: Decimal | None = Field(default=None, gt=0)
    max_drawdown_pct: Decimal | None = Field(default=None, gt=0)
    max_leverage: int | None = Field(default=None, ge=1, le=30)
    max_margin_pct: Decimal | None = Field(default=None, gt=0)
    max_positions: int | None = Field(default=None, ge=1)
    max_same_direction: int | None = Field(default=None, ge=1)
    correlation_limit: Decimal | None = Field(default=None, ge=0, le=1)
    entry_direction: Literal["both", "long_only", "short_only"] | None = None
    entry_trigger: Literal["breakout_or_pullback", "breakout_only", "pullback_only"] | None = None
    candidate_count: int | None = Field(default=None, ge=1)
    scan_interval_minutes: Literal[5, 15, 30, 60] | None = None
    min_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    min_net_reward_risk: Decimal | None = Field(default=None, gt=0)
    min_stop_atr: Decimal | None = Field(default=None, gt=0)
    max_stop_atr: Decimal | None = Field(default=None, gt=0)
    manual_exit_levels_enabled: bool | None = None
    manual_stop_atr: Decimal | None = Field(default=None, gt=0, le=10)
    manual_take_profit_atr: Decimal | None = Field(default=None, gt=0, le=20)
    model_primary_portfolio_enabled: bool | None = None
    strong_trend_entry_override_enabled: bool | None = None
    strong_trend_adx_min: Decimal | None = Field(default=None, ge=0)
    trend_adx_min: Decimal | None = Field(default=None, ge=0)
    volatility_soft_limit_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    volatility_hard_limit_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    elevated_volatility_risk_multiplier: Decimal | None = Field(default=None, gt=0, le=1)
    high_volatility_risk_multiplier: Decimal | None = Field(default=None, gt=0, le=1)
    entry_symbols: list[str] | None = Field(default=None, max_length=30)
    portfolio_strategy_enabled: bool | None = None
    portfolio_rebalance_deadband_fraction: Decimal | None = Field(
        default=None, ge=0, le=1
    )
    portfolio_rebalance_cooldown_minutes: int | None = Field(default=None, ge=0, le=1_440)
    hft_enabled: bool | None = None
    hft_dry_run: bool | None = None
    hft_symbols: list[str] | None = Field(default=None, max_length=10)
    hft_event_interval_ms: int | None = Field(default=None, ge=50, le=5_000)
    hft_max_spread_pct: Decimal | None = Field(default=None, gt=0, le=0.02)
    hft_min_depth_usdt: Decimal | None = Field(default=None, gt=0)
    hft_order_notional_usdt: Decimal | None = Field(default=None, gt=0)
    hft_max_inventory_usdt: Decimal | None = Field(default=None, gt=0)
    hft_cooldown_seconds: int | None = Field(default=None, ge=0, le=3_600)
    hft_market_stale_seconds: Decimal | None = Field(default=None, gt=0, le=60)
    hft_max_consecutive_losses: int | None = Field(default=None, ge=1, le=100)
    hft_imbalance_threshold: Decimal | None = Field(default=None, gt=0, lt=1)
    password: str = Field(default="", max_length=256)

    @field_validator("entry_symbols", mode="before")
    @classmethod
    def normalize_entry_symbols(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            symbol = str(item).strip().upper()
            valid = symbol.isascii() and symbol.isalnum() and 5 <= len(symbol) <= 20
            if symbol and not valid:
                raise ValueError("entry_symbols must contain valid Binance symbols")
            if symbol and symbol not in normalized:
                normalized.append(symbol)
        return normalized

    @field_validator("hft_symbols", mode="before")
    @classmethod
    def normalize_hft_symbols(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            symbol = str(item).strip().upper()
            valid = symbol.isascii() and symbol.isalnum() and 5 <= len(symbol) <= 20
            if symbol and not valid:
                raise ValueError("hft_symbols must contain valid Binance symbols")
            if symbol and symbol not in normalized:
                normalized.append(symbol)
        return normalized

    @model_validator(mode="after")
    def validate_stop_range(self) -> ConfigUpdateRequest:
        if self.min_stop_atr is not None and self.max_stop_atr is not None:
            if self.min_stop_atr > self.max_stop_atr:
                raise ValueError("min_stop_atr cannot exceed max_stop_atr")
        if (
            self.manual_exit_levels_enabled
            and self.manual_stop_atr is not None
            and self.max_stop_atr is not None
            and self.manual_stop_atr > self.max_stop_atr
        ):
            raise ValueError("manual_stop_atr cannot exceed max_stop_atr")
        if (
            self.volatility_soft_limit_percentile is not None
            and self.volatility_hard_limit_percentile is not None
            and self.volatility_soft_limit_percentile
            > self.volatility_hard_limit_percentile
        ):
            raise ValueError(
                "volatility_soft_limit_percentile cannot exceed volatility_hard_limit_percentile"
            )
        return self


class ReplayBacktestConfigRequest(BaseModel):
    """Optional deterministic replay overrides.

    The API resolves omitted values from the current runtime configuration
    before persisting the replay.  Keeping overrides nested makes the saved
    parameters self-contained and prevents a later config change from
    changing the meaning of an already queued replay.
    """

    model_config = ConfigDict(extra="forbid")

    initial_equity: Decimal | None = Field(default=None, gt=0)
    risk_pct: Decimal | None = Field(default=None, gt=0, le=1)
    stop_atr: Decimal | None = Field(default=None, gt=0)
    trailing_atr: Decimal | None = Field(default=None, gt=0)
    fee_rate: Decimal | None = Field(default=None, ge=0, le=0.1)
    slippage_rate: Decimal | None = Field(default=None, ge=0, le=0.1)
    estimated_funding_rate: Decimal | None = Field(default=None, ge=-0.1, le=0.1)
    daily_loss_pct: Decimal | None = Field(default=None, gt=0, le=1)
    max_drawdown_pct: Decimal | None = Field(default=None, gt=0, le=1)
    portfolio_risk_pct: Decimal | None = Field(default=None, gt=0, le=1)
    max_leverage: int | None = Field(default=None, ge=1, le=30)
    max_margin_pct: Decimal | None = Field(default=None, gt=0, le=1)
    max_positions: int | None = Field(default=None, ge=1, le=100)
    max_same_direction: int | None = Field(default=None, ge=1, le=100)
    correlation_limit: Decimal | None = Field(default=None, ge=0, le=1)
    candidate_count: int | None = Field(default=None, ge=1, le=30)
    max_spread_pct: Decimal | None = Field(default=None, ge=0, le=1)
    max_abs_funding_rate: Decimal | None = Field(default=None, ge=0, le=1)
    max_abs_basis_pct: Decimal | None = Field(default=None, ge=0, le=1)
    min_book_depth_usdt: Decimal | None = Field(default=None, ge=0)
    min_listing_days: int | None = Field(default=None, ge=0, le=10_000)
    max_volatility_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    entry_direction: Literal["both", "long_only", "short_only"] | None = None
    entry_trigger: Literal["breakout_or_pullback", "breakout_only", "pullback_only"] | None = None
    min_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    min_net_reward_risk: Decimal | None = Field(default=None, gt=0)
    min_stop_atr: Decimal | None = Field(default=None, gt=0)
    max_stop_atr: Decimal | None = Field(default=None, gt=0)
    manual_exit_levels_enabled: bool | None = None
    manual_stop_atr: Decimal | None = Field(default=None, gt=0, le=10)
    manual_take_profit_atr: Decimal | None = Field(default=None, gt=0, le=20)
    strong_trend_entry_override_enabled: bool | None = None
    strong_trend_adx_min: Decimal | None = Field(default=None, ge=0)
    trend_adx_min: Decimal | None = Field(default=None, ge=0)
    volatility_soft_limit_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    volatility_hard_limit_percentile: Decimal | None = Field(default=None, ge=0, le=1)
    elevated_volatility_risk_multiplier: Decimal | None = Field(default=None, gt=0, le=1)
    high_volatility_risk_multiplier: Decimal | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def validate_ranges(self) -> ReplayBacktestConfigRequest:
        if (
            self.stop_atr is not None
            and self.min_stop_atr is not None
            and self.stop_atr < self.min_stop_atr
        ):
            raise ValueError("stop_atr cannot be below min_stop_atr")
        if (
            self.stop_atr is not None
            and self.max_stop_atr is not None
            and self.stop_atr > self.max_stop_atr
        ):
            raise ValueError("stop_atr cannot exceed max_stop_atr")
        if self.min_stop_atr is not None and self.max_stop_atr is not None:
            if self.min_stop_atr > self.max_stop_atr:
                raise ValueError("min_stop_atr cannot exceed max_stop_atr")
        if (
            self.volatility_soft_limit_percentile is not None
            and self.volatility_hard_limit_percentile is not None
            and self.volatility_soft_limit_percentile
            > self.volatility_hard_limit_percentile
        ):
            raise ValueError(
                "volatility_soft_limit_percentile cannot exceed volatility_hard_limit_percentile"
            )
        return self


class ReplayRequest(BaseModel):
    """A reproducible replay request.

    ``deterministic`` replays the local, deterministic strategy over historical
    candles. ``recorded_portfolio`` recompiles one persisted Portfolio-v1
    decision against the exact context captured at decision time.  Neither mode
    invokes a remote model while replaying.
    """

    mode: Literal["deterministic", "recorded_portfolio"] = "deterministic"
    symbols: list[str] = Field(default_factory=list, max_length=30)
    start_date: str | None = None
    end_date: str | None = None
    portfolio_decision_id: str | None = Field(default=None, min_length=36, max_length=36)
    backtest_config: ReplayBacktestConfigRequest | None = None
    # Retained solely so older clients receive a clear validation failure instead
    # of silently interpreting a model sample as a backtest result.
    include_ai_sample: bool | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_date_range(self) -> ReplayRequest:
        if self.include_ai_sample:
            raise ValueError(
                "include_ai_sample is no longer supported; replay never calls a remote model"
            )
        if self.mode == "deterministic":
            if not self.symbols:
                raise ValueError("deterministic replay requires at least one symbol")
            if self.start_date is None or self.end_date is None:
                raise ValueError("deterministic replay requires start_date and end_date")
            start = date.fromisoformat(self.start_date)
            end = date.fromisoformat(self.end_date)
            if end < start:
                raise ValueError("end_date cannot precede start_date")
            if (end - start).days > 365:
                raise ValueError("replay range cannot exceed 366 days")
        elif self.portfolio_decision_id is None:
            raise ValueError("recorded_portfolio replay requires portfolio_decision_id")
        return self


class ReducePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str = Field(min_length=1, max_length=120)
    fraction: Decimal = Field(gt=0, le=1)
    operation_id: str = Field(min_length=8, max_length=80)
    password: str = Field(default="", max_length=256)

    @field_validator("fraction")
    @classmethod
    def validate_fraction(cls, value: Decimal) -> Decimal:
        if value not in {Decimal("0.25"), Decimal("0.5"), Decimal("1")}:
            raise ValueError("fraction must be 0.25, 0.5, or 1")
        return value


class ManualEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9-]+$")
    symbol: str = Field(min_length=5, max_length=20)
    side: Literal["LONG", "SHORT"]
    leverage: int = Field(ge=1, le=30)
    stop_distance_pct: Decimal = Field(ge=Decimal("0.10"), le=Decimal("10"))
    tp1_r: Decimal = Field(ge=Decimal("0.5"), le=Decimal("10"))
    tp2_r: Decimal = Field(ge=Decimal("2"), le=Decimal("12"))
    password: str = Field(default="", max_length=256)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol.isascii() or not symbol.isalnum():
            raise ValueError("symbol must be a Binance contract symbol")
        return symbol

    @model_validator(mode="after")
    def validate_targets(self) -> ManualEntryRequest:
        if self.tp2_r < self.tp1_r:
            raise ValueError("tp2_r cannot be below tp1_r")
        return self


class ManualEntryChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000)


class ManualEntryAdviceRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    side: Literal["LONG", "SHORT"]
    leverage: int = Field(ge=1, le=30)
    stop_distance_pct: Decimal = Field(ge=Decimal("0.10"), le=Decimal("10"))
    tp1_r: Decimal = Field(ge=Decimal("0.5"), le=Decimal("10"))
    tp2_r: Decimal = Field(ge=Decimal("2"), le=Decimal("12"))
    messages: list[ManualEntryChatMessage] = Field(min_length=1, max_length=12)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol.isascii() or not symbol.isalnum():
            raise ValueError("symbol must be a Binance contract symbol")
        return symbol

    @model_validator(mode="after")
    def validate_targets(self) -> ManualEntryAdviceRequest:
        if self.tp2_r < self.tp1_r:
            raise ValueError("tp2_r cannot be below tp1_r")
        return self
