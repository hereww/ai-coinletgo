from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_system.backtest.engine import BacktestConfig, BacktestEngine, aggregate_candles
from trading_system.backtest.portfolio import PortfolioBacktestEngine, SymbolLedger
from trading_system.domain.enums import PositionSide
from trading_system.domain.models import Candle, ExchangeFilters, MarketSnapshot


def candles(count: int = 300) -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    price = Decimal("100")
    for index in range(count):
        close = price + Decimal(index) * Decimal("0.1")
        rows.append(
            Candle(
                open_time=start + timedelta(minutes=index * 15),
                close_time=start + timedelta(minutes=(index + 1) * 15) - timedelta(milliseconds=1),
                open=close - Decimal("0.02"),
                high=close + Decimal("0.2"),
                low=close - Decimal("0.2"),
                close=close,
                volume=Decimal("1000") + index,
            )
        )
    return rows


class RecordingEngine(BacktestEngine):
    seen_lengths: list[int]

    def __init__(self) -> None:
        self.seen_lengths = []

    def _signal(self, history: list[Candle]) -> PositionSide | None:
        self.seen_lengths.append(len(history))
        return None


def test_replay_signal_generation_has_no_future_candles() -> None:
    engine = RecordingEngine()
    engine.run(candles())
    assert engine.seen_lengths[0] == 221
    assert engine.seen_lengths[-1] == 300
    assert engine.seen_lengths == list(range(221, 301))


def test_entry_and_final_close_fees_are_counted_once() -> None:
    engine = BacktestEngine()
    config = BacktestConfig(slippage_rate=Decimal("0"), fee_rate=Decimal("0.001"))
    open_position = engine._open(
        PositionSide.LONG, Decimal("100"), Decimal("1"), Decimal("1000"), config
    )
    entry_fee = open_position.fees
    close_cash_flow = engine._close_remaining(open_position, Decimal("100"), config)
    assert entry_fee > 0
    assert open_position.fees == entry_fee * 2
    assert close_cash_flow == -entry_fee
    assert open_position.remaining == 0


def test_manual_atr_exit_levels_use_shared_40_40_20_geometry() -> None:
    engine = BacktestEngine()
    config = BacktestConfig(
        slippage_rate=Decimal("0"),
        fee_rate=Decimal("0"),
        manual_exit_levels_enabled=True,
        manual_stop_atr=Decimal("1.8"),
        manual_take_profit_atr=Decimal("5"),
    )
    open_position = engine._open(
        PositionSide.LONG, Decimal("100"), Decimal("1"), Decimal("1000"), config
    )
    assert open_position.entry == Decimal("100")
    assert open_position.stop == Decimal("98.2")
    assert open_position.tp1 == Decimal("101.8")
    assert open_position.tp2 == Decimal("105")


