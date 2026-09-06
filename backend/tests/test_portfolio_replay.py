from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from tests.factories import context, snapshot
from trading_system.backtest.engine import BacktestConfig
from trading_system.backtest.service import ReplayService
from trading_system.config import Settings
from trading_system.domain.enums import (
    PortfolioPlanActionType,
    PortfolioTargetSide,
    PositionSide,
    SystemMode,
)
from trading_system.domain.models import (
    Candle,
    ExchangeFilters,
    PortfolioAllocation,
    PortfolioDecision,
    PortfolioPlanAction,
)
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.orchestration.cycle import TradingCycle
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository
from trading_system.risk.portfolio import PortfolioCompiler


@pytest.mark.asyncio
async def test_deterministic_replay_freezes_factor_policy_and_weights(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replay-factor-freeze.db'}")
    await database.create_schema()
    repository = Repository(database)
    run_id = await repository.create_factor_research_run(
        {"interval": "1h", "rebalance_bars": 24}
    )
    await repository.complete_factor_research(
        run_id,
        {
            "parameters": {
                "interval": "1h",
                "rebalance_bars": 24,
                "winsorize_quantile": "0.05",
            },
            "factors": [
                {
                    "key": "momentum_5d",
                    "label": "5日动量",
                    "direction": "POSITIVE",
                    "status": "PASSED",
                    "mean_ic": "0.08",
                }
            ],
        },
    )
    settings = Settings(
        factor_rank_weight=0.35,
        factor_min_risk_multiplier=0.70,
    )
    service = ReplayService(
        repository,
        cast(BinanceUSDMarketClient, object()),
        cast(TelegramNotifier, object()),
        settings,
    )
    parameters: dict[str, object] = {
        "mode": "deterministic",
        "symbols": ["BTCUSDT"],
        "start_date": "2025-01-01",
        "end_date": "2025-01-02",
        "factor_research_run_id": run_id,
    }
    try:
        replay_id = await service.create(parameters)
        queued = next(
            row for row in await repository.list_replays() if row["id"] == replay_id
        )
    finally:
        await database.dispose()

    frozen = cast(dict[str, object], queued["parameters"])
    policy = cast(dict[str, object], frozen["factor_policy_snapshot"])
    assert policy["research_run_id"] == run_id
    assert policy["factors"] == [
        {
            "key": "momentum_5d",
            "label": "5日动量",
            "direction": "POSITIVE",
            "mean_ic": "0.08",
            "out_of_sample_ic": None,
        }
    ]
    assert frozen["factor_rank_weight"] == "0.35"
    assert frozen["factor_minimum_risk_multiplier"] == "0.7"


@pytest.mark.asyncio
async def test_recorded_portfolio_replay_recompiles_the_saved_plan(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'portfolio-replay.db'}")
    await database.create_schema()
    repository = Repository(database)
    account_context = context()
    compile_now = datetime(2025, 1, 1, tzinfo=UTC)
    allocation = PortfolioAllocation(
        symbol="BTCUSDT",
        target_side=PortfolioTargetSide.LONG,
        allocation_fraction=Decimal("0.5"),
        priority=1,
        confidence=Decimal("0.9"),
        entry_min=Decimal("99.9"),
        entry_max=Decimal("100.1"),
        stop_price=Decimal("98"),
        target_price=Decimal("106"),
        thesis="趋势延续",
    )
    decision = PortfolioDecision(
        market_regime="TRENDING",
        portfolio_risk_budget_fraction=Decimal("1"),
        allocations=[allocation],
        summary="组合风险受控",
        created_at=compile_now,
        expires_at=compile_now + timedelta(minutes=15),
    )
    market_snapshot = snapshot(timestamp=compile_now)
    exchange_filters = ExchangeFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.1"),
        min_quantity=Decimal("0.1"),
        min_notional=Decimal("5"),
    )
    compiler = PortfolioCompiler()
    plan = compiler.compile(
        decision,
        snapshots={"BTCUSDT": market_snapshot},
        account=account_context.account,
        positions=[],
        filters={"BTCUSDT": exchange_filters},
        limits=account_context.limits,
        mode=SystemMode.TESTNET,
        now=compile_now,
    )
    replay_context = TradingCycle._portfolio_replay_context(
        candidates=[market_snapshot],
        snapshots={"BTCUSDT": market_snapshot},
        account=account_context.account,
        positions=[],
        filters={"BTCUSDT": exchange_filters},
        limits=account_context.limits,
        mode=SystemMode.TESTNET,
        correlations={},
        last_rebalance_at=None,
        cooldown_minutes=30,
        compile_now=compile_now,
    )
    await repository.save_portfolio_decision(
        decision,
        status=plan.status.value,
        prompt_version="portfolio-v1",
        model_name="test-model",
        input_hash="a" * 64,
        replay_context=replay_context,
        compiled_plan=plan,
    )
    service = ReplayService(
        repository,
        cast(BinanceUSDMarketClient, object()),
        cast(TelegramNotifier, object()),
    )
    try:
        metrics = await service._run_recorded_portfolio(
            {"portfolio_decision_id": str(decision.decision_id)}
        )
    finally:
        await database.dispose()
    summary = cast(dict[str, object], metrics["summary"])
    assert summary["compiler_plan_match"] is True
    assert summary["planned_actions"] == 1


