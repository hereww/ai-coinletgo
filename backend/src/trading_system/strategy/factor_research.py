from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import mean, pstdev, stdev
from typing import Any

from trading_system.domain.models import Candle, MarketSnapshot
from trading_system.strategy.factors import (
    FACTOR_DEFINITIONS,
    FactorBar,
    compute_factor_values,
    factor_data_available,
)


@dataclass(frozen=True)
class _PortfolioObservation:
    gross_return: float
    fee_cost: float
    slippage_cost: float
    funding_cost: float
    net_return: float
    turnover: float


@dataclass(frozen=True)
class _FactorObservation:
    timestamp: datetime
    ic: float
    observations: int
    membership: set[tuple[str, str]]
    decay: dict[int, float | None]
    portfolio: _PortfolioObservation | None
    portfolio_maker: _PortfolioObservation | None


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


def _membership(
    zscores: Mapping[str, float], quantile: Decimal = Decimal("0.2")
) -> set[tuple[str, str]]:
    if len(zscores) < 3:
        return set()
    size = max(1, math.ceil(len(zscores) * float(quantile)))
    ordered = sorted(zscores, key=lambda symbol: (zscores[symbol], symbol))
    result = {(symbol, "short") for symbol in ordered[:size]}
    result.update((symbol, "long") for symbol in ordered[-size:])
    return result


def _portfolio_weights(
    zscores: Mapping[str, float], quantile: Decimal
) -> tuple[dict[str, float], set[tuple[str, str]]]:
    """Build equal-weight dollar-neutral portfolios with gross exposure of one."""

    if len(zscores) < 3:
        return {}, set()
    size = max(1, math.ceil(len(zscores) * float(quantile)))
    ordered = sorted(zscores, key=lambda symbol: (zscores[symbol], symbol))
    shorts = ordered[:size]
    longs = ordered[-size:]
    side_weight = 0.5 / size
    weights = {symbol: side_weight for symbol in longs}
    weights.update({symbol: -side_weight for symbol in shorts})
    return weights, _membership(zscores, quantile)


def _portfolio_observation(
    zscores: Mapping[str, float],
    forward_returns: Mapping[str, Decimal],
    funding_rates: Mapping[str, Decimal | None],
    previous_weights: Mapping[str, float] | None,
    *,
    quantile: Decimal,
    bars_per_day: int,
    holding_bars: int,
    fee_rate: Decimal,
    slippage_rate: Decimal,
    funding_rate_fallback: Decimal,
) -> tuple[_PortfolioObservation | None, dict[str, float]]:
    valid = {symbol: value for symbol, value in zscores.items() if symbol in forward_returns}
    weights, _ = _portfolio_weights(valid, quantile)
    if not weights:
        return None, {}
    longs = [symbol for symbol, weight in weights.items() if weight > 0]
    shorts = [symbol for symbol, weight in weights.items() if weight < 0]
    if not longs or not shorts:
        return None, weights
    gross = sum(
        weights[symbol] * float(forward_returns[symbol]) for symbol in weights
    )
    old = previous_weights or {}
    turnover = sum(
        abs(weights.get(symbol, 0.0) - old.get(symbol, 0.0))
        for symbol in set(weights).union(old)
    )
    fee = turnover * float(fee_rate)
    slippage = turnover * float(slippage_rate)

    def rate_for(symbol: str) -> Decimal:
        value = funding_rates.get(symbol)
        return value if value is not None and value.is_finite() else funding_rate_fallback

    # Binance settles funding every eight hours.  The rate is deliberately
    # read from the latest event known at the signal timestamp only.
    settlements = Decimal(holding_bars) / Decimal(bars_per_day) * Decimal("3")
    funding = float(
        sum(-Decimal(str(weights[symbol])) * rate_for(symbol) for symbol in weights)
        * settlements
    )
    return (
        _PortfolioObservation(
            gross_return=gross,
            fee_cost=fee,
            slippage_cost=slippage,
            funding_cost=funding,
            net_return=gross - fee - slippage + funding,
            turnover=turnover,
        ),
        weights,
    )


