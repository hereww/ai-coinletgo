from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from tests.factories import context, position, snapshot
from trading_system.domain.enums import (
    OrderStatus,
    PortfolioPlanActionType,
    PortfolioPlanStatus,
    PortfolioTargetSide,
    PositionSide,
    SystemMode,
)
from trading_system.domain.models import (
    OrderState,
    PortfolioAllocation,
    PortfolioDecision,
    PortfolioPlanAction,
)
from trading_system.orchestration.cycle import TradingCycle
from trading_system.risk.portfolio import PortfolioCompiler


def filters() -> dict[str, object]:
    from trading_system.domain.models import ExchangeFilters

    return {
        "BTCUSDT": ExchangeFilters(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.1"),
            min_quantity=Decimal("0.1"),
            min_notional=Decimal("5"),
        ),
        "ETHUSDT": ExchangeFilters(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.1"),
            min_quantity=Decimal("0.1"),
            min_notional=Decimal("5"),
        ),
    }


def allocation(symbol: str = "BTCUSDT", **updates: object) -> PortfolioAllocation:
    values: dict[str, object] = {
        "symbol": symbol,
        "target_side": PortfolioTargetSide.LONG,
        "allocation_fraction": Decimal("0.5"),
        "priority": 1,
        "confidence": Decimal("0.9"),
        "entry_min": Decimal("99.9"),
        "entry_max": Decimal("100.1"),
        "stop_price": Decimal("98"),
        "target_price": Decimal("106"),
        "thesis": "趋势延续",
    }
    values.update(updates)
    return PortfolioAllocation.model_validate(values)


def decision(*allocations: PortfolioAllocation, **updates: object) -> PortfolioDecision:
    values: dict[str, object] = {
        "market_regime": "TRENDING",
        "portfolio_risk_budget_fraction": Decimal("1"),
        "allocations": list(allocations),
        "summary": "组合风险受控",
        "expires_at": datetime.now(UTC) + timedelta(minutes=15),
    }
    values.update(updates)
    return PortfolioDecision.model_validate(values)


def test_portfolio_decision_rejects_overallocated_risk() -> None:
    with pytest.raises(ValueError, match="cannot exceed one"):
        decision(allocation("BTCUSDT"), allocation("ETHUSDT", allocation_fraction=Decimal("0.6")))


def test_compiler_allocates_new_candidate_under_hard_risk_cap() -> None:
    snapshot_btc = snapshot()
    compiler = PortfolioCompiler()
    plan = compiler.compile(
        decision(allocation()),
        snapshots={"BTCUSDT": snapshot_btc},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )
    assert plan.status == PortfolioPlanStatus.APPROVED
    assert plan.actions[0].action == PortfolioPlanActionType.OPEN
    assert plan.actions[0].target_quantity > 0
    assert plan.approved_risk_usdt <= plan.risk_cap_usdt


def test_compiler_applies_snapshot_volatility_risk_multiplier() -> None:
    compiler = PortfolioCompiler()
    full = compiler.compile(
        decision(allocation()),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )
    reduced = compiler.compile(
        decision(allocation()),
        snapshots={
            "BTCUSDT": snapshot(volatility_risk_multiplier=Decimal("0.5"))
        },
        account=context().account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )
    assert reduced.actions[0].target_risk_usdt < full.actions[0].target_risk_usdt


def test_compiler_allows_strong_uptrend_long_without_15m_trigger() -> None:
    limits = context().limits.model_copy(
        update={"strong_trend_entry_override_enabled": True, "strong_trend_adx_min": 30}
    )
    plan = PortfolioCompiler().compile(
        decision(allocation()),
        snapshots={"BTCUSDT": snapshot(breakout_15m=0, pullback_15m=0)},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=limits,
        mode=SystemMode.TESTNET,
    )
    assert plan.status == PortfolioPlanStatus.APPROVED
    assert plan.actions[0].action == PortfolioPlanActionType.OPEN
    assert "strong_trend_entry_override" in plan.actions[0].reasons


def test_model_primary_accepts_opportunity_without_indicator_gates() -> None:
    limits = context().limits.model_copy(
        update={"model_primary_portfolio_enabled": True, "min_net_reward_risk": 3}
    )
    plan = PortfolioCompiler().compile(
        decision(
            allocation(
                confidence=Decimal("0.1"),
                target_price=Decimal("100.2"),
            )
        ),
        snapshots={
            "BTCUSDT": snapshot(
                market_regime="RANGING",
                trend_1h=-1,
                trend_4h=-1,
                adx_1h=Decimal("5"),
                breakout_15m=0,
                pullback_15m=0,
            )
        },
        account=context().account,
        positions=[],
        filters=filters(),
        limits=limits,
        mode=SystemMode.TESTNET,
    )
    assert plan.status == PortfolioPlanStatus.APPROVED
    assert plan.actions[0].action == PortfolioPlanActionType.OPEN
    assert "model_primary_opportunity_accepted" in plan.actions[0].reasons


