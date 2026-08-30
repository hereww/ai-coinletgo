from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(max_length=256)


class PasswordActionRequest(BaseModel):
    password: str = Field(default="", max_length=256)
    target: str = Field(default="", max_length=20)


class ModelRelayUpdateRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=500)
    model_name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:/-]+$")
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    timeout_seconds: float = Field(default=45.0, gt=1, le=120)
    daily_request_limit: int = Field(default=110, ge=1, le=110)
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
    scan_interval_minutes: int | None = Field(default=None, ge=15, le=120)
    min_confidence: Decimal | None = Field(default=None, ge=0, le=1)
    min_net_reward_risk: Decimal | None = Field(default=None, gt=0)
    min_stop_atr: Decimal | None = Field(default=None, gt=0)
    max_stop_atr: Decimal | None = Field(default=None, gt=0)
    entry_symbols: list[str] | None = Field(default=None, max_length=30)
    portfolio_strategy_enabled: bool | None = None
    portfolio_rebalance_deadband_fraction: Decimal | None = Field(
        default=None, ge=0, le=1
    )
    portfolio_rebalance_cooldown_minutes: int | None = Field(default=None, ge=0, le=1_440)
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

    @model_validator(mode="after")
    def validate_stop_range(self) -> ConfigUpdateRequest:
        if self.min_stop_atr is not None and self.max_stop_atr is not None:
            if self.min_stop_atr > self.max_stop_atr:
                raise ValueError("min_stop_atr cannot exceed max_stop_atr")
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
    operation_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9-]+$")
    symbol: str = Field(min_length=5, max_length=20)
    side: Literal["LONG", "SHORT"]
    leverage: int = Field(ge=1, le=30)
    stop_distance_pct: Decimal = Field(ge=Decimal("0.10"), le=Decimal("10"))
    tp1_r: Decimal = Field(ge=Decimal("0.5"), le=Decimal("10"))
    tp2_r: Decimal = Field(ge=Decimal("2"), le=Decimal("12"))
    confirmation: str = Field(default="", max_length=80)

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