def _portfolio_statistics(
    observations: Sequence[_FactorObservation], *, bars_per_year: int, rebalance_bars: int
) -> dict[str, float | int | None]:
    rows = [row.portfolio for row in observations if row.portfolio is not None]
    if not rows:
        return {
            "observations": 0,
            "gross_return": None,
            "fee_cost": None,
            "slippage_cost": None,
            "funding_cost": None,
            "net_return": None,
            "average_turnover": None,
            "total_turnover": None,
            "max_drawdown": None,
            "annualized_volatility": None,
            "sharpe": None,
        }
    gross_equity = 1.0
    net_equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    net_returns: list[float] = []
    for row in rows:
        gross_equity *= 1 + row.gross_return
        net_equity *= 1 + row.net_return
        peak = max(peak, net_equity)
        max_drawdown = max(max_drawdown, (peak - net_equity) / peak)
        net_returns.append(row.net_return)
    deviation = pstdev(net_returns) if len(net_returns) >= 2 else None
    annualization = math.sqrt(bars_per_year / max(1, rebalance_bars))
    return {
        "observations": len(rows),
        "gross_return": gross_equity - 1,
        "fee_cost": sum(row.fee_cost for row in rows),
        "slippage_cost": sum(row.slippage_cost for row in rows),
        "funding_cost": sum(row.funding_cost for row in rows),
        "net_return": net_equity - 1,
        "average_turnover": mean(row.turnover for row in rows),
        "total_turnover": sum(row.turnover for row in rows),
        "max_drawdown": max_drawdown,
        "annualized_volatility": deviation * annualization if deviation is not None else None,
        "sharpe": (
            mean(net_returns) / deviation * annualization
            if deviation is not None and deviation > 0
            else None
        ),
    }


def _orient_portfolio(
    portfolio: _PortfolioObservation | None, direction: int
) -> _PortfolioObservation | None:
    """Orient returns to the direction learned from a prior training window."""

    if portfolio is None or direction >= 0:
        return portfolio
    return _PortfolioObservation(
        gross_return=-portfolio.gross_return,
        fee_cost=portfolio.fee_cost,
        slippage_cost=portfolio.slippage_cost,
        funding_cost=-portfolio.funding_cost,
        net_return=(
            -portfolio.gross_return
            - portfolio.fee_cost
            - portfolio.slippage_cost
            - portfolio.funding_cost
        ),
        turnover=portfolio.turnover,
    )


