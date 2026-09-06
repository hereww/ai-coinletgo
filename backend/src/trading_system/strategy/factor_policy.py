from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import mean
from typing import Any, Literal

from trading_system.domain.models import FactorOverlay, MarketSnapshot
from trading_system.strategy.factor_research import cross_sectional_zscores, spearman_rank_ic

PolicyStatus = Literal["ACTIVE", "SHADOW"]


def eligible_research_snapshot(research_run: Mapping[str, Any]) -> dict[str, object] | None:
    report = research_run.get("report")
    if not isinstance(report, Mapping):
        return None
    raw_factors = report.get("factors")
    if not isinstance(raw_factors, list):
        return None
    factors = [
        {
            "key": str(item.get("key", "")),
            "label": str(item.get("label", item.get("key", ""))),
            "direction": str(item.get("direction", "POSITIVE")),
            "mean_ic": item.get("mean_ic"),
            "out_of_sample_ic": item.get("out_of_sample_ic"),
        }
        for item in raw_factors
        if isinstance(item, Mapping)
        and item.get("status") == "PASSED"
        and str(item.get("key", ""))
    ]
    if not factors:
        return None
    parameters = report.get("parameters")
    return {
        "research_run_id": str(research_run.get("id", "")),
        "completed_at": _json_time(research_run.get("completed_at")),
        "factors": factors,
        "parameters": dict(parameters) if isinstance(parameters, Mapping) else {},
        "research_summary": dict(report.get("summary", {}))
        if isinstance(report.get("summary"), Mapping)
        else {},
        "research_methodology": dict(report.get("methodology", {}))
        if isinstance(report.get("methodology"), Mapping)
        else {},
    }


