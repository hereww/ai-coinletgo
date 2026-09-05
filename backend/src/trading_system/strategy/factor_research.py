from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import mean, stdev
from typing import Any

from trading_system.domain.models import Candle
from trading_system.strategy.factors import (
    FACTOR_DEFINITIONS,
    FactorBar,
    compute_factor_values,
    factor_data_available,
)


@dataclass(frozen=True)
class _FactorObservation:
    timestamp: datetime
    ic: float
    observations: int
    membership: set[tuple[str, str]]
    decay: dict[int, float | None]


def align_funding_point_in_time(
    candles: Sequence[Candle], funding_rates: Mapping[datetime, Decimal]
) -> list[FactorBar]:
    """Attach only the latest funding event known by each candle close."""

    events = sorted(funding_rates.items())
    event_index = 0
    latest_time: datetime | None = None
    latest_rate: Decimal | None = None
    rows: list[FactorBar] = []
    for candle in sorted(candles, key=lambda item: item.close_time):
        while event_index < len(events) and events[event_index][0] <= candle.close_time:
            latest_time, latest_rate = events[event_index]
            event_index += 1
        rows.append(
            FactorBar(
                timestamp=candle.close_time,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                funding_rate=latest_rate,
                funding_observed_at=latest_time,
            )
        )
    return rows


