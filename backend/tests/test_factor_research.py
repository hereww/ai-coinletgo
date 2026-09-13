from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from tests.factories import snapshot
from trading_system.domain.models import Candle
from trading_system.exchange.binance_historical import BinanceHistoricalDataClient
from trading_system.persistence.repository import Repository
from trading_system.strategy.factor_policy import (
    build_factor_overlays,
    build_window_payload,
    deterministic_percentiles,
    mature_window_payload,
    promotion_metrics,
    rebalance_window,
)
from trading_system.strategy.factor_research import (
    align_funding_point_in_time,
    benjamini_hochberg,
    build_factor_shadow_ranking,
    cross_sectional_zscores,
    run_factor_research,
    spearman_rank_ic,
)
from trading_system.strategy.factor_service import FactorResearchService
from trading_system.strategy.factors import FactorBar, compute_factor_values


@pytest.mark.asyncio
async def test_factor_research_service_fails_closed_when_disabled() -> None:
    class RejectingRepository:
        async def create_factor_research_run(self, parameters: dict[str, object]) -> str:
            raise AssertionError(f"repository should not be called: {parameters}")

    service = FactorResearchService(
        cast(Repository, RejectingRepository()),
        cast(BinanceHistoricalDataClient, object()),
        "Asia/Shanghai",
    )

    with pytest.raises(RuntimeError, match="历史研究已暂停"):
        await service.create({"symbols": ["BTCUSDT"]})


def factor_bar(
    timestamp: datetime,
    price: Decimal,
    *,
    funding_rate: Decimal | None = None,
    funding_observed_at: datetime | None = None,
    open_interest: Decimal | None = None,
    basis_pct: Decimal | None = None,
    book_imbalance: Decimal | None = None,
) -> FactorBar:
    return FactorBar(
        timestamp=timestamp,
        open=price,
        high=price * Decimal("1.01"),
        low=price * Decimal("0.99"),
        close=price,
        volume=Decimal("100"),
        funding_rate=funding_rate,
        funding_observed_at=funding_observed_at,
        open_interest=open_interest,
        basis_pct=basis_pct,
        book_imbalance=book_imbalance,
    )


def candle(timestamp: datetime, price: Decimal) -> Candle:
    return Candle(
        open_time=timestamp - timedelta(hours=1),
        close_time=timestamp,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("10"),
    )


def test_price_factors_require_complete_lookback_and_do_not_read_future() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    bars = [factor_bar(start + timedelta(days=index), Decimal(100 + index)) for index in range(26)]

    incomplete = compute_factor_values(bars[:20], bars_per_day=1, bars_per_year=365)
    assert incomplete["momentum_5d"] == Decimal("119") / Decimal("114") - 1
    assert incomplete["momentum_decay_5d_minus_20d"] is None
    assert incomplete["distance_20d_high"] is not None
    assert incomplete["realized_volatility_20d"] is None

    before = compute_factor_values(bars[:21], bars_per_day=1, bars_per_year=365)
    changed_future = [*bars[:21], factor_bar(bars[21].timestamp, Decimal("999999"))]
    after = compute_factor_values(changed_future[:-1], bars_per_day=1, bars_per_year=365)
    assert before == after
    assert before["momentum_5d"] == Decimal("120") / Decimal("115") - 1


def test_cross_sectional_statistics_have_expected_properties() -> None:
    zscores = cross_sectional_zscores(
        {"A": Decimal("1"), "B": Decimal("2"), "C": Decimal("3")},
        Decimal("0.05"),
    )
    assert sum(zscores.values()) == pytest.approx(0, abs=1e-12)
    assert spearman_rank_ic(
        zscores,
        {"A": Decimal("0.01"), "B": Decimal("0.02"), "C": Decimal("0.03")},
    ) == pytest.approx(1)
    assert spearman_rank_ic(
        zscores,
        {"A": Decimal("0.03"), "B": Decimal("0.02"), "C": Decimal("0.01")},
    ) == pytest.approx(-1)


def test_benjamini_hochberg_adjustment_is_monotone() -> None:
    adjusted = benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adjusted == pytest.approx({"a": 0.03, "b": 0.04, "c": 0.04})


def test_funding_alignment_never_backfills_from_the_future() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = [candle(start + timedelta(hours=index), Decimal("100")) for index in range(5)]
    rates = {
        start + timedelta(hours=2): Decimal("0.001"),
        start + timedelta(hours=4): Decimal("0.002"),
    }

    aligned = align_funding_point_in_time(candles, rates)

    assert aligned[0].funding_rate is None
    assert aligned[1].funding_rate is None
    assert aligned[2].funding_rate == Decimal("0.001")
    assert aligned[3].funding_rate == Decimal("0.001")
    assert aligned[4].funding_rate == Decimal("0.002")
    assert aligned[3].funding_observed_at == start + timedelta(hours=2)


