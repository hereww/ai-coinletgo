from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.factories import position, signal, snapshot
from trading_system.config import Settings
from trading_system.domain.enums import DecisionStatus, OrderStatus, PositionSide
from trading_system.domain.models import OrderState, RiskDecision
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository


def income_row(
    income_id: str,
    income_type: str,
    income: str,
    event_time: datetime,
    *,
    symbol: str = "BTCUSDT",
    trade_id: str | None = None,
) -> dict[str, object]:
    return {
        "income_id": income_id,
        "symbol": symbol,
        "income_type": income_type,
        "income": income,
        "asset": "USDT",
        "trade_id": trade_id,
        "event_time": event_time,
        "payload": {},
    }


@pytest.mark.asyncio
async def test_income_ledger_groups_pnl_by_shanghai_day_and_trade_id(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'income-ledger.db'}")
    await database.create_schema()
    repository = Repository(database, "Asia/Shanghai")
    rows = [
        # 2026-09-03 23:59:59 in Shanghai: outside the requested range.
        income_row(
            "outside-range",
            "REALIZED_PNL",
            "99",
            datetime(2026, 9, 3, 15, 59, 59, tzinfo=UTC),
            trade_id="ignored",
        ),
        # Exactly midnight in Shanghai on 2026-09-04.
        income_row(
            "realized-1",
            "REALIZED_PNL",
            "5",
            datetime(2026, 9, 3, 16, 0, tzinfo=UTC),
            trade_id="1001",
        ),
        income_row(
            "commission-1",
            "COMMISSION",
            "-0.5",
            datetime(2026, 9, 3, 16, 1, tzinfo=UTC),
            trade_id="1001",
        ),
        income_row(
            "funding-1",
            "FUNDING_FEE",
            "-0.25",
            datetime(2026, 9, 3, 20, 0, tzinfo=UTC),
        ),
        # Exactly midnight in Shanghai on 2026-09-05.
        income_row(
            "realized-2",
            "REALIZED_PNL",
            "-3",
            datetime(2026, 9, 4, 16, 0, tzinfo=UTC),
            symbol="ETHUSDT",
            trade_id="2001",
        ),
        income_row(
            "commission-2",
            "COMMISSION",
            "-0.3",
            datetime(2026, 9, 4, 16, 1, tzinfo=UTC),
            symbol="ETHUSDT",
            trade_id="2001",
        ),
        # Income types outside the PnL definition remain queryable, but do not affect PnL.
        income_row(
            "insurance-1",
            "INSURANCE_CLEAR",
            "100",
            datetime(2026, 9, 3, 17, 0, tzinfo=UTC),
        ),
    ]
    try:
        assert await repository.save_income_ledger(rows) == len(rows)
        assert await repository.save_income_ledger(rows) == 0

        ledger = await repository.list_income_ledger(date(2026, 9, 4), date(2026, 9, 4))
        daily = await repository.list_daily_pnl(date(2026, 9, 4), date(2026, 9, 5))
        trades = await repository.list_trade_pnl(date(2026, 9, 4), date(2026, 9, 5))
    finally:
        await database.dispose()

    assert {row["income_id"] for row in ledger} == {
        "realized-1",
        "commission-1",
        "funding-1",
        "insurance-1",
    }
    assert [(row["date"], row["net_pnl"]) for row in daily] == [
        ("2026-09-05", "-3.3000000000"),
        ("2026-09-04", "4.2500000000"),
    ]
    assert daily[1]["event_count"] == 3
    assert [row["trade_id"] for row in trades] == ["2001", "1001"]
    assert trades[1]["realized_pnl"] == "5.0000000000"
    assert trades[1]["commission"] == "-0.5000000000"
    assert trades[1]["funding_fee"] == "0"
    assert trades[1]["net_pnl"] == "4.5000000000"


@pytest.mark.asyncio
async def test_runtime_config_persists_and_applies_after_restart(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'config.db'}")
    await database.create_schema()
    repository = Repository(database)
    try:
        await repository.save_runtime_config(
            {"capital_limit_usdt": 2500.0, "max_leverage": 2, "ignored": "value"}
        )
        settings = Settings(capital_limit_usdt=1000, max_leverage=3)
        loaded = await repository.apply_runtime_config(settings)
    finally:
        await database.dispose()
    assert loaded == {"capital_limit_usdt": 2500.0, "max_leverage": 2}
    assert settings.capital_limit_usdt == 2500.0
    assert settings.max_leverage == 2


@pytest.mark.asyncio
async def test_hydration_preserves_original_risk_after_stop_tightening(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'positions.db'}")
    await database.create_schema()
    repository = Repository(database)
    original = position(
        quantity=Decimal("2"),
        initial_quantity=Decimal("2"),
        entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        original_stop_price=Decimal("98"),
        initial_risk_usdt=Decimal("4"),
    )
    await repository.sync_positions([original])
    current = position(
        quantity=Decimal("1.2"),
        initial_quantity=Decimal("1.2"),
        entry_price=Decimal("100"),
        mark_price=Decimal("103"),
        stop_price=Decimal("100.2"),
        original_stop_price=Decimal("100.2"),
        initial_risk_usdt=Decimal("0.24"),
    )
    try:
        hydrated = (await repository.hydrate_positions([current]))[0]
    finally:
        await database.dispose()
    assert hydrated.initial_quantity == Decimal("2")
    assert hydrated.original_stop_price == Decimal("98")
    assert hydrated.initial_risk_usdt == Decimal("4")
    assert hydrated.current_r == Decimal("1.5")


