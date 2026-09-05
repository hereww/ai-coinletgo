from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import mean, pstdev

FactorValue = Decimal | None


@dataclass(frozen=True)
class FactorBar:
    """Point-in-time input for factor research.

    Optional derivatives and microstructure fields deliberately remain null
    when the exchange cannot provide their history. This prevents a missing
    series from becoming a synthetic zero in research results.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    funding_rate: Decimal | None = None
    funding_observed_at: datetime | None = None
    open_interest: Decimal | None = None
    basis_pct: Decimal | None = None
    spread_pct: Decimal | None = None
    book_imbalance: Decimal | None = None


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    label: str
    category: str
    description: str
    required_data: tuple[str, ...]


FACTOR_DEFINITIONS: tuple[FactorDefinition, ...] = (
    FactorDefinition(
        "momentum_5d", "5日动量", "动量", "当前收盘价相对5日前收盘价的收益率", ("price",)
    ),
    FactorDefinition(
        "momentum_decay_5d_minus_20d",
        "5日减20日动量衰减",
        "动量",
        "5日收益率减去20日收益率，用于识别动能加速或衰减",
        ("price",),
    ),
    FactorDefinition(
        "distance_5d_high",
        "距离5日高点",
        "趋势",
        "收盘价相对最近5日最高价的位置，越接近0越强",
        ("price",),
    ),
    FactorDefinition(
        "distance_20d_high", "距离20日高点", "趋势", "收盘价相对最近20日最高价的位置", ("price",)
    ),
    FactorDefinition(
        "realized_volatility_20d",
        "20日实现波动率",
        "波动率",
        "最近20日对数收益率的年化波动率，按当前K线周期计算",
        ("price",),
    ),
    FactorDefinition(
        "downside_upside_vol_ratio",
        "下行/上行波动率比",
        "波动率",
        "最近20日下行收益波动率除以上行收益波动率",
        ("price",),
    ),
    FactorDefinition(
        "volume_price_confirmation",
        "成交量价格确认",
        "量价",
        "20日收益率乘以20日成交量z-score，衡量放量是否确认方向",
        ("price", "volume"),
    ),
    FactorDefinition(
        "funding_zscore",
        "资金费率z-score",
        "衍生品",
        "最新已发生资金费率相对最近20次资金费率事件的标准化偏离",
        ("funding",),
    ),
    FactorDefinition(
        "funding_oi_quality",
        "Funding×OI质量",
        "衍生品",
        "资金费率z-score与OI变化的组合，区分拥挤杠杆与价格确认",
        ("funding", "open_interest"),
    ),
    FactorDefinition(
        "price_oi_state",
        "价格-OI状态",
        "衍生品",
        "20日价格收益率与20日OI变化率的乘积",
        ("price", "open_interest"),
    ),
    FactorDefinition(
        "basis_zscore", "基差zscore", "衍生品", "基差相对最近20个有效观测的标准化偏离", ("basis",)
    ),
    FactorDefinition(
        "book_imbalance",
        "盘口失衡",
        "微观结构",
        "(买方深度-卖方深度)/(买方深度+卖方深度)的历史观测",
        ("order_book",),
    ),
)

FACTOR_DEFINITION_BY_KEY = {item.key: item for item in FACTOR_DEFINITIONS}


def _finite(value: Decimal | None) -> bool:
    return value is not None and value.is_finite()


def _return(current: Decimal, previous: Decimal) -> Decimal | None:
    if not current.is_finite() or not previous.is_finite() or previous <= 0:
        return None
    return current / previous - Decimal("1")


def _returns(bars: list[FactorBar]) -> list[Decimal]:
    values: list[Decimal] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        value = _return(current.close, previous.close)
        if value is not None:
            values.append(value)
    return values


def _zscore(value: Decimal, values: Iterable[Decimal]) -> Decimal | None:
    clean = [item for item in values if item.is_finite()]
    if len(clean) < 2:
        return None
    deviation = pstdev(float(item) for item in clean)
    if deviation == 0:
        return Decimal("0")
    return Decimal(str((float(value) - float(mean(clean))) / deviation))


def _rolling_return(bars: list[FactorBar], lookback: int) -> Decimal | None:
    if len(bars) <= lookback:
        return None
    return _return(bars[-1].close, bars[-1 - lookback].close)


def _volume_zscore(bars: list[FactorBar], lookback: int) -> Decimal | None:
    if len(bars) < lookback:
        return None
    volumes = [item.volume for item in bars[-lookback:]]
    if any(not item.is_finite() for item in volumes):
        return None
    return _zscore(volumes[-1], volumes)


def _realized_volatility(
    bars: list[FactorBar], lookback: int, bars_per_year: int
) -> Decimal | None:
    if len(bars) <= lookback:
        return None
    returns = _returns(bars[-lookback - 1 :])
    if len(returns) != lookback:
        return None
    return Decimal(str(pstdev(float(item) for item in returns) * math.sqrt(bars_per_year)))


def _funding_event_values(bars: list[FactorBar], limit: int) -> list[Decimal]:
    by_timestamp: dict[datetime, Decimal] = {}
    for bar in bars:
        if (
            bar.funding_observed_at is not None
            and bar.funding_observed_at <= bar.timestamp
            and bar.funding_rate is not None
            and bar.funding_rate.is_finite()
        ):
            by_timestamp[bar.funding_observed_at] = bar.funding_rate
    ordered = [by_timestamp[key] for key in sorted(by_timestamp)]
    return ordered[-limit:]


def _rolling_open_interest_change(bars: list[FactorBar], lookback: int) -> Decimal | None:
    if len(bars) <= lookback:
        return None
    current = bars[-1].open_interest
    previous = bars[-1 - lookback].open_interest
    if current is None or previous is None:
        return None
    if not current.is_finite() or not previous.is_finite() or previous <= 0:
        return None
    return current / previous - Decimal("1")


def compute_factor_values(
    bars: list[FactorBar], *, bars_per_day: int, bars_per_year: int
) -> dict[str, FactorValue]:
    """Compute built-in factors using only rows at or before the final bar."""

    if not bars:
        return {item.key: None for item in FACTOR_DEFINITIONS}
    if bars_per_day <= 0 or bars_per_year <= 0:
        raise ValueError("bar frequencies must be positive")

    lookback_5d = 5 * bars_per_day
    lookback_20d = 20 * bars_per_day
    return_5d = _rolling_return(bars, lookback_5d)
    return_20d = _rolling_return(bars, lookback_20d)
    last_close = bars[-1].close

    distance_5d: Decimal | None = None
    if len(bars) >= lookback_5d:
        distance_5d = _return(last_close, max(item.high for item in bars[-lookback_5d:]))

    distance_20d: Decimal | None = None
    if len(bars) >= lookback_20d:
        distance_20d = _return(last_close, max(item.high for item in bars[-lookback_20d:]))

    downside_upside: Decimal | None = None
    if len(bars) > lookback_20d:
        recent_returns = _returns(bars[-lookback_20d - 1 :])
        downside = [item for item in recent_returns if item < 0]
        upside = [item for item in recent_returns if item > 0]
        if len(downside) >= 2 and len(upside) >= 2:
            downside_vol = pstdev(float(item) for item in downside)
            upside_vol = pstdev(float(item) for item in upside)
            if upside_vol > 0:
                downside_upside = Decimal(str(downside_vol / upside_vol))

    funding_values = _funding_event_values(bars, 20)
    current_funding = bars[-1].funding_rate
    funding_z = (
        _zscore(current_funding, funding_values)
        if current_funding is not None and current_funding.is_finite() and len(funding_values) >= 20
        else None
    )
    oi_change = _rolling_open_interest_change(bars, lookback_20d)
    basis_values = [
        item.basis_pct
        for item in bars[-20:]
        if item.basis_pct is not None and item.basis_pct.is_finite()
    ]
    current_basis = bars[-1].basis_pct
    basis_z = (
        _zscore(current_basis, basis_values)
        if current_basis is not None and current_basis.is_finite() and len(basis_values) >= 20
        else None
    )
    volume_z = _volume_zscore(bars, lookback_20d)
    imbalance = bars[-1].book_imbalance

    return {
        "momentum_5d": return_5d,
        "momentum_decay_5d_minus_20d": (
            return_5d - return_20d if return_5d is not None and return_20d is not None else None
        ),
        "distance_5d_high": distance_5d,
        "distance_20d_high": distance_20d,
        "realized_volatility_20d": _realized_volatility(bars, lookback_20d, bars_per_year),
        "downside_upside_vol_ratio": downside_upside,
        "volume_price_confirmation": (
            return_20d * volume_z if return_20d is not None and volume_z is not None else None
        ),
        "funding_zscore": funding_z,
        "funding_oi_quality": (
            -funding_z * oi_change if funding_z is not None and oi_change is not None else None
        ),
        "price_oi_state": (
            return_20d * oi_change if return_20d is not None and oi_change is not None else None
        ),
        "basis_zscore": basis_z,
        "book_imbalance": (imbalance if imbalance is not None and imbalance.is_finite() else None),
    }


def factor_data_available(definition: FactorDefinition, bars: Iterable[FactorBar]) -> bool:
    """Return whether any point-in-time row contains all required sources."""

    return any(
        all(definition_data_present(item, source) for source in definition.required_data)
        for item in bars
    )


def definition_data_present(bar: FactorBar, source: str) -> bool:
    if source == "price":
        return bar.close.is_finite()
    if source == "volume":
        return bar.volume.is_finite()
    if source == "funding":
        return _finite(bar.funding_rate)
    if source == "open_interest":
        return _finite(bar.open_interest)
    if source == "basis":
        return _finite(bar.basis_pct)
    if source == "order_book":
        return _finite(bar.book_imbalance)
    return False


def factor_definitions_payload() -> list[dict[str, object]]:
    return [
        {
            "key": item.key,
            "label": item.label,
            "category": item.category,
            "description": item.description,
            "required_data": list(item.required_data),
        }
        for item in FACTOR_DEFINITIONS
    ]


def normalize_factor_values(
    values: Mapping[str, FactorValue],
) -> dict[str, Decimal | None]:
    """Strip non-finite values before report construction."""

    return {
        key: value if value is not None and value.is_finite() else None
        for key, value in values.items()
    }