def test_research_passes_predictive_factor_and_marks_missing_histories_unavailable() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    symbols = {"AAAUSDT": 1, "BBBUSDT": 2, "CCCUSDT": 3, "DDDUSDT": 4}
    markets: dict[str, list[FactorBar]] = {}
    for symbol, growth_bps in symbols.items():
        growth = Decimal("1") + Decimal(growth_bps) / Decimal("1000")
        price = Decimal("100")
        rows: list[FactorBar] = []
        for index in range(70):
            price *= growth
            rows.append(factor_bar(start + timedelta(days=index), price))
        markets[symbol] = rows

    report = run_factor_research(
        markets,
        evaluation_start=start + timedelta(days=21),
        evaluation_end=start + timedelta(days=65),
        bars_per_day=1,
        bars_per_year=365,
        forward_bars=1,
        rebalance_bars=1,
        min_cross_section=3,
        winsorize_quantile=Decimal("0.05"),
    )
    factors = {item["key"]: item for item in report["factors"]}

    assert factors["momentum_5d"]["status"] == "PASSED"
    assert factors["momentum_5d"]["mean_ic"] == pytest.approx(1)
    assert factors["momentum_5d"]["in_sample_ic"] == pytest.approx(1)
    assert factors["momentum_5d"]["out_of_sample_ic"] == pytest.approx(1)
    assert factors["momentum_5d"]["q_value"] == pytest.approx(0)
    assert factors["price_oi_state"]["status"] == "UNAVAILABLE"
    assert factors["price_oi_state"]["mean_ic"] is None
    assert factors["basis_zscore"]["status"] == "UNAVAILABLE"
    assert factors["book_imbalance"]["status"] == "UNAVAILABLE"
    assert report["methodology"]["validation"] == "rolling_walk_forward_expanding_train_test"
    assert "gross_return" in factors["momentum_5d"]["portfolio"]
    assert "net_return" in factors["momentum_5d"]["portfolio"]
    assert "portfolio_maker" in factors["momentum_5d"]


def test_shadow_ranking_uses_only_passed_factors_and_respects_direction() -> None:
    timestamp = datetime(2026, 9, 5, tzinfo=UTC)
    snapshots = [
        snapshot(symbol="AAAUSDT", timestamp=timestamp, factor_values={"reversal": Decimal("1")}),
        snapshot(symbol="BBBUSDT", timestamp=timestamp, factor_values={"reversal": Decimal("2")}),
        snapshot(symbol="CCCUSDT", timestamp=timestamp, factor_values={"reversal": Decimal("3")}),
    ]
    research_run = {
        "id": "run-1",
        "report": {
            "parameters": {"winsorize_quantile": "0.05"},
            "factors": [
                {"key": "reversal", "label": "反转", "direction": "NEGATIVE", "status": "PASSED"},
                {"key": "watch", "label": "观察因子", "direction": "POSITIVE", "status": "WATCH"},
                {
                    "key": "missing",
                    "label": "缺失因子",
                    "direction": "POSITIVE",
                    "status": "UNAVAILABLE",
                },
            ],
        },
    }

    shadow = build_factor_shadow_ranking(snapshots, research_run)

    assert shadow is not None
    assert shadow["research_run_id"] == "run-1"
    assert shadow["execution_effect"] == "shadow_only; does_not_change_candidates_or_orders"
    assert shadow["selected_factors"] == [
        {"key": "reversal", "label": "反转", "direction": "NEGATIVE"}
    ]
    assert shadow["rankings"][0]["symbol"] == "AAAUSDT"
    assert shadow["rankings"][0]["factor_coverage"] == pytest.approx(1)

    assert build_factor_shadow_ranking(
        snapshots,
        {"id": "run-2", "report": {"factors": [{"key": "watch", "status": "WATCH"}]}},
    ) is None