def build_factor_overlays(
    snapshots: Sequence[MarketSnapshot],
    frozen_policy: Mapping[str, Any],
    *,
    status: PolicyStatus,
    rank_weight: Decimal,
    minimum_risk_multiplier: Decimal,
) -> tuple[dict[str, FactorOverlay], dict[str, object]]:
    if not snapshots:
        raise ValueError("factor policy requires a non-empty cross-section")
    raw_factors = frozen_policy.get("factors")
    if not isinstance(raw_factors, list) or not raw_factors:
        raise ValueError("factor policy has no selected factors")
    research_run_id = str(frozen_policy.get("research_run_id", ""))
    if not research_run_id:
        raise ValueError("factor policy research_run_id is missing")
    parameters = frozen_policy.get("parameters")
    winsorize = Decimal("0.05")
    if isinstance(parameters, Mapping):
        winsorize = Decimal(str(parameters.get("winsorize_quantile", winsorize)))

    baseline_scores = {item.symbol: item.score for item in snapshots}
    baseline_percentiles, baseline_ranks = deterministic_percentiles(baseline_scores)
    factor_contributions: dict[str, dict[str, Decimal]] = {
        item.symbol: {} for item in snapshots
    }
    selected_factors: list[dict[str, object]] = []
    for raw_factor in raw_factors:
        if not isinstance(raw_factor, Mapping):
            continue
        key = str(raw_factor.get("key", ""))
        if not key:
            continue
        selected_factors.append(dict(raw_factor))
        values = {
            item.symbol: item.factor_values[key]
            for item in snapshots
            if key in item.factor_values and item.factor_values[key].is_finite()
        }
        scores = cross_sectional_zscores(values, winsorize)
        if not scores:
            raise ValueError(f"invalid factor cross-section:{key}")
        direction = -1 if str(raw_factor.get("direction", "POSITIVE")) == "NEGATIVE" else 1
        for symbol, score in scores.items():
            factor_contributions[symbol][key] = Decimal(str(score * direction))
    if not selected_factors:
        raise ValueError("factor policy has no valid factor definitions")

    factor_scores = {
        symbol: sum(contributions.values(), Decimal("0"))
        / Decimal(len(selected_factors))
        for symbol, contributions in factor_contributions.items()
    }
    factor_percentiles, factor_ranks = deterministic_percentiles(factor_scores)
    combined_scores = {
        symbol: (Decimal("1") - rank_weight) * baseline_percentiles[symbol]
        + rank_weight * factor_percentiles[symbol]
        for symbol in baseline_scores
    }
    combined_order = sorted(
        baseline_scores,
        key=lambda symbol: (
            -combined_scores[symbol],
            -baseline_scores[symbol],
            symbol,
        ),
    )
    combined_ranks = {symbol: index for index, symbol in enumerate(combined_order, start=1)}
    overlays: dict[str, FactorOverlay] = {}
    rows: list[dict[str, object]] = []
    for snapshot in snapshots:
        symbol = snapshot.symbol
        present = len(factor_contributions[symbol])
        coverage = Decimal(present) / Decimal(len(selected_factors))
        risk_multiplier = (
            minimum_risk_multiplier
            + (Decimal("1") - minimum_risk_multiplier) * factor_percentiles[symbol]
            if coverage == Decimal("1")
            else minimum_risk_multiplier
        )
        overlay = FactorOverlay(
            research_run_id=research_run_id,
            policy_status=status,
            baseline_score=baseline_scores[symbol],
            baseline_percentile=baseline_percentiles[symbol],
            factor_score=factor_scores[symbol],
            factor_percentile=factor_percentiles[symbol],
            combined_score=combined_scores[symbol],
            factor_coverage=coverage,
            contributions=factor_contributions[symbol],
            risk_multiplier=min(Decimal("1"), risk_multiplier),
            baseline_rank=baseline_ranks[symbol],
            factor_rank=factor_ranks[symbol],
            combined_rank=combined_ranks[symbol],
            rank_change=baseline_ranks[symbol] - combined_ranks[symbol],
        )
        overlays[symbol] = overlay
        rows.append(
            {
                "symbol": symbol,
                "baseline_score": str(overlay.baseline_score),
                "baseline_percentile": str(overlay.baseline_percentile),
                "factor_score": str(overlay.factor_score),
                "factor_percentile": str(overlay.factor_percentile),
                "combined_score": str(overlay.combined_score),
                "factor_coverage": str(overlay.factor_coverage),
                "contributions": {
                    key: str(value) for key, value in overlay.contributions.items()
                },
                "risk_multiplier": str(overlay.risk_multiplier),
                "baseline_rank": overlay.baseline_rank,
                "factor_rank": overlay.factor_rank,
                "combined_rank": overlay.combined_rank,
                "rank_change": overlay.rank_change,
                "mark_price": str(snapshot.mark_price),
                "funding_rate": str(snapshot.funding_rate),
            }
        )
    rows.sort(key=lambda row: (int(str(row["combined_rank"])), str(row["symbol"])))
    return overlays, {
        "research_run_id": research_run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "policy_status": status,
        "selected_factors": selected_factors,
        # The shadow evaluation must keep the transaction-cost assumptions
        # that were used to qualify this research run.  Later runtime setting
        # changes must not rewrite a candidate's promotion evidence.
        "research_parameters": dict(parameters) if isinstance(parameters, Mapping) else {},
        "rank_weight": str(rank_weight),
        "minimum_risk_multiplier": str(minimum_risk_multiplier),
        "rankings": rows,
        "execution_effect": (
            "testnet_candidate_ranking_and_risk_scaling"
            if status == "ACTIVE"
            else "shadow_only"
        ),
    }


def deterministic_percentiles(
    values: Mapping[str, Decimal],
) -> tuple[dict[str, Decimal], dict[str, int]]:
    if not values:
        raise ValueError("percentiles require values")
    ordered = sorted(values, key=lambda symbol: (-values[symbol], symbol))
    ranks = {symbol: index for index, symbol in enumerate(ordered, start=1)}
    if len(ordered) == 1:
        return {ordered[0]: Decimal("0.5")}, ranks
    denominator = Decimal(len(ordered) - 1)
    percentiles = {
        symbol: Decimal(len(ordered) - rank) / denominator
        for symbol, rank in ranks.items()
    }
    return percentiles, ranks


