from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_system.domain.enums import PositionSide, SignalAction, SystemMode
from trading_system.domain.models import (
    AccountState,
    ExchangeFilters,
    MarketSnapshot,
    PositionState,
    RiskContext,
    RiskLimits,
    TradeSignal,
)


def snapshot(**updates: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timestamp": datetime.now(UTC),
        "mark_price": Decimal("100"),
        "index_price": Decimal("100"),
        "best_bid": Decimal("99.9"),
        "best_ask": Decimal("100.1"),
        "spread_pct": Decimal("0.001"),
        "quote_volume_24h": Decimal("1000000"),
        "funding_rate": Decimal("0.0001"),
        "basis_pct": Decimal("0.001"),
        "book_depth_usdt": Decimal("1000000"),
        "open_interest": Decimal("10000"),
        "atr_15m": Decimal("1"),
        "adx_1h": Decimal("30"),
        "trend_1h": 1,
        "trend_4h": 1,
        "breakout_15m": 1,
        "pullback_15m": 0,
        "volume_zscore": Decimal("2"),
        "volatility_percentile": Decimal("0.5"),
        "listing_days": 365,
        "recent_returns_1h": [Decimal(index) / Decimal("10000") for index in range(30)],
    }
    values.update(updates)
    return MarketSnapshot.model_validate(values)


def signal(**updates: object) -> TradeSignal:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "action": SignalAction.OPEN_LONG,
        "confidence": Decimal("0.80"),
        "entry_min": Decimal("99.9"),
        "entry_max": Decimal("100.1"),
        "invalidation_price": Decimal("99"),
        "target_price": Decimal("103"),
        "thesis": "trend continuation",
        "expires_at": datetime.now(UTC) + timedelta(minutes=15),
    }
    values.update(updates)
    return TradeSignal.model_validate(values)


def position(**updates: object) -> PositionState:
    values: dict[str, object] = {
        "position_id": "binance-ETHUSDT-LONG",
        "symbol": "ETHUSDT",
        "side": PositionSide.LONG,
        "quantity": Decimal("1"),
        "initial_quantity": Decimal("1"),
        "entry_price": Decimal("50"),
        "mark_price": Decimal("51"),
        "stop_price": Decimal("49"),
        "original_stop_price": Decimal("49"),
        "initial_risk_usdt": Decimal("1"),
        "margin_used": Decimal("20"),
    }
    values.update(updates)
    return PositionState.model_validate(values)


def context(**updates: object) -> RiskContext:
    values: dict[str, object] = {
        "mode": SystemMode.TESTNET,
        "account": AccountState(
            equity=Decimal("1000"),
            available_balance=Decimal("1000"),
            day_start_equity=Decimal("1000"),
            high_water_mark=Decimal("1000"),
        ),
        "positions": [],
        "filters": ExchangeFilters(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.1"),
            min_quantity=Decimal("0.1"),
            min_notional=Decimal("5"),
        ),
        "limits": RiskLimits(),
    }
    values.update(updates)
    return RiskContext.model_validate(values)