def test_factor_overlay_orients_scores_and_penalizes_partial_coverage() -> None:
    snapshots = [
        snapshot(
            symbol="AAAUSDT",
            score=Decimal("3"),
            factor_values={"momentum": Decimal("3"), "reversal": Decimal("1")},
        ),
        snapshot(
            symbol="BBBUSDT",
            score=Decimal("2"),
            factor_values={"momentum": Decimal("2"), "reversal": Decimal("2")},
        ),
        snapshot(
            symbol="CCCUSDT",
            score=Decimal("1"),
            factor_values={"momentum": Decimal("1")},
        ),
    ]
    policy = {
        "research_run_id": "run-policy",
        "parameters": {"winsorize_quantile": "0.05", "interval": "1h", "rebalance_bars": 24},
        "factors": [
            {"key": "momentum", "direction": "POSITIVE"},
            {"key": "reversal", "direction": "NEGATIVE"},
        ],
    }

    overlays, ranking = build_factor_overlays(
        snapshots,
        policy,
        status="SHADOW",
        rank_weight=Decimal("0.2"),
        minimum_risk_multiplier=Decimal("0.75"),
    )

    assert overlays["AAAUSDT"].contributions["reversal"] > 0
    assert overlays["CCCUSDT"].factor_coverage == Decimal("0.5")
    assert overlays["CCCUSDT"].risk_multiplier == Decimal("0.75")
    assert "reversal" not in overlays["CCCUSDT"].contributions
    assert [row["symbol"] for row in ranking["rankings"]] == [
        "AAAUSDT",
        "BBBUSDT",
        "CCCUSDT",
    ]
    ranking["research_parameters"] = {
        "taker_fee_rate": "0.001",
        "slippage_rate": "0.002",
    }
    window = build_window_payload(
        ranking,
        window_start=datetime(2026, 9, 6, tzinfo=UTC),
        window_end=datetime(2026, 9, 7, tzinfo=UTC),
        candidate_count=2,
    )
    assert window["cost_assumptions"] == {
        "fee_rate": "0.001",
        "slippage_rate": "0.002",
    }


def test_factor_percentiles_and_window_buckets_are_deterministic() -> None:
    percentiles, ranks = deterministic_percentiles(
        {"BBBUSDT": Decimal("1"), "AAAUSDT": Decimal("1"), "CCCUSDT": Decimal("0")}
    )
    assert ranks == {"AAAUSDT": 1, "BBBUSDT": 2, "CCCUSDT": 3}
    assert percentiles["AAAUSDT"] == Decimal("1")
    first = rebalance_window(
        datetime(2026, 9, 6, 1, 5, tzinfo=UTC),
        {"parameters": {"interval": "1h", "rebalance_bars": 24}},
    )
    second = rebalance_window(
        datetime(2026, 9, 6, 23, 55, tzinfo=UTC),
        {"parameters": {"interval": "1h", "rebalance_bars": 24}},
    )
    assert first == second


def test_shadow_window_maturity_and_promotion_gates() -> None:
    payload = {
        "window_start": datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
        "window_end": datetime(2026, 9, 6, tzinfo=UTC).isoformat(),
        "candidate_count": 1,
        "rankings": [
            {
                "symbol": "AAAUSDT",
                "mark_price": "100",
                "funding_rate": "0",
                "factor_score": "1",
                "baseline_rank": 2,
                "combined_rank": 1,
            },
            {
                "symbol": "BBBUSDT",
                "mark_price": "100",
                "funding_rate": "0",
                "factor_score": "-1",
                "baseline_rank": 1,
                "combined_rank": 2,
            },
        ],
    }
    result = mature_window_payload(
        payload,
        {"AAAUSDT": Decimal("110"), "BBBUSDT": Decimal("99")},
        fee_rate=Decimal("0"),
        slippage_rate=Decimal("0"),
    )
    assert result is not None
    assert Decimal(str(result["oriented_ic"])) > 0
    windows = [
        {"status": "MATURED", "window_start": str(index), "result": result}
        for index in range(30)
    ]
    metrics = promotion_metrics(windows, 30)
    assert metrics["eligible_for_promotion"] is True
    assert metrics["failure_reasons"] == []


def test_shadow_window_uses_frozen_research_cost_assumptions() -> None:
    payload = {
        "window_start": datetime(2026, 9, 5, tzinfo=UTC).isoformat(),
        "window_end": datetime(2026, 9, 6, tzinfo=UTC).isoformat(),
        "candidate_count": 1,
        "cost_assumptions": {"fee_rate": "0.01", "slippage_rate": "0.02"},
        "rankings": [
            {
                "symbol": "AAAUSDT",
                "mark_price": "100",
                "funding_rate": "0",
                "factor_score": "1",
                "baseline_rank": 1,
                "combined_rank": 1,
            },
            {
                "symbol": "BBBUSDT",
                "mark_price": "100",
                "funding_rate": "0",
                "factor_score": "-1",
                "baseline_rank": 2,
                "combined_rank": 2,
            },
        ],
    }

    result = mature_window_payload(
        payload,
        {"AAAUSDT": Decimal("110"), "BBBUSDT": Decimal("90")},
    )

    assert result is not None
    assert result["fee_rate"] == "0.01"
    assert result["slippage_rate"] == "0.02"
    assert result["shadow_net_return"] == "0.04"