def _walk_forward_slices(timestamp_count: int, requested_folds: int) -> list[tuple[int, int, int]]:
    """Create expanding train/test windows without looking ahead."""

    if timestamp_count < 6:
        return []
    fold_count = min(max(1, requested_folds), max(1, timestamp_count // 3))
    test_size = max(1, timestamp_count // (fold_count + 1))
    initial_train = timestamp_count - fold_count * test_size
    if initial_train < 3:
        test_size = max(1, (timestamp_count - 3) // fold_count)
        initial_train = timestamp_count - fold_count * test_size
    return [
        (
            initial_train + index * test_size,
            min(timestamp_count, initial_train + (index + 1) * test_size),
            index + 1,
        )
        for index in range(fold_count)
        if initial_train + index * test_size >= 3
        and initial_train + index * test_size < timestamp_count
    ]


def _walk_forward_reports(
    factor_rows: Mapping[str, Sequence[_FactorObservation]],
    timestamps: Sequence[datetime],
    *,
    requested_folds: int,
    bars_per_year: int,
    rebalance_bars: int,
) -> dict[str, list[dict[str, Any]]]:
    """Select factors on train windows and report only subsequent test windows."""

    result: dict[str, list[dict[str, Any]]] = {
        definition.key: [] for definition in FACTOR_DEFINITIONS
    }
    for train_end, test_end, fold in _walk_forward_slices(len(timestamps), requested_folds):
        train_start_time = timestamps[0]
        train_end_time = timestamps[train_end - 1]
        test_start_time = timestamps[train_end]
        test_end_time = timestamps[min(test_end, len(timestamps)) - 1]
        train_p_values: dict[str, float] = {}
        train_rows_by_factor: dict[str, list[_FactorObservation]] = {}
        test_rows_by_factor: dict[str, list[_FactorObservation]] = {}
        for definition in FACTOR_DEFINITIONS:
            rows = list(factor_rows[definition.key])
            train_rows = [
                row for row in rows if train_start_time <= row.timestamp <= train_end_time
            ]
            test_rows = [
                row for row in rows if test_start_time <= row.timestamp <= test_end_time
            ]
            train_rows_by_factor[definition.key] = train_rows
            test_rows_by_factor[definition.key] = test_rows
            p_value = _mean_ic_p_value([row.ic for row in train_rows])
            if p_value is not None:
                train_p_values[definition.key] = p_value
        train_q_values = benjamini_hochberg(train_p_values)
        for definition in FACTOR_DEFINITIONS:
            train_rows = train_rows_by_factor[definition.key]
            test_rows = test_rows_by_factor[definition.key]
            train_mean = _mean_or_none([row.ic for row in train_rows])
            train_q = train_q_values.get(definition.key)
            train_direction = -1 if train_mean is not None and train_mean < 0 else 1
            selected = (
                len(train_rows) >= 3
                and train_mean is not None
                and abs(train_mean) >= 0.02
                and train_q is not None
                and train_q <= 0.05
            )
            oriented_test_rows = [
                _FactorObservation(
                    timestamp=row.timestamp,
                    ic=row.ic,
                    observations=row.observations,
                    membership=row.membership,
                    decay=row.decay,
                    portfolio=_orient_portfolio(row.portfolio, train_direction),
                    portfolio_maker=_orient_portfolio(row.portfolio_maker, train_direction),
                )
                for row in test_rows
            ]
            test_stats = _portfolio_statistics(
                oriented_test_rows,
                bars_per_year=bars_per_year,
                rebalance_bars=rebalance_bars,
            )
            test_mean = _mean_or_none([row.ic for row in test_rows])
            result[definition.key].append(
                {
                    "fold": fold,
                    "train": {
                        "start": train_start_time.isoformat(),
                        "end": train_end_time.isoformat(),
                        "timestamp_count": len(train_rows),
                        "mean_ic": train_mean,
                        "q_value": train_q,
                        "direction": "POSITIVE" if train_direction > 0 else "NEGATIVE",
                        "selected": selected,
                    },
                    "test": {
                        "start": test_start_time.isoformat(),
                        "end": test_end_time.isoformat(),
                        "timestamp_count": len(test_rows),
                        "mean_ic": test_mean,
                        "direction_stable": (
                            test_mean is not None
                            and train_mean is not None
                            and test_mean * train_mean > 0
                        ),
                        "gross_return": test_stats["gross_return"],
                        "net_return": test_stats["net_return"],
                        "max_drawdown": test_stats["max_drawdown"],
                        "sharpe": test_stats["sharpe"],
                    },
                    "selected_on_train": selected,
                }
            )
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
    maker_fee_rate: Decimal = Decimal("0.0002"),
    taker_fee_rate: Decimal = Decimal("0.0005"),
    slippage_rate: Decimal = Decimal("0.0005"),
    funding_rate_fallback: Decimal = Decimal("0.0001"),
    walk_forward_folds: int = 4,
    portfolio_quantile: Decimal = Decimal("0.2"),
) -> dict[str, Any]:
    """Evaluate point-in-time factors, net long/short returns, and walk-forward OOS."""

    if not markets:
        raise ValueError("factor research has no market data")
    if forward_bars <= 0 or rebalance_bars <= 0:
        raise ValueError("forward and rebalance bars must be positive")
    if bars_per_day <= 0 or bars_per_year <= 0:
        raise ValueError("bar frequencies must be positive")
    if not 0 < portfolio_quantile <= Decimal("0.5"):
        raise ValueError("portfolio quantile must be between 0 and 0.5")
    if walk_forward_folds <= 0:
        raise ValueError("walk-forward folds must be positive")
    if maker_fee_rate < 0 or taker_fee_rate < 0 or slippage_rate < 0:
        raise ValueError("transaction costs cannot be negative")

    # Report the conservative taker-cost case by default. The maker fee is
    # retained in the methodology so an operator can compare assumptions.
    fee_rate = taker_fee_rate

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
    previous_weights: dict[str, dict[str, float]] = {}

    for timestamp in timestamps:
        by_factor: dict[str, dict[str, Decimal]] = {
            definition.key: {} for definition in FACTOR_DEFINITIONS
        }
        returns_by_horizon: dict[int, dict[str, Decimal]] = {
            horizon: {} for horizon in decay_horizons
        }
        rebalance_returns: dict[str, Decimal] = {}
        funding_rates: dict[str, Decimal | None] = {}
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
            funding_rates[symbol] = rows[index].funding_rate
            for horizon in decay_horizons:
                forward_value = _forward_return(rows, index, horizon)
                if forward_value is not None and forward_value.is_finite():
                    returns_by_horizon[horizon][symbol] = forward_value
            rebalance_value = _forward_return(rows, index, rebalance_bars)
            if rebalance_value is not None and rebalance_value.is_finite():
                rebalance_returns[symbol] = rebalance_value

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
                portfolio, weights = _portfolio_observation(
                    zscores,
                    rebalance_returns,
                    funding_rates,
                    previous_weights.get(definition.key),
                    quantile=portfolio_quantile,
                    bars_per_day=bars_per_day,
                    holding_bars=rebalance_bars,
                    fee_rate=fee_rate,
                    slippage_rate=slippage_rate,
                    funding_rate_fallback=funding_rate_fallback,
                )
                portfolio_maker, _ = _portfolio_observation(
                    zscores,
                    rebalance_returns,
                    funding_rates,
                    previous_weights.get(definition.key),
                    quantile=portfolio_quantile,
                    bars_per_day=bars_per_day,
                    holding_bars=rebalance_bars,
                    fee_rate=maker_fee_rate,
                    slippage_rate=slippage_rate,
                    funding_rate_fallback=funding_rate_fallback,
                )
                previous_weights[definition.key] = weights
                factor_rows[definition.key].append(
                    _FactorObservation(
                        timestamp=timestamp,
                        ic=main_ic,
                        observations=observation_count,
                        membership=_membership(zscores, portfolio_quantile),
                        decay=decay,
                        portfolio=portfolio,
                        portfolio_maker=portfolio_maker,
                    )
                )

    flattened = [bar for rows in market_rows.values() for bar in rows]
    walk_forward = _walk_forward_reports(
        factor_rows,
        timestamps,
        requested_folds=walk_forward_folds,
        bars_per_year=bars_per_year,
        rebalance_bars=rebalance_bars,
    )
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
        portfolio_stats = _portfolio_statistics(
            observation_rows,
            bars_per_year=bars_per_year,
            rebalance_bars=rebalance_bars,
        )
        maker_portfolio_stats = _portfolio_statistics(
            [
                _FactorObservation(
                    timestamp=row.timestamp,
                    ic=row.ic,
                    observations=row.observations,
                    membership=row.membership,
                    decay=row.decay,
                    portfolio=row.portfolio_maker,
                    portfolio_maker=row.portfolio_maker,
                )
                for row in observation_rows
            ],
            bars_per_year=bars_per_year,
            rebalance_bars=rebalance_bars,
        )
        folds = walk_forward[definition.key]
        selected_folds = [fold for fold in folds if fold["selected_on_train"]]
        selected_test_rows: list[_FactorObservation] = []
        for fold in selected_folds:
            test_start = datetime.fromisoformat(str(fold["test"]["start"]))
            test_end = datetime.fromisoformat(str(fold["test"]["end"]))
            direction = -1 if str(fold["train"].get("direction")) == "NEGATIVE" else 1
            selected_test_rows.extend(
                _FactorObservation(
                    timestamp=row.timestamp,
                    ic=row.ic,
                    observations=row.observations,
                    membership=row.membership,
                    decay=row.decay,
                    portfolio=_orient_portfolio(row.portfolio, direction),
                    portfolio_maker=_orient_portfolio(row.portfolio_maker, direction),
                )
                for row in observation_rows
                if test_start <= row.timestamp <= test_end
            )
        walk_forward_portfolio = _portfolio_statistics(
            selected_test_rows,
            bars_per_year=bars_per_year,
            rebalance_bars=rebalance_bars,
        )
        walk_forward_oos_ics = [row.ic for row in selected_test_rows]
        stable_oos_folds = sum(
            bool(fold["test"].get("direction_stable")) for fold in selected_folds
        )
        positive_net_folds = sum(
            isinstance(fold["test"]["net_return"], (int, float))
            and float(fold["test"]["net_return"]) > 0
            for fold in selected_folds
        )
        walk_forward_ic = _mean_or_none(walk_forward_oos_ics)
        same_direction = (
            isinstance(average, float)
            and isinstance(walk_forward_ic, float)
            and average != 0
            and walk_forward_ic != 0
            and math.copysign(1, average) == math.copysign(1, walk_forward_ic)
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
                "walk_forward_oos_ic": walk_forward_ic,
                "p_value": p_value,
                "q_value": None,
                "turnover": _turnover([row.membership for row in observation_rows]),
                "timestamp_count": len(observation_rows),
                "observation_count": sum(row.observations for row in observation_rows),
                "decay": decay_report,
                "portfolio": portfolio_stats,
                "portfolio_maker": maker_portfolio_stats,
                "walk_forward_portfolio": walk_forward_portfolio,
                "walk_forward": folds,
                "unavailable_reason": (
                    None if source_available else _factor_source_reason(definition.required_data)
                ),
                "gates": {
                    "minimum_timestamps": len(observation_rows) >= 20,
                    "absolute_ic": average is not None and abs(average) >= 0.02,
                    "oos_same_direction": same_direction,
                    "fdr_5pct": False,
                    "walk_forward_folds": len(folds) >= 2,
                    "walk_forward_selected": len(selected_folds) >= 2,
                    "walk_forward_ic_stability": (
                        bool(selected_folds)
                        and stable_oos_folds / len(selected_folds) >= 0.5
                    ),
                    "walk_forward_net_positive": (
                        bool(selected_folds)
                        and positive_net_folds / len(selected_folds) >= 0.5
                        and isinstance(walk_forward_portfolio["net_return"], (int, float))
                        and float(walk_forward_portfolio["net_return"]) > 0
                    ),
                },
            }
        )

    q_values = benjamini_hochberg(p_values)
    for report in reports:
        key = str(report["key"])
        q_value = q_values.get(key)
        report["q_value"] = q_value
        gates = report["gates"]
        if not isinstance(gates, dict):
            raise TypeError("factor gates must be a dictionary")
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
            "validation": "rolling_walk_forward_expanding_train_test",
            "multiple_testing": "benjamini_hochberg",
            "icir": "unannualized_mean_ic_over_sample_std",
            "portfolio": "equal_weight_dollar_neutral_gross_exposure_1_top_bottom_quantiles",
            "cost_model": "taker_fee_plus_slippage_plus_point_in_time_funding",
            "maker_fee_rate": str(maker_fee_rate),
            "taker_fee_rate": str(taker_fee_rate),
            "slippage_rate": str(slippage_rate),
            "funding_rate_fallback": str(funding_rate_fallback),
            "portfolio_quantile": str(portfolio_quantile),
            "pass_rule": (
                "20期以上、|IC|>=0.02、BH-FDR q<=0.05、至少2个滚动折叠在训练集选中、"
                "样本外IC方向稳定且样本外净收益为正"
            ),
        },
    }