@pytest.mark.asyncio
async def test_hydration_preserves_verified_tp1_stage_while_algo_snapshot_is_unknown(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tp-stage.db'}")
    await database.create_schema()
    repository = Repository(database)
    previous = position(
        tp1_price=None,
        tp2_price=Decimal("104"),
        tp1_completed=True,
    )
    await repository.sync_positions([previous])
    unknown = position(
        tp1_price=None,
        tp2_price=None,
        tp1_completed=False,
        tp1_status_known=False,
    )
    try:
        hydrated = (await repository.hydrate_positions([unknown]))[0]
    finally:
        await database.dispose()
    assert hydrated.tp1_completed is True


@pytest.mark.asyncio
async def test_hydration_resets_tp1_stage_when_full_protection_reappears(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tp-stage-reset.db'}")
    await database.create_schema()
    repository = Repository(database)
    previous = position(
        tp1_price=None,
        tp2_price=Decimal("104"),
        tp1_completed=True,
    )
    await repository.sync_positions([previous])
    rebuilt = position(
        tp1_price=Decimal("102"),
        tp2_price=Decimal("104"),
        tp1_completed=False,
        tp1_status_known=True,
    )
    try:
        hydrated = (await repository.hydrate_positions([rebuilt]))[0]
    finally:
        await database.dispose()
    assert hydrated.tp1_completed is False


@pytest.mark.asyncio
async def test_signal_api_rows_normalize_thesis_to_reason(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'signals.db'}")
    await database.create_schema()
    repository = Repository(database)
    item = signal(thesis="structured rationale")
    await repository.save_signal(
        item,
        status="APPROVED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="a" * 64,
    )
    try:
        rows = await repository.list_signals()
    finally:
        await database.dispose()
    assert rows[0]["reason"] == "本轮允许开多，信号已通过硬风控。"
    assert "建议按本轮信号的入场区间执行" in rows[0]["recommendation_zh"]


@pytest.mark.asyncio
async def test_signal_api_rows_use_signal_analysis_time_not_persistence_time(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'signal-time.db'}")
    await database.create_schema()
    repository = Repository(database)
    analysis_time = datetime.now(UTC) - timedelta(minutes=45)
    item = signal(
        created_at=analysis_time,
        expires_at=analysis_time + timedelta(minutes=15),
    )
    await repository.save_signal(
        item,
        status="REJECTED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="e" * 64,
    )
    try:
        row = (await repository.list_signals())[0]
    finally:
        await database.dispose()
    assert row["created_at"] == analysis_time.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_signal_api_rows_ignore_anomalous_analysis_time(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'signal-time-outlier.db'}")
    await database.create_schema()
    repository = Repository(database)
    item = signal(
        created_at=datetime(2024, 5, 22, 10, 0, tzinfo=UTC),
        expires_at=datetime(2024, 5, 22, 10, 15, tzinfo=UTC),
    )
    await repository.save_signal(
        item,
        status="REJECTED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="f" * 64,
    )
    try:
        row = (await repository.list_signals())[0]
    finally:
        await database.dispose()
    assert isinstance(row["created_at"], datetime)
    assert row["created_at"].year == datetime.now(UTC).year


@pytest.mark.asyncio
async def test_signal_api_rows_sort_by_signal_analysis_time(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'signal-order.db'}")
    await database.create_schema()
    repository = Repository(database)
    base_time = datetime.now(UTC) - timedelta(minutes=30)
    later_signal = signal(
        symbol="LATERUSDT",
        created_at=base_time,
        expires_at=base_time + timedelta(minutes=15),
    )
    earlier_signal = signal(
        symbol="EARLIERUSDT",
        created_at=base_time - timedelta(minutes=5),
        expires_at=base_time + timedelta(minutes=10),
    )
    # Persist the newer analysis first and the older analysis second so
    # database insertion order is intentionally opposite signal time order.
    await repository.save_signal(
        later_signal,
        status="REJECTED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="1" * 64,
    )
    await repository.save_signal(
        earlier_signal,
        status="REJECTED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="2" * 64,
    )
    try:
        rows = await repository.list_signals()
    finally:
        await database.dispose()
    assert [row["symbol"] for row in rows[:2]] == ["LATERUSDT", "EARLIERUSDT"]


@pytest.mark.asyncio
async def test_signal_api_rows_include_market_context_and_risk_decision(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'signal-details.db'}")
    await database.create_schema()
    repository = Repository(database)
    item = signal(reason_codes=["trend_aligned"], risk_flags=["late_entry"])
    decision = RiskDecision(
        signal_id=item.signal_id,
        status=DecisionStatus.APPROVED,
        reasons=["all_hard_limits_passed"],
        capital_base=Decimal("1000"),
        risk_amount_usdt=Decimal("1"),
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        stop_price=Decimal("99"),
        target_price=Decimal("103"),
        leverage=3,
        estimated_margin=Decimal("33.33"),
        net_reward_risk=Decimal("2.5"),
    )
    await repository.save_signal(
        item,
        status="APPROVED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="c" * 64,
        market_snapshot=snapshot(),
    )
    await repository.save_risk_decision(decision)
    try:
        row = (await repository.list_signals())[0]
    finally:
        await database.dispose()
    assert row["market_context"]["mark_price"] == "100"
    assert row["market_context"]["trend_1h"] == 1
    assert row["risk_flags_zh"] == ["入场位置偏晚"]
    assert row["risk_decision"]["status"] == "APPROVED"
    assert row["risk_decision"]["reasons_zh"] == ["已通过全部硬风控"]
    assert row["risk_decision"]["net_reward_risk"] == "2.5"


@pytest.mark.asyncio
async def test_signal_api_rows_allow_missing_detail_records(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'signal-details-missing.db'}")
    await database.create_schema()
    repository = Repository(database)
    await repository.save_signal(
        signal(),
        status="REJECTED_UNKNOWN_SYMBOL",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="d" * 64,
    )
    try:
        row = (await repository.list_signals())[0]
    finally:
        await database.dispose()
    assert row["market_context"] is None
    assert row["risk_decision"] is None


@pytest.mark.asyncio
async def test_signal_reasons_and_recommendations_are_chinese(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'reason-zh.db'}")
    await database.create_schema()
    repository = Repository(database)
    item = signal(
        action="NO_TRADE",
        reason_codes=["HOLD_POSITION", "NO_NEW_TRIGGER", "UNKNOWN_NEW_GATE"],
    )
    await repository.save_signal(
        item,
        status="REJECTED",
        prompt_version="v1",
        model_name="gpt-5.6",
        input_hash="b" * 64,
    )
    try:
        row = (await repository.list_signals())[0]
    finally:
        await database.dispose()
    assert row["reason"] == (
        "本轮未开仓，原因：已有仓位，当前周期继续持有；当前没有新的入场触发；"
        "有一项交易条件未满足（条件、新的、条件）。"
    )
    assert row["recommendation_zh"].startswith("建议等待现有仓位平仓")
    assert all("_" not in label for label in row["reason_codes_zh"])


@pytest.mark.asyncio
async def test_order_portfolio_lineage_round_trips_and_legacy_orders_remain_compatible(
    tmp_path: object,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}")
    await database.create_schema()
    repository = Repository(database)
    decision_id = uuid4()
    allocation_id = uuid4()
    portfolio_order = OrderState(
        client_order_id="portfolio-entry",
        exchange_order_id="1001",
        symbol="BTCUSDT",
        side="BUY",
        position_side=PositionSide.LONG,
        order_type="LIMIT",
        quantity=Decimal("0.1"),
        filled_quantity=Decimal("0.1"),
        average_price=Decimal("100"),
        status=OrderStatus.FILLED,
        portfolio_decision_id=decision_id,
        portfolio_allocation_id=allocation_id,
        action_sequence=2,
    )
    legacy_order = OrderState(
        client_order_id="legacy-entry",
        symbol="ETHUSDT",
        side="SELL",
        position_side=PositionSide.SHORT,
        order_type="MARKET",
        quantity=Decimal("1"),
        status=OrderStatus.SUBMITTED,
    )
    try:
        await repository.save_orders([portfolio_order, legacy_order])
        rows = {str(row["client_order_id"]): row for row in await repository.list_orders()}
    finally:
        await database.dispose()

    assert rows["portfolio-entry"]["portfolio_decision_id"] == str(decision_id)
    assert rows["portfolio-entry"]["portfolio_allocation_id"] == str(allocation_id)
    assert rows["portfolio-entry"]["action_sequence"] == 2
    assert rows["legacy-entry"]["portfolio_decision_id"] is None
    assert rows["legacy-entry"]["portfolio_allocation_id"] is None
    assert rows["legacy-entry"]["action_sequence"] is None


@pytest.mark.asyncio
async def test_factor_research_run_lifecycle_is_persisted(tmp_path: object) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'factor-runs.db'}")
    await database.create_schema()
    repository = Repository(database)
    parameters: dict[str, object] = {
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "interval": "1h",
    }
    try:
        completed_id = await repository.create_factor_research_run(parameters)
        await repository.set_factor_research_running(completed_id)
        await repository.complete_factor_research(completed_id, {"summary": {"factor_count": 12}})
        failed_id = await repository.create_factor_research_run(parameters)
        await repository.fail_factor_research(failed_id, "market source unavailable")
        rows = await repository.list_factor_research_runs()
    finally:
        await database.dispose()

    by_id = {row["id"]: row for row in rows}
    assert by_id[completed_id]["status"] == "COMPLETED"
    assert by_id[completed_id]["report"] == {"summary": {"factor_count": 12}}
    assert by_id[completed_id]["completed_at"] is not None
    assert by_id[failed_id]["status"] == "FAILED"
    assert by_id[failed_id]["report"] == {"error": "market source unavailable"}