def test_recorded_paper_outcome_uses_shared_exit_policy() -> None:
    decision_time = datetime(2025, 1, 2, tzinfo=UTC)
    rows = [
        Candle(
            open_time=decision_time - timedelta(minutes=15 * (101 - index)),
            close_time=decision_time - timedelta(minutes=15 * (100 - index)),
            open=Decimal("100"),
            high=Decimal("100.5"),
            low=Decimal("99.5"),
            close=Decimal("100"),
            volume=Decimal("1000"),
        )
        for index in range(100)
    ]
    rows.extend(
        [
            Candle(
                open_time=decision_time,
                close_time=decision_time + timedelta(minutes=15),
                open=Decimal("100"),
                high=Decimal("102"),
                low=Decimal("99"),
                close=Decimal("101.8"),
                volume=Decimal("1000"),
            ),
            Candle(
                open_time=decision_time + timedelta(minutes=15),
                close_time=decision_time + timedelta(minutes=30),
                open=Decimal("102"),
                high=Decimal("105.2"),
                low=Decimal("101"),
                close=Decimal("105"),
                volume=Decimal("1000"),
            ),
        ]
    )
    action = PortfolioPlanAction(
        symbol="BTCUSDT",
        action=PortfolioPlanActionType.OPEN,
        side=PositionSide.LONG,
        target_quantity=Decimal("1"),
        quantity_delta=Decimal("1"),
        target_risk_usdt=Decimal("1.8"),
        entry_min=Decimal("99.9"),
        entry_max=Decimal("100.1"),
        stop_price=Decimal("98.2"),
        target_price=Decimal("105"),
    )
    outcome = ReplayService._simulate_paper_action(
        action,
        rows,
        {},
        decision_time,
        BacktestConfig(
            fee_rate=Decimal("0"),
            slippage_rate=Decimal("0"),
            estimated_funding_rate=Decimal("0"),
        ),
    )
    assert outcome["tp1_price"] == "101.8"
    assert outcome["final_target_price"] == "105"
    assert outcome["tp1_hit"] is True
    assert outcome["final_target_hit"] is True


@pytest.mark.asyncio
async def test_old_portfolio_decision_without_snapshot_cannot_be_replayed(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'old-portfolio-decision.db'}")
    await database.create_schema()
    repository = Repository(database)
    decision = PortfolioDecision(
        market_regime="UNCERTAIN",
        portfolio_risk_budget_fraction=Decimal("0"),
        allocations=[],
        summary="空仓",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    try:
        await repository.save_portfolio_decision(
            decision,
            status="APPROVED",
            prompt_version="portfolio-v1",
            model_name="test-model",
            input_hash="b" * 64,
        )
        assert await repository.get_portfolio_replay_input(str(decision.decision_id)) is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_portfolio_decision_keeps_raw_ai_allocations_when_compile_has_no_action(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'portfolio-intent.db'}")
    await database.create_schema()
    repository = Repository(database)
    decision = PortfolioDecision(
        market_regime="TRENDING",
        portfolio_risk_budget_fraction=Decimal("0.4"),
        allocations=[
            PortfolioAllocation(
                symbol="BTCUSDT",
                target_side=PortfolioTargetSide.LONG,
                allocation_fraction=Decimal("0.2"),
                priority=1,
                confidence=Decimal("0.8"),
                entry_min=Decimal("99"),
                entry_max=Decimal("101"),
                stop_price=Decimal("95"),
                target_price=Decimal("110"),
                thesis="趋势延续",
            )
        ],
        summary="保留 AI 意图",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    try:
        await repository.save_portfolio_decision(
            decision,
            status="REJECTED",
            prompt_version="portfolio-v1",
            model_name="test-model",
            input_hash="c" * 64,
        )
        rows = await repository.list_portfolio_decisions()
    finally:
        await database.dispose()

    assert len(rows) == 1
    assert len(rows[0]["allocations"]) == 1
    allocation = rows[0]["allocations"][0]
    assert allocation["status"] == "MODEL_INTENT"
    assert allocation["symbol"] == "BTCUSDT"
    assert allocation["target_side"] == "LONG"
