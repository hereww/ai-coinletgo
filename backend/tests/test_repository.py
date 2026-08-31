from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from tests.factories import position, signal, snapshot
from trading_system.config import Settings
from trading_system.domain.enums import DecisionStatus, OrderStatus, PositionSide
from trading_system.domain.models import OrderState, RiskDecision
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository


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
        rows = {
            str(row["client_order_id"]): row for row in await repository.list_orders()
        }
    finally:
        await database.dispose()

    assert rows["portfolio-entry"]["portfolio_decision_id"] == str(decision_id)
    assert rows["portfolio-entry"]["portfolio_allocation_id"] == str(allocation_id)
    assert rows["portfolio-entry"]["action_sequence"] == 2
    assert rows["legacy-entry"]["portfolio_decision_id"] is None
    assert rows["legacy-entry"]["portfolio_allocation_id"] is None
    assert rows["legacy-entry"]["action_sequence"] is None