def test_tp1_tp2_and_trailing_manage_remaining_twenty_percent() -> None:
    engine = BacktestEngine()
    config = BacktestConfig(slippage_rate=Decimal("0"), fee_rate=Decimal("0"))
    open_position = engine._open(
        PositionSide.LONG, Decimal("100"), Decimal("1"), Decimal("1000"), config
    )
    trigger = Candle(
        open_time=datetime(2025, 1, 1, tzinfo=UTC),
        close_time=datetime(2025, 1, 1, 0, 15, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("104"),
        low=Decimal("100"),
        close=Decimal("103"),
        volume=Decimal("1"),
    )
    _, closed, _ = engine._manage(open_position, trigger, Decimal("1"), config)
    assert closed is False
    assert open_position.tp1_hit and open_position.tp2_hit
    assert abs(open_position.remaining - open_position.quantity * Decimal("0.2")) < Decimal("1e-25")
    assert open_position.stop == Decimal("102.5")


def test_aggregate_uses_only_complete_bars() -> None:
    source = candles(9)
    result = aggregate_candles(source, 4)
    assert len(result) == 2
    assert result[-1].close == source[7].close


class ForcedPortfolioEngine(PortfolioBacktestEngine):
    @staticmethod
    def _signal(history: list[Candle]) -> PositionSide | None:
        return PositionSide.LONG

    @staticmethod
    def _portfolio_snapshot(
        symbol: str, history: list[Candle], config: BacktestConfig
    ) -> MarketSnapshot:
        return PortfolioBacktestEngine._portfolio_snapshot(symbol, history, config).model_copy(
            update={
                "market_regime": "TRENDING",
                "volatility_risk_multiplier": Decimal("1"),
                "adx_1h": Decimal("30"),
                "trend_1h": 1,
                "trend_4h": 1,
                "breakout_15m": 1,
            }
        )

    def _rank_candidates(
        self,
        snapshots: list[MarketSnapshot],
        ledgers: dict[str, SymbolLedger],
        limit: int,
    ) -> list[MarketSnapshot]:
        del ledgers
        for index, snapshot in enumerate(snapshots):
            snapshot.score = Decimal(len(snapshots) - index)
        return snapshots[:limit]


def flat_candles(count: int = 2883) -> list[Candle]:
    start = datetime(2024, 12, 1, tzinfo=UTC)
    return [
        Candle(
            open_time=start + timedelta(minutes=index * 15),
            close_time=(
                start
                + timedelta(minutes=(index + 1) * 15)
                - timedelta(milliseconds=1)
            ),
            open=Decimal("100"),
            high=Decimal("101.25"),
            low=Decimal("98.75"),
            close=Decimal("100"),
            volume=Decimal("1000"),
        )
        for index in range(count)
    ]


def drifting_candles(step: str, count: int = 2883) -> list[Candle]:
    start = datetime(2024, 12, 1, tzinfo=UTC)
    drift = Decimal(step)
    return [
        Candle(
            open_time=start + timedelta(minutes=index * 15),
            close_time=start + timedelta(minutes=(index + 1) * 15)
            - timedelta(milliseconds=1),
            open=Decimal("100") + drift * index,
            high=Decimal("101.25") + drift * index,
            low=Decimal("98.75") + drift * index,
            close=Decimal("100") + drift * index,
            volume=Decimal("1000"),
        )
        for index in range(count)
    ]


def replay_filters(**updates: str) -> ExchangeFilters:
    values = {
        "tick_size": Decimal("0.1"),
        "step_size": Decimal("1"),
        "min_quantity": Decimal("1"),
        "min_notional": Decimal("5"),
    }
    values.update({key: Decimal(value) for key, value in updates.items()})
    return ExchangeFilters.model_validate(values)


def portfolio_config(**updates: object) -> BacktestConfig:
    values: dict[str, object] = {
        "fee_rate": Decimal("0"),
        "slippage_rate": Decimal("0"),
        "estimated_funding_rate": Decimal("0"),
        "stop_atr": Decimal("1"),
    }
    values.update(updates)
    return BacktestConfig(**values)


def run_portfolio(
    symbols: list[str],
    *,
    config: BacktestConfig | None = None,
    exchange_filters: ExchangeFilters | None = None,
    rows: list[Candle] | None = None,
):
    rows = rows or flat_candles()
    return ForcedPortfolioEngine().run_portfolio(
        {symbol: rows for symbol in symbols},
        {symbol: exchange_filters or replay_filters() for symbol in symbols},
        config or portfolio_config(),
        evaluation_start=rows[-3].open_time,
    )


def test_portfolio_replay_rejects_highly_correlated_second_symbol() -> None:
    result = run_portfolio(
        ["AAAUSDT", "BBBUSDT"], rows=drifting_candles("0.01")
    )
    assert result.trades == 1
    assert result.signal_rejections["correlated_with_AAAUSDT"] >= 1
    assert result.symbol_results["AAAUSDT"]["trades"] == 1


def test_portfolio_replay_enforces_shared_risk_and_direction_limits() -> None:
    risk_limited = run_portfolio(
        ["AAAUSDT", "BBBUSDT"],
        config=portfolio_config(
            portfolio_risk_pct=Decimal("0.0025"), correlation_limit=Decimal("1")
        ),
        rows=drifting_candles("0.01"),
    )
    assert risk_limited.signal_rejections["portfolio_risk_capacity_exhausted"] >= 1

    direction_limited = run_portfolio(
        ["AAAUSDT", "BBBUSDT"],
        config=portfolio_config(max_same_direction=1, correlation_limit=Decimal("1")),
        rows=drifting_candles("0.01"),
    )
    assert direction_limited.signal_rejections["same_direction_limit_reached"] >= 1


def test_portfolio_replay_applies_exchange_quantity_filters() -> None:
    result = run_portfolio(
        ["AAAUSDT"], exchange_filters=replay_filters(min_quantity="10")
    )
    assert result.trades == 0
    assert result.signal_rejections["quantity_below_exchange_minimum"] >= 1
    filters = result.symbol_results["AAAUSDT"]["exchange_filters"]
    assert filters["min_quantity"] == "10"


def test_portfolio_replay_applies_point_in_time_factor_risk_scaling() -> None:
    markets = {
        "AAAUSDT": drifting_candles("0.01"),
        "BBBUSDT": drifting_candles("0.002"),
        "CCCUSDT": drifting_candles("-0.005"),
    }
    filters = {symbol: replay_filters(step_size="0.01") for symbol in markets}
    policy: dict[str, object] = {
        "research_run_id": "0d5f81b1-4a64-4ac9-8d72-e1c4a2f70162",
        "factors": [
            {
                "key": "momentum_5d",
                "label": "5日动量",
                "direction": "POSITIVE",
            }
        ],
        "parameters": {
            "interval": "1h",
            "rebalance_bars": 24,
            "winsorize_quantile": "0.05",
        },
    }
    config = portfolio_config(
        candidate_count=3,
        max_positions=3,
        max_same_direction=3,
        correlation_limit=Decimal("1"),
    )
    evaluation_start = markets["AAAUSDT"][-3].open_time
    baseline = ForcedPortfolioEngine().run_portfolio(
        markets,
        filters,
        config,
        evaluation_start=evaluation_start,
        factor_policy=policy,
        factor_enabled=False,
    )
    factored = ForcedPortfolioEngine().run_portfolio(
        markets,
        filters,
        config,
        evaluation_start=evaluation_start,
        factor_policy=policy,
        factor_enabled=True,
    )
    assert baseline.factor_risk_clippings == 0
    assert factored.factor_fallbacks == 0
    assert factored.factor_risk_clippings >= 1


def test_portfolio_funding_cost_respects_position_direction() -> None:
    rows = flat_candles()
    settlement_candle = next(
        row
        for row in rows
        if (row.close_time + timedelta(milliseconds=1)).hour == 8
        and (row.close_time + timedelta(milliseconds=1)).minute == 0
    )
    engine = BacktestEngine()
    config = portfolio_config(estimated_funding_rate=Decimal("0.001"))
    long_position = engine._open(
        PositionSide.LONG, Decimal("100"), Decimal("1"), Decimal("1000"), config
    )
    short_position = engine._open(
        PositionSide.SHORT, Decimal("100"), Decimal("1"), Decimal("1000"), config
    )
    long_cost = PortfolioBacktestEngine._funding_cost(
        long_position, settlement_candle, config, None
    )
    short_cost = PortfolioBacktestEngine._funding_cost(
        short_position, settlement_candle, config, None
    )
    assert long_cost > 0
    assert short_cost == -long_cost


def test_portfolio_snapshot_uses_latest_historical_funding_rate() -> None:
    rows = flat_candles()
    config = portfolio_config(estimated_funding_rate=Decimal("0.0001"))
    snapshot = PortfolioBacktestEngine._portfolio_snapshot(
        "BTCUSDT",
        rows,
        config,
        funding_rate=Decimal("-0.0007"),
    )
    assert snapshot.funding_rate == Decimal("-0.0007")
    assert PortfolioBacktestEngine._funding_rate_at(
        {
            rows[-10].close_time: Decimal("0.0002"),
            rows[-1].close_time: Decimal("-0.0007"),
        },
        rows[-1].close_time,
        Decimal("0.0001"),
    ) == Decimal("-0.0007")
