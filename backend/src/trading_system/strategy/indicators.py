from __future__ import annotations

from decimal import Decimal
from math import isfinite, sqrt
from statistics import mean, pstdev
from typing import Literal

from trading_system.domain.models import Candle

MarketRegime = Literal["TRENDING", "RANGING", "VOLATILE", "UNCERTAIN"]


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    if period <= 0 or not values:
        return []
    alpha = Decimal("2") / Decimal(period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (Decimal("1") - alpha)))
    return result


def true_ranges(candles: list[Candle]) -> list[Decimal]:
    if not candles:
        return []
    ranges = [candles[0].high - candles[0].low]
    for previous, current in zip(candles, candles[1:], strict=False):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return ranges


def atr(candles: list[Candle], period: int = 14) -> Decimal:
    ranges = true_ranges(candles)
    if not ranges:
        return Decimal("0")
    window = ranges[-period:]
    return sum(window, Decimal("0")) / Decimal(len(window))


def adx(candles: list[Candle], period: int = 14) -> Decimal:
    if len(candles) < period + 1:
        return Decimal("0")
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        up = current.high - previous.high
        down = previous.low - current.low
        plus_dm.append(up if up > down and up > 0 else Decimal("0"))
        minus_dm.append(down if down > up and down > 0 else Decimal("0"))
    tr = true_ranges(candles)[1:]
    tr_sum = sum(tr[-period:], Decimal("0"))
    if tr_sum == 0:
        return Decimal("0")
    plus_di = Decimal("100") * sum(plus_dm[-period:], Decimal("0")) / tr_sum
    minus_di = Decimal("100") * sum(minus_dm[-period:], Decimal("0")) / tr_sum
    denominator = plus_di + minus_di
    if denominator == 0:
        return Decimal("0")
    return Decimal("100") * abs(plus_di - minus_di) / denominator


def trend_direction(
    candles: list[Candle], fast: int = 20, slow: int = 50
) -> Literal[-1, 0, 1]:
    if len(candles) < slow:
        return 0
    closes = [candle.close for candle in candles]
    fast_values = ema(closes, fast)
    slow_values = ema(closes, slow)
    slope = fast_values[-1] - fast_values[-4]
    if fast_values[-1] > slow_values[-1] and slope > 0:
        return 1
    if fast_values[-1] < slow_values[-1] and slope < 0:
        return -1
    return 0


def donchian_breakout(candles: list[Candle], period: int = 20) -> Literal[-1, 0, 1]:
    if len(candles) <= period:
        return 0
    previous = candles[-period - 1 : -1]
    last = candles[-1]
    if last.close > max(candle.high for candle in previous):
        return 1
    if last.close < min(candle.low for candle in previous):
        return -1
    return 0


def pullback_signal(
    candles: list[Candle], trend: Literal[-1, 0, 1]
) -> Literal[-1, 0, 1]:
    """Confirm a pullback after a recent directional breakout.

    A candle merely touching EMA20 is not enough: the setup must first have a
    same-direction Donchian break, then retrace toward EMA20/breakout level,
    and finally close back in the trend direction within a bounded ATR move.
    """
    if len(candles) < 30 or trend == 0:
        return 0
    closes = [candle.close for candle in candles]
    ema20_values = ema(closes, 20)
    current_atr = atr(candles[-50:])
    last = candles[-1]
    if current_atr <= 0:
        return 0
    # Search only a recent window so a stale breakout cannot validate a new
    # entry.  Exclude the current candle, which is the confirmation candle.
    start = max(20, len(candles) - 6)
    breakout_index: int | None = None
    for index in range(start, len(candles) - 1):
        previous = candles[max(0, index - 20) : index]
        if len(previous) < 20:
            continue
        candidate = candles[index]
        if trend == 1 and candidate.close > max(item.high for item in previous):
            breakout_index = index
        elif trend == -1 and candidate.close < min(item.low for item in previous):
            breakout_index = index
    if breakout_index is None or breakout_index >= len(candles) - 1:
        return 0

    breakout_level = (
        max(item.high for item in candles[max(0, breakout_index - 20) : breakout_index])
        if trend == 1
        else min(item.low for item in candles[max(0, breakout_index - 20) : breakout_index])
    )
    ema20 = ema20_values[-1]
    retrace_window = candles[breakout_index + 1 : -1]
    if not retrace_window:
        return 0
    touched_retest = any(
        item.low <= max(ema20, breakout_level) + current_atr * Decimal("0.35")
        and item.high >= min(ema20, breakout_level) - current_atr * Decimal("0.35")
        for item in retrace_window
    )
    last_ema = ema20
    if trend == 1:
        confirmed = (
            last.close > last.open
            and last.close > last_ema
            and last.close > breakout_level
        )
        invalidated = min(item.close for item in retrace_window) < (
            breakout_level - current_atr * Decimal("0.5")
        )
    else:
        confirmed = (
            last.close < last.open
            and last.close < last_ema
            and last.close < breakout_level
        )
        invalidated = max(item.close for item in retrace_window) > (
            breakout_level + current_atr * Decimal("0.5")
        )
    if touched_retest and confirmed and not invalidated:
        return trend
    return 0