def test_model_primary_does_not_block_model_add_with_cooldown() -> None:
    limits = context().limits.model_copy(update={"model_primary_portfolio_enabled": True})
    current = position(
        symbol="BTCUSDT",
        quantity=Decimal("1"),
        initial_quantity=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("98"),
        original_stop_price=Decimal("98"),
        initial_risk_usdt=Decimal("1"),
    )
    plan = PortfolioCompiler().compile(
        decision(
            allocation(
                allocation_fraction=Decimal("1"),
                stop_price=Decimal("98"),
                target_price=Decimal("106"),
            )
        ),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=limits,
        mode=SystemMode.TESTNET,
        last_rebalance_at=datetime.now(UTC),
        cooldown_minutes=30,
    )
    assert plan.actions[0].action == PortfolioPlanActionType.ADD
    assert all("rebalance_cooldown_active" not in item.reasons for item in plan.actions)


def test_compiler_strong_uptrend_bypasses_opportunity_filters() -> None:
    limits = context().limits.model_copy(
        update={
            "strong_trend_entry_override_enabled": True,
            "strong_trend_adx_min": Decimal("30"),
            "min_confidence": Decimal("0.75"),
            "min_net_reward_risk": Decimal("2"),
            "max_same_direction": 1,
            "correlation_limit": Decimal("0.1"),
        }
    )
    current = position(
        symbol="ETHUSDT",
        quantity=Decimal("0.5"),
        initial_quantity=Decimal("0.5"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("98"),
        original_stop_price=Decimal("98"),
        initial_risk_usdt=Decimal("1"),
    )
    keep_eth = allocation(
        "ETHUSDT",
        allocation_fraction=Decimal("0.13"),
        stop_price=Decimal("98"),
        target_price=Decimal("106"),
    )
    aggressive_btc = allocation(
        confidence=Decimal("0.1"),
        stop_price=Decimal("99"),
        target_price=Decimal("100.2"),
    )
    now = datetime.now(UTC)

    plan = PortfolioCompiler().compile(
        decision(keep_eth, aggressive_btc),
        snapshots={
            "BTCUSDT": snapshot(breakout_15m=0, pullback_15m=0),
            "ETHUSDT": snapshot(symbol="ETHUSDT"),
        },
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=limits,
        mode=SystemMode.TESTNET,
        correlations={("BTCUSDT", "ETHUSDT"): Decimal("0.99")},
        last_rebalance_at=now,
        cooldown_minutes=30,
        now=now,
    )

    btc_action = next(item for item in plan.actions if item.symbol == "BTCUSDT")
    assert btc_action.action == PortfolioPlanActionType.OPEN
    assert "strong_trend_entry_override" in btc_action.reasons


def test_compiler_strong_uptrend_keeps_available_balance_guard() -> None:
    limits = context().limits.model_copy(
        update={"strong_trend_entry_override_enabled": True}
    )
    account = context().account.model_copy(update={"available_balance": Decimal("0")})
    plan = PortfolioCompiler().compile(
        decision(allocation(confidence=Decimal("0.1"))),
        snapshots={"BTCUSDT": snapshot(breakout_15m=0, pullback_15m=0)},
        account=account,
        positions=[],
        filters=filters(),
        limits=limits,
        mode=SystemMode.TESTNET,
    )

    assert plan.actions[0].action == PortfolioPlanActionType.REJECTED
    assert "available_balance_insufficient" in plan.actions[0].reasons


def test_compiler_uses_manual_atr_exits_for_new_entries() -> None:
    limits = context().limits.model_copy(
        update={
            "manual_exit_levels_enabled": True,
            "manual_stop_atr": Decimal("1.8"),
            "manual_take_profit_atr": Decimal("5"),
            "max_stop_atr": Decimal("4"),
            "min_net_reward_risk": Decimal("1.8"),
        }
    )
    requested = allocation(
        stop_price=Decimal("99.5"),
        target_price=Decimal("101"),
    )
    plan = PortfolioCompiler().compile(
        decision(requested),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=limits,
        mode=SystemMode.TESTNET,
    )

    action = plan.actions[0]
    assert action.action == PortfolioPlanActionType.OPEN
    assert action.stop_price == Decimal("98.3")
    assert action.target_price == Decimal("105.1")


@pytest.mark.parametrize(
    ("side", "stop_price", "target_price", "risk_entry"),
    [
        (PortfolioTargetSide.LONG, Decimal("98.2"), Decimal("106"), Decimal("100.2")),
        (PortfolioTargetSide.SHORT, Decimal("101.8"), Decimal("94"), Decimal("99.8")),
    ],
)
def test_compiler_sizes_new_position_from_worst_permitted_fill_edge(
    side: PortfolioTargetSide,
    stop_price: Decimal,
    target_price: Decimal,
    risk_entry: Decimal,
) -> None:
    requested = allocation(
        target_side=side,
        allocation_fraction=Decimal("1"),
        entry_min=Decimal("99.8"),
        entry_max=Decimal("100.2"),
        stop_price=stop_price,
        target_price=target_price,
    )

    candidate_snapshot = snapshot(
        trend_1h=-1,
        trend_4h=-1,
        breakout_15m=-1,
    ) if side == PortfolioTargetSide.SHORT else snapshot()
    plan = PortfolioCompiler().compile(
        decision(requested),
        snapshots={"BTCUSDT": candidate_snapshot},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )

    action = plan.actions[0]
    worst_fill_risk = action.target_quantity * abs(risk_entry - stop_price)
    assert action.action == PortfolioPlanActionType.OPEN
    assert action.target_quantity == Decimal("3.7")
    assert action.target_risk_usdt == worst_fill_risk
    assert worst_fill_risk <= plan.risk_cap_usdt


def test_compiler_rejects_target_when_available_balance_is_insufficient() -> None:
    account = context().account.model_copy(update={"available_balance": Decimal("0")})
    plan = PortfolioCompiler().compile(
        decision(allocation()),
        snapshots={"BTCUSDT": snapshot()},
        account=account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )

    assert plan.status == PortfolioPlanStatus.REJECTED
    assert plan.actions[0].action == PortfolioPlanActionType.REJECTED
    assert "available_balance_insufficient" in plan.actions[0].reasons


def test_compiler_closes_existing_position_when_target_is_flat() -> None:
    current = position(symbol="BTCUSDT", side=PositionSide.LONG)
    flat = allocation(
        "BTCUSDT",
        target_side=PortfolioTargetSide.FLAT,
        allocation_fraction=Decimal("0"),
        entry_min=None,
        entry_max=None,
        stop_price=None,
        target_price=None,
    )
    plan = PortfolioCompiler().compile(
        decision(flat),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )
    assert plan.actions[0].action == PortfolioPlanActionType.CLOSE
    assert plan.actions[0].target_quantity == 0


@pytest.mark.parametrize("mode", [SystemMode.PAUSED, SystemMode.RISK_HALTED])
def test_compiler_allows_de_risk_actions_when_entries_are_halted(mode: SystemMode) -> None:
    current = position(symbol="BTCUSDT", side=PositionSide.LONG)
    flat = allocation(
        "BTCUSDT",
        target_side=PortfolioTargetSide.FLAT,
        allocation_fraction=Decimal("0"),
        entry_min=None,
        entry_max=None,
        stop_price=None,
        target_price=None,
    )

    plan = PortfolioCompiler().compile(
        decision(flat),
        snapshots={},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=context().limits,
        mode=mode,
    )

    assert plan.actions[0].action == PortfolioPlanActionType.CLOSE
    assert plan.actions[0].reasons == ["model_target_is_flat"]


@pytest.mark.parametrize("mode", [SystemMode.PAUSED, SystemMode.RISK_HALTED])
def test_compiler_rejects_risk_increases_when_entries_are_halted(mode: SystemMode) -> None:
    plan = PortfolioCompiler().compile(
        decision(allocation()),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=mode,
    )

    assert plan.status == PortfolioPlanStatus.REJECTED
    assert plan.actions[0].action == PortfolioPlanActionType.REJECTED
    assert plan.actions[0].reasons == ["system_mode_disallows_risk_increase"]


def test_compiler_defers_same_cycle_reversal() -> None:
    current = position(symbol="BTCUSDT", side=PositionSide.LONG)
    reverse = allocation(
        "BTCUSDT",
        target_side=PortfolioTargetSide.SHORT,
        entry_min=Decimal("99.9"),
        entry_max=Decimal("100.1"),
        stop_price=Decimal("102"),
        target_price=Decimal("94"),
    )
    plan = PortfolioCompiler().compile(
        decision(reverse),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )
    assert plan.actions[0].action == PortfolioPlanActionType.CLOSE
    assert "same_cycle_reversal_deferred" in plan.actions[0].reasons


def test_compiler_refreshes_protection_when_target_price_changes() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("98"),
        tp1_price=Decimal("102"),
        tp2_price=Decimal("104"),
        initial_risk_usdt=Decimal("2"),
    )
    updated = allocation(
        "BTCUSDT",
        allocation_fraction=Decimal("0.27"),
        stop_price=Decimal("98"),
        target_price=Decimal("108"),
    )

    plan = PortfolioCompiler().compile(
        decision(updated),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )

    assert plan.actions[0].action == PortfolioPlanActionType.TIGHTEN_STOP
    assert "take_profit_updated" in plan.actions[0].reasons


