from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from tests.factories import context, snapshot
from trading_system.backtest.service import ReplayService
from trading_system.domain.enums import PortfolioTargetSide, SystemMode
from trading_system.domain.models import ExchangeFilters, PortfolioAllocation, PortfolioDecision
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.orchestration.cycle import TradingCycle
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository
from trading_system.risk.portfolio import PortfolioCompiler


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