def rebalance_window(
    now: datetime, frozen_policy: Mapping[str, Any]
) -> tuple[datetime, datetime]:
    parameters = frozen_policy.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("factor policy parameters are missing")
    interval = str(parameters.get("interval", "1h"))
    hours_per_bar = {"1h": 1, "4h": 4}.get(interval)
    if hours_per_bar is None:
        raise ValueError("unsupported factor interval")
    rebalance_bars = int(str(parameters.get("rebalance_bars", 24)))
    duration = timedelta(hours=hours_per_bar * rebalance_bars)
    if duration <= timedelta(0):
        raise ValueError("factor rebalance duration must be positive")
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    aware = aware.astimezone(UTC)
    seconds = int(duration.total_seconds())
    bucket = int(aware.timestamp()) // seconds * seconds
    start = datetime.fromtimestamp(bucket, UTC)
    return start, start + duration


def build_window_payload(
    ranking: Mapping[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
    candidate_count: int,
) -> dict[str, object]:
    rows = ranking.get("rankings")
    if not isinstance(rows, list):
        raise ValueError("factor ranking rows are missing")
    return {
        "research_run_id": str(ranking.get("research_run_id", "")),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "candidate_count": max(1, candidate_count),
        "rank_weight": ranking.get("rank_weight"),
        "selected_factors": ranking.get("selected_factors", []),
        "cost_assumptions": _frozen_cost_assumptions(ranking),
        "rankings": rows,
    }


def mature_window_payload(
    payload: Mapping[str, Any],
    current_prices: Mapping[str, Decimal],
    *,
    fee_rate: Decimal | None = None,
    slippage_rate: Decimal | None = None,
) -> dict[str, object] | None:
    frozen_costs = payload.get("cost_assumptions")
    if fee_rate is None:
        fee_rate = _cost_assumption(
            frozen_costs, "fee_rate", default=Decimal("0.0005")
        )
    if slippage_rate is None:
        slippage_rate = _cost_assumption(
            frozen_costs, "slippage_rate", default=Decimal("0.0005")
        )
    raw_rows = payload.get("rankings")
    if not isinstance(raw_rows, list):
        return None
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol", ""))
        current = current_prices.get(symbol)
        if current is None or current <= 0:
            continue
        start = Decimal(str(raw.get("mark_price", "0")))
        if start <= 0:
            continue
        rows.append(
            {
                **dict(raw),
                "forward_return": current / start - Decimal("1"),
            }
        )
    if len(rows) < 2:
        return None
    factor_scores = {str(row["symbol"]): float(Decimal(str(row["factor_score"]))) for row in rows}
    returns = {str(row["symbol"]): Decimal(str(row["forward_return"])) for row in rows}
    ic = spearman_rank_ic(factor_scores, returns)
    candidate_count = min(int(str(payload.get("candidate_count", 1))), len(rows))
    baseline = sorted(rows, key=lambda row: (int(row["baseline_rank"]), str(row["symbol"])))
    shadow = sorted(rows, key=lambda row: (int(row["combined_rank"]), str(row["symbol"])))
    baseline_selection = baseline[:candidate_count]
    shadow_selection = shadow[:candidate_count]
    baseline_gross = _average_return(baseline_selection)
    shadow_gross = _average_return(shadow_selection)
    baseline_funding = _average_funding(baseline_selection, payload)
    shadow_funding = _average_funding(shadow_selection, payload)
    round_trip_cost = Decimal("2") * (fee_rate + slippage_rate)
    baseline_net = baseline_gross - round_trip_cost - baseline_funding
    shadow_net = shadow_gross - round_trip_cost - shadow_funding
    baseline_symbols = {str(row["symbol"]) for row in baseline_selection}
    shadow_symbols = {str(row["symbol"]) for row in shadow_selection}
    turnover = Decimal(len(baseline_symbols.symmetric_difference(shadow_symbols))) / Decimal(
        max(1, candidate_count * 2)
    )
    return {
        "matured_at": datetime.now(UTC).isoformat(),
        "observations": len(rows),
        "oriented_ic": str(ic) if ic is not None and math.isfinite(ic) else None,
        "baseline_gross_return": str(baseline_gross),
        "baseline_net_return": str(baseline_net),
        "shadow_gross_return": str(shadow_gross),
        "shadow_net_return": str(shadow_net),
        "baseline_funding_cost": str(baseline_funding),
        "shadow_funding_cost": str(shadow_funding),
        "fee_rate": str(fee_rate),
        "slippage_rate": str(slippage_rate),
        "candidate_turnover": str(turnover),
        "baseline_symbols": sorted(baseline_symbols),
        "shadow_symbols": sorted(shadow_symbols),
        "forward_returns": {symbol: str(value) for symbol, value in returns.items()},
    }


def _frozen_cost_assumptions(ranking: Mapping[str, Any]) -> dict[str, str]:
    parameters = ranking.get("research_parameters")
    return {
        "fee_rate": str(
            _cost_assumption(parameters, "taker_fee_rate", default=Decimal("0.0005"))
        ),
        "slippage_rate": str(
            _cost_assumption(parameters, "slippage_rate", default=Decimal("0.0005"))
        ),
    }


def _cost_assumption(
    source: object, key: str, *, default: Decimal
) -> Decimal:
    if not isinstance(source, Mapping):
        return default
    try:
        value = Decimal(str(source.get(key, default)))
    except (ArithmeticError, ValueError):
        return default
    return value if value.is_finite() and value >= 0 else default


def promotion_metrics(
    windows: Sequence[Mapping[str, Any]], required_windows: int
) -> dict[str, object]:
    matured = [item for item in windows if item.get("status") == "MATURED"]
    matured.sort(key=lambda item: str(item.get("window_start", "")))
    ic_values: list[Decimal] = []
    shadow_returns: list[Decimal] = []
    baseline_returns: list[Decimal] = []
    turnovers: list[Decimal] = []
    for window in matured:
        result = window.get("result")
        if not isinstance(result, Mapping):
            continue
        if result.get("oriented_ic") is not None:
            ic_values.append(Decimal(str(result["oriented_ic"])))
        shadow_returns.append(Decimal(str(result.get("shadow_net_return", "0"))))
        baseline_returns.append(Decimal(str(result.get("baseline_net_return", "0"))))
        turnovers.append(Decimal(str(result.get("candidate_turnover", "0"))))
    shadow_net, shadow_drawdown = _equity_metrics(shadow_returns)
    baseline_net, baseline_drawdown = _equity_metrics(baseline_returns)
    mean_ic = sum(ic_values, Decimal("0")) / Decimal(len(ic_values)) if ic_values else None
    gates = {
        "minimum_windows": len(matured) >= required_windows,
        "positive_oriented_mean_ic": mean_ic is not None and mean_ic > 0,
        "positive_after_cost_return": shadow_net > 0,
        "drawdown_not_worse_than_baseline": shadow_drawdown <= baseline_drawdown,
    }
    reasons = [key for key, passed in gates.items() if not passed]
    return {
        "matured_windows": len(matured),
        "required_windows": required_windows,
        "progress": str(min(Decimal("1"), Decimal(len(matured)) / Decimal(required_windows))),
        "oriented_mean_ic": str(mean_ic) if mean_ic is not None else None,
        "shadow_net_return": str(shadow_net),
        "baseline_net_return": str(baseline_net),
        "shadow_max_drawdown": str(shadow_drawdown),
        "baseline_max_drawdown": str(baseline_drawdown),
        "candidate_turnover": str(mean(turnovers)) if turnovers else "0",
        "gates": gates,
        "failure_reasons": reasons,
        "eligible_for_promotion": all(gates.values()),
    }


def _average_return(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    if not rows:
        return Decimal("0")
    return sum((Decimal(str(row["forward_return"])) for row in rows), Decimal("0")) / Decimal(
        len(rows)
    )


def _average_funding(
    rows: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
) -> Decimal:
    if not rows:
        return Decimal("0")
    start = datetime.fromisoformat(str(payload["window_start"]))
    end = datetime.fromisoformat(str(payload["window_end"]))
    settlements = Decimal(str((end - start).total_seconds())) / Decimal("28800")
    return sum(
        (Decimal(str(row.get("funding_rate", "0"))) * settlements for row in rows),
        Decimal("0"),
    ) / Decimal(len(rows))


def _equity_metrics(returns: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    equity = Decimal("1")
    peak = equity
    drawdown = Decimal("0")
    for value in returns:
        equity *= Decimal("1") + value
        peak = max(peak, equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - equity) / peak)
    return equity - Decimal("1"), drawdown


def _json_time(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else None