def test_compiler_ignores_existing_target_that_collides_with_tp1() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("98"),
        tp1_price=Decimal("102"),
        tp2_price=Decimal("106"),
        initial_risk_usdt=Decimal("2"),
    )
    invalid_update = allocation(
        "BTCUSDT",
        allocation_fraction=Decimal("0.27"),
        stop_price=Decimal("98"),
        target_price=Decimal("102"),
    )

    plan = PortfolioCompiler().compile(
        decision(invalid_update),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )

    assert plan.actions[0].action == PortfolioPlanActionType.HOLD
    assert plan.actions[0].target_price == Decimal("106")
    assert "take_profit_target_not_beyond_tp1_ignored" in plan.actions[0].reasons


def test_compiler_rejects_add_when_target_does_not_extend_beyond_tp1() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        stop_price=Decimal("98"),
        tp1_price=Decimal("102"),
        tp2_price=Decimal("106"),
        initial_risk_usdt=Decimal("2"),
    )
    invalid_add = allocation(
        "BTCUSDT",
        allocation_fraction=Decimal("0.5"),
        stop_price=Decimal("98"),
        target_price=Decimal("102"),
    )

    plan = PortfolioCompiler().compile(
        decision(invalid_add),
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[current],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )

    assert plan.actions[0].action == PortfolioPlanActionType.REJECTED
    assert plan.actions[0].reasons == ["take_profit_target_not_beyond_tp1"]