def classify_market_regime(
    trend_1h: Literal[-1, 0, 1],
    trend_4h: Literal[-1, 0, 1],
    adx_1h: Decimal,
    volatility_percentile: Decimal,
    *,
    trend_adx_min: Decimal = Decimal("20"),
    volatility_soft_limit: Decimal = Decimal("0.75"),
    volatility_hard_limit: Decimal = Decimal("0.90"),
) -> MarketRegime:
    if volatility_percentile >= volatility_hard_limit:
        return "VOLATILE"
    if trend_1h != 0 and trend_1h == trend_4h and adx_1h >= trend_adx_min:
        return "TRENDING"
    if volatility_percentile <= volatility_soft_limit:
        return "RANGING"
    return "UNCERTAIN"


def volatility_risk_multiplier(
    volatility_percentile: Decimal,
    *,
    soft_limit: Decimal = Decimal("0.75"),
    hard_limit: Decimal = Decimal("0.90"),
    elevated_multiplier: Decimal = Decimal("0.75"),
    high_multiplier: Decimal = Decimal("0.50"),
) -> Decimal:
    if volatility_percentile >= hard_limit:
        return high_multiplier
    if volatility_percentile > soft_limit:
        return elevated_multiplier
    return Decimal("1")


def volume_zscore(candles: list[Candle], period: int = 20) -> Decimal:
    if len(candles) < 2:
        return Decimal("0")
    values = [float(candle.volume) for candle in candles[-period:]]
    deviation = pstdev(values)
    if deviation == 0:
        return Decimal("0")
    return Decimal(str((values[-1] - mean(values)) / deviation))


def pearson_correlation(left: list[Decimal], right: list[Decimal]) -> Decimal:
    """Return a conservative correlation value for non-risk callers.

    Risk checks use :func:`strict_pearson_correlation` so missing or unusable
    data cannot be mistaken for a real low-correlation observation.
    """

    return strict_pearson_correlation(left, right) or Decimal("1")


def strict_pearson_correlation(
    left: list[Decimal], right: list[Decimal]
) -> Decimal | None:
    """Return correlation only when the input is sufficient for risk checks."""

    size = min(len(left), len(right))
    if size < 20:
        return None
    try:
        x = [float(value) for value in left[-size:]]
        y = [float(value) for value in right[-size:]]
    except (TypeError, ValueError):
        return None
    if not all(isfinite(value) for value in x + y):
        return None
    x_mean = mean(x)
    y_mean = mean(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y, strict=True))
    x_var = sum((a - x_mean) ** 2 for a in x)
    y_var = sum((b - y_mean) ** 2 for b in y)
    denominator = sqrt(x_var * y_var)
    if denominator == 0:
        return None
    return Decimal(str(max(-1.0, min(1.0, numerator / denominator))))
