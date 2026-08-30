from __future__ import annotations

from decimal import Decimal

from trading_system.domain.models import Candle, MarketSnapshot, UniverseSymbol
from trading_system.strategy.indicators import (
    adx,
    atr,
    donchian_breakout,
    pullback_signal,
    trend_direction,
    volume_zscore,
)


def close_returns(candles: list[Candle]) -> list[Decimal]:
    returns: list[Decimal] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        if previous.close > 0:
            returns.append((current.close - previous.close) / previous.close)
    return returns


def volatility_percentile(candles: list[Candle], period: int = 14) -> Decimal:
    if len(candles) < period * 2:
        return Decimal("0.5")
    samples: list[Decimal] = []
    for index in range(period, len(candles) + 1):
        samples.append(atr(candles[:index], period))
    current = samples[-1]
    rank = sum(value <= current for value in samples)
    return Decimal(rank) / Decimal(len(samples))


def build_snapshot(
    universe: UniverseSymbol,
    candles_15m: list[Candle],
    candles_1h: list[Candle],
    candles_4h: list[Candle],
    open_interest: Decimal,
    previous_open_interest: Decimal | None = None,
    book_depth_usdt: Decimal = Decimal("0"),
) -> MarketSnapshot:
    trend_1h = trend_direction(candles_1h)
    trend_4h = trend_direction(candles_4h)
    oi_change = Decimal("0")
    if previous_open_interest and previous_open_interest > 0:
        oi_change = (open_interest - previous_open_interest) / previous_open_interest
    return MarketSnapshot(
        symbol=universe.symbol,
        timestamp=candles_15m[-1].close_time,
        mark_price=universe.mark_price,
        index_price=universe.index_price,
        best_bid=universe.best_bid,
        best_ask=universe.best_ask,
        spread_pct=universe.spread_pct,
        quote_volume_24h=universe.quote_volume_24h,
        funding_rate=universe.funding_rate,
        basis_pct=(universe.mark_price - universe.index_price) / universe.index_price,
        book_depth_usdt=book_depth_usdt,
        open_interest=open_interest,
        open_interest_change_pct=oi_change,
        atr_15m=atr(candles_15m),
        adx_1h=adx(candles_1h),
        trend_1h=trend_1h,
        trend_4h=trend_4h,
        breakout_15m=donchian_breakout(candles_15m),
        pullback_15m=pullback_signal(candles_15m, trend_1h),
        volume_zscore=volume_zscore(candles_15m),
        volatility_percentile=volatility_percentile(candles_15m),
        listing_days=universe.listing_days,
        status=universe.status,
        recent_returns_1h=close_returns(candles_1h),
    )