def test_compiler_fails_closed_for_expired_decision() -> None:
    created_at = datetime.now(UTC) - timedelta(minutes=15)
    expired = decision(
        allocation(),
        created_at=created_at,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    plan = PortfolioCompiler().compile(
        expired,
        snapshots={"BTCUSDT": snapshot()},
        account=context().account,
        positions=[],
        filters=filters(),
        limits=context().limits,
        mode=SystemMode.TESTNET,
    )
    assert plan.status == PortfolioPlanStatus.REJECTED
    assert plan.actions == []
    assert "portfolio_decision_expired" in plan.reasons


def test_all_portfolio_child_orders_receive_the_same_lineage() -> None:
    decision_id = uuid4()
    allocation_id = uuid4()
    portfolio_decision = decision(
        allocation("BTCUSDT", allocation_id=allocation_id),
        decision_id=decision_id,
    )
    action = PortfolioPlanAction(
        allocation_id=allocation_id,
        symbol="BTCUSDT",
        action=PortfolioPlanActionType.OPEN,
        side=PositionSide.LONG,
        target_quantity=Decimal("1"),
        quantity_delta=Decimal("1"),
        action_sequence=3,
    )
    orders = [
        OrderState(
            client_order_id="entry",
            symbol="BTCUSDT",
            side="BUY",
            position_side=PositionSide.LONG,
            order_type="LIMIT",
            quantity=Decimal("1"),
            status=OrderStatus.FILLED,
        ),
        OrderState(
            client_order_id="stop",
            symbol="BTCUSDT",
            side="SELL",
            position_side=PositionSide.LONG,
            order_type="STOP_MARKET",
            quantity=Decimal("1"),
            status=OrderStatus.SUBMITTED,
        ),
        OrderState(
            client_order_id="take-profit",
            symbol="BTCUSDT",
            side="SELL",
            position_side=PositionSide.LONG,
            order_type="TAKE_PROFIT_MARKET",
            quantity=Decimal("1"),
            status=OrderStatus.SUBMITTED,
        ),
    ]

    tagged = TradingCycle._tag_portfolio_orders(orders, action, portfolio_decision)

    assert [item.client_order_id for item in tagged] == [
        "entry",
        "stop",
        "take-profit",
    ]
    assert all(item.portfolio_decision_id == decision_id for item in tagged)
    assert all(item.portfolio_allocation_id == allocation_id for item in tagged)
    assert all(item.action_sequence == 3 for item in tagged)
