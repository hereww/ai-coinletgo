from __future__ import annotations

from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev
from typing import Literal

from trading_system.domain.models import Candle


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
    if len(candles) < 24 or trend == 0:
        return 0
    closes = [candle.close for candle in candles]
    ema20 = ema(closes, 20)[-1]
    current_atr = atr(candles)
    last = candles[-1]
    if current_atr <= 0:
        return 0
    near_ema = abs(last.low - ema20) <= current_atr * Decimal("0.3")
    if trend == 1 and near_ema and last.close > last.open and last.close > ema20:
        return 1
    near_ema = abs(last.high - ema20) <= current_atr * Decimal("0.3")
    if trend == -1 and near_ema and last.close < last.open and last.close < ema20:
        return -1
    return 0


def volume_zscore(candles: list[Candle], period: int = 20) -> Decimal:
    if len(candles) < 2:
        return Decimal("0")
    values = [float(candle.volume) for candle in candles[-period:]]
    deviation = pstdev(values)
    if deviation == 0:
        return Decimal("0")
    return Decimal(str((values[-1] - mean(values)) / deviation))


def pearson_correlation(left: list[Decimal], right: list[Decimal]) -> Decimal:
    size = min(len(left), len(right))
    if size < 20:
        return Decimal("1")
    x = [float(value) for value in left[-size:]]
    y = [float(value) for value in right[-size:]]
    x_mean = mean(x)
    y_mean = mean(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y, strict=True))
    x_var = sum((a - x_mean) ** 2 for a in x)
    y_var = sum((b - y_mean) ** 2 for b in y)
    denominator = sqrt(x_var * y_var)
    if denominator == 0:
        return Decimal("1")
    return Decimal(str(max(-1.0, min(1.0, numerator / denominator))))
