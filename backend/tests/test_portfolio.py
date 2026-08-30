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