def build_factor_shadow_ranking(
    snapshots: Sequence[MarketSnapshot],
    research_run: Mapping[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any] | None:
    """Rank current snapshots with passed factors for shadow recording only."""

    report = research_run.get("report")
    if not isinstance(report, Mapping):
        return None
    raw_factors = report.get("factors")
    if not isinstance(raw_factors, list):
        return None
    passed = [
        item
        for item in raw_factors
        if isinstance(item, Mapping) and item.get("status") == "PASSED"
    ]
    if not passed or not snapshots:
        return None

    parameters = report.get("parameters")
    winsorize = Decimal("0.05")
    if isinstance(parameters, Mapping):
        try:
            winsorize = Decimal(str(parameters.get("winsorize_quantile", winsorize)))
        except (ArithmeticError, ValueError):
            winsorize = Decimal("0.05")

    factor_scores: dict[str, dict[str, float]] = {}
    for factor in passed:
        key = str(factor.get("key", ""))
        values = {
            snapshot.symbol: snapshot.factor_values[key]
            for snapshot in snapshots
            if key in snapshot.factor_values and snapshot.factor_values[key].is_finite()
        }
        scores = cross_sectional_zscores(values, winsorize)
        if str(factor.get("direction", "POSITIVE")) == "NEGATIVE":
            scores = {symbol: -value for symbol, value in scores.items()}
        factor_scores[key] = scores

    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        contributions = {
            key: scores[snapshot.symbol]
            for key, scores in factor_scores.items()
            if snapshot.symbol in scores
        }
        if not contributions:
            continue
        rows.append(
            {
                "symbol": snapshot.symbol,
                "score": mean(contributions.values()),
                "factor_coverage": len(contributions) / len(factor_scores),
                "contributions": contributions,
            }
        )
    if not rows:
        return None
    rows.sort(key=lambda row: (-float(row["score"]), str(row["symbol"])))
    return {
        "research_run_id": str(research_run.get("id", "")),
        "generated_at": datetime.now(snapshots[0].timestamp.tzinfo).isoformat(),
        "selected_factors": [
            {
                "key": str(item.get("key")),
                "label": str(item.get("label")),
                "direction": str(item.get("direction", "POSITIVE")),
            }
            for item in passed
        ],
        "rankings": rows[: max(1, limit)],
        "execution_effect": "shadow_only; does_not_change_candidates_or_orders",
    }