def _percentile(sorted_values: Sequence[float], quantile: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires values")
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def cross_sectional_zscores(
    values: Mapping[str, Decimal], winsorize_quantile: Decimal
) -> dict[str, float]:
    """Winsorize and z-score one point-in-time cross-section."""

    clean = {key: float(value) for key, value in values.items() if value.is_finite()}
    if len(clean) < 2:
        return {}
    ordered = sorted(clean.values())
    quantile = float(winsorize_quantile)
    lower = _percentile(ordered, quantile)
    upper = _percentile(ordered, 1 - quantile)
    clipped = {key: min(max(value, lower), upper) for key, value in clean.items()}
    center = mean(clipped.values())
    variance = mean((value - center) ** 2 for value in clipped.values())
    deviation = math.sqrt(variance)
    if deviation == 0:
        return {key: 0.0 for key in clipped}
    return {key: (value - center) / deviation for key, value in clipped.items()}


def _ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        average_rank = (cursor + 1 + end) / 2
        for index in range(cursor, end):
            ranks[ordered[index][0]] = average_rank
        cursor = end
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale == 0 or right_scale == 0:
        return None
    return numerator / (left_scale * right_scale)


def spearman_rank_ic(
    factor_values: Mapping[str, float], forward_returns: Mapping[str, Decimal]
) -> float | None:
    symbols = sorted(set(factor_values).intersection(forward_returns))
    if len(symbols) < 2:
        return None
    factor_ranks = _ranks([factor_values[symbol] for symbol in symbols])
    return_ranks = _ranks([float(forward_returns[symbol]) for symbol in symbols])
    return _pearson(factor_ranks, return_ranks)


def benjamini_hochberg(p_values: Mapping[str, float]) -> dict[str, float]:
    """Return monotone Benjamini-Hochberg adjusted p-values."""

    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 1.0
    for rank in range(count, 0, -1):
        key, value = ordered[rank - 1]
        running = min(running, value * count / rank)
        adjusted[key] = min(1.0, running)
    return adjusted


def _forward_return(bars: Sequence[FactorBar], index: int, horizon: int) -> Decimal | None:
    future_index = index + horizon
    if future_index >= len(bars) or bars[index].close <= 0:
        return None
    return bars[future_index].close / bars[index].close - Decimal("1")


def _mean_or_none(values: Sequence[float]) -> float | None:
    return mean(values) if values else None


def _mean_ic_p_value(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    deviation = stdev(values)
    if deviation == 0:
        return 0.0 if mean(values) != 0 else 1.0
    statistic = mean(values) / (deviation / math.sqrt(len(values)))
    return math.erfc(abs(statistic) / math.sqrt(2))


def _membership(zscores: Mapping[str, float]) -> set[tuple[str, str]]:
    if len(zscores) < 3:
        return set()
    size = max(1, math.ceil(len(zscores) * 0.2))
    ordered = sorted(zscores, key=lambda symbol: (zscores[symbol], symbol))
    result = {(symbol, "short") for symbol in ordered[:size]}
    result.update((symbol, "long") for symbol in ordered[-size:])
    return result


def _turnover(memberships: Sequence[set[tuple[str, str]]]) -> float | None:
    changes: list[float] = []
    for previous, current in zip(memberships, memberships[1:], strict=False):
        denominator = max(len(previous), len(current))
        if denominator:
            changes.append(1 - len(previous.intersection(current)) / denominator)
    return _mean_or_none(changes)


def _factor_source_reason(required_data: Sequence[str]) -> str:
    unavailable_labels = {
        "open_interest": "历史OI",
        "basis": "历史基差",
        "order_book": "历史盘口",
    }
    missing = [unavailable_labels[item] for item in required_data if item in unavailable_labels]
    return "缺少" + "、".join(missing) + "数据"


def run_factor_research(
    markets: Mapping[str, Sequence[FactorBar]],
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
    bars_per_day: int,
    bars_per_year: int,
    forward_bars: int,
    rebalance_bars: int,
    min_cross_section: int,
    winsorize_quantile: Decimal,
) -> dict[str, Any]:
    """Evaluate point-in-time factors against later cross-sectional returns."""

    if not markets:
        raise ValueError("factor research has no market data")
    if forward_bars <= 0 or rebalance_bars <= 0:
        raise ValueError("forward and rebalance bars must be positive")

    market_rows = {
        symbol: list(sorted(rows, key=lambda item: item.timestamp))
        for symbol, rows in markets.items()
    }
    indexes = {
        symbol: {bar.timestamp: index for index, bar in enumerate(rows)}
        for symbol, rows in market_rows.items()
    }
    timestamps = sorted(
        {
            bar.timestamp
            for rows in market_rows.values()
            for bar in rows
            if evaluation_start <= bar.timestamp < evaluation_end
        }
    )[::rebalance_bars]
    decay_horizons = sorted({1, min(4, forward_bars), forward_bars})
    factor_rows: dict[str, list[_FactorObservation]] = {
        definition.key: [] for definition in FACTOR_DEFINITIONS
    }

    for timestamp in timestamps:
        by_factor: dict[str, dict[str, Decimal]] = {
            definition.key: {} for definition in FACTOR_DEFINITIONS
        }
        returns_by_horizon: dict[int, dict[str, Decimal]] = {
            horizon: {} for horizon in decay_horizons
        }
        for symbol, rows in market_rows.items():
            index = indexes[symbol].get(timestamp)
            if index is None:
                continue
            computed_values = compute_factor_values(
                rows[: index + 1], bars_per_day=bars_per_day, bars_per_year=bars_per_year
            )
            for key, factor_value in computed_values.items():
                if factor_value is not None and factor_value.is_finite():
                    by_factor[key][symbol] = factor_value
            for horizon in decay_horizons:
                forward_value = _forward_return(rows, index, horizon)
                if forward_value is not None and forward_value.is_finite():
                    returns_by_horizon[horizon][symbol] = forward_value

        for definition in FACTOR_DEFINITIONS:
            zscores = cross_sectional_zscores(by_factor[definition.key], winsorize_quantile)
            main_returns = returns_by_horizon[forward_bars]
            observation_count = len(set(zscores).intersection(main_returns))
            main_ic = (
                spearman_rank_ic(zscores, main_returns)
                if observation_count >= min_cross_section
                else None
            )
            decay: dict[int, float | None] = {}
            for horizon in decay_horizons:
                horizon_returns = returns_by_horizon[horizon]
                decay[horizon] = (
                    spearman_rank_ic(zscores, horizon_returns)
                    if len(set(zscores).intersection(horizon_returns)) >= min_cross_section
                    else None
                )
            if main_ic is not None:
                factor_rows[definition.key].append(
                    _FactorObservation(
                        timestamp=timestamp,
                        ic=main_ic,
                        observations=observation_count,
                        membership=_membership(zscores),
                        decay=decay,
                    )
                )

    flattened = [bar for rows in market_rows.values() for bar in rows]
    reports: list[dict[str, Any]] = []
    p_values: dict[str, float] = {}
    for definition in FACTOR_DEFINITIONS:
        observation_rows = factor_rows[definition.key]
        source_available = factor_data_available(definition, flattened)
        ics = [row.ic for row in observation_rows]
        split = len(ics) // 2
        in_sample = ics[:split]
        out_of_sample = ics[split:]
        deviation = stdev(ics) if len(ics) >= 2 else None
        average = _mean_or_none(ics)
        p_value = _mean_ic_p_value(ics)
        if p_value is not None:
            p_values[definition.key] = p_value
        decay_report: list[dict[str, float | int | None]] = []
        for horizon in decay_horizons:
            decay_values = [
                decay_value
                for row in observation_rows
                if (decay_value := row.decay[horizon]) is not None
            ]
            decay_report.append(
                {
                    "forward_bars": horizon,
                    "mean_ic": _mean_or_none(decay_values),
                    "timestamp_count": len(decay_values),
                }
            )
        reports.append(
            {
                "key": definition.key,
                "label": definition.label,
                "category": definition.category,
                "description": definition.description,
                "required_data": list(definition.required_data),
                "source_available": source_available,
                "status": "PENDING",
                "direction": "POSITIVE" if average is not None and average >= 0 else "NEGATIVE",
                "mean_ic": average,
                "ic_std": deviation,
                "icir": average / deviation
                if average is not None and deviation is not None and deviation != 0
                else None,
                "positive_ic_rate": (sum(value > 0 for value in ics) / len(ics) if ics else None),
                "in_sample_ic": _mean_or_none(in_sample),
                "out_of_sample_ic": _mean_or_none(out_of_sample),
                "p_value": p_value,
                "q_value": None,
                "turnover": _turnover([row.membership for row in observation_rows]),
                "timestamp_count": len(observation_rows),
                "observation_count": sum(row.observations for row in observation_rows),
                "decay": decay_report,
                "unavailable_reason": (
                    None if source_available else _factor_source_reason(definition.required_data)
                ),
                "gates": {
                    "minimum_timestamps": len(observation_rows) >= 20,
                    "absolute_ic": average is not None and abs(average) >= 0.02,
                    "oos_same_direction": False,
                    "fdr_5pct": False,
                },
            }
        )

    q_values = benjamini_hochberg(p_values)
    for report in reports:
        key = str(report["key"])
        q_value = q_values.get(key)
        report["q_value"] = q_value
        average = report["mean_ic"]
        out_of_sample = report["out_of_sample_ic"]
        same_direction = (
            isinstance(average, float)
            and isinstance(out_of_sample, float)
            and average != 0
            and out_of_sample != 0
            and math.copysign(1, average) == math.copysign(1, out_of_sample)
        )
        gates = report["gates"]
        if not isinstance(gates, dict):
            raise TypeError("factor gates must be a dictionary")
        gates["oos_same_direction"] = same_direction
        gates["fdr_5pct"] = q_value is not None and q_value <= 0.05
        if not report["source_available"]:
            report["status"] = "UNAVAILABLE"
        elif not gates["minimum_timestamps"]:
            report["status"] = "INSUFFICIENT"
            report["unavailable_reason"] = "有效横截面少于20期"
        elif all(gates.values()):
            report["status"] = "PASSED"
        else:
            report["status"] = "WATCH"

    counts = {
        status: sum(report["status"] == status for report in reports)
        for status in ("PASSED", "WATCH", "INSUFFICIENT", "UNAVAILABLE")
    }
    return {
        "generated_at": datetime.now(evaluation_start.tzinfo).isoformat(),
        "summary": {
            "factor_count": len(reports),
            "passed": counts["PASSED"],
            "watch": counts["WATCH"],
            "insufficient": counts["INSUFFICIENT"],
            "unavailable": counts["UNAVAILABLE"],
            "timestamps_evaluated": len(timestamps),
            "total_observations": sum(int(report["observation_count"]) for report in reports),
        },
        "factors": reports,
        "methodology": {
            "signal_timing": "bar_close_point_in_time",
            "target": "cross_sectional_forward_return",
            "correlation": "spearman_rank_ic",
            "normalization": "cross_sectional_winsorized_zscore",
            "validation": "chronological_half_holdout",
            "multiple_testing": "benjamini_hochberg",
            "icir": "unannualized_mean_ic_over_sample_std",
            "pass_rule": "20期以上、|IC|>=0.02、样本外同向且BH-FDR q<=0.05",
        },
    }
