from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.factories import position
from trading_system.domain.enums import OrderStatus, PortfolioPlanActionType, PositionSide
from trading_system.domain.models import (
    ExecutionIntent,
    OrderState,
    PortfolioDecision,
    PortfolioPlanAction,
)
from trading_system.exchange.base import ExchangeError, ExchangeUnknownStatusError
from trading_system.orchestration.cycle import TradingCycle


def _order(order_type: str = "STOP_MARKET") -> OrderState:
    return OrderState(
        client_order_id=f"protection-{order_type.lower()}",
        exchange_order_id="1",
        symbol="BTCUSDT",
        side="SELL",
        position_side=PositionSide.LONG,
        order_type=order_type,
        quantity=Decimal("5"),
        stop_price=Decimal("98"),
        status=OrderStatus.SUBMITTED,
    )


def _decision() -> PortfolioDecision:
    return PortfolioDecision(
        market_regime="TRENDING",
        portfolio_risk_budget_fraction=Decimal("1"),
        allocations=[],
        summary="执行测试",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


class FakeExitManager:
    async def execute(self, position, quantity, operation_id, limit_price):
        del position, operation_id, limit_price
        return [
            OrderState(
                client_order_id="reduce",
                exchange_order_id="2",
                symbol="BTCUSDT",
                side="SELL",
                position_side=PositionSide.LONG,
                order_type="LIMIT",
                quantity=quantity,
                filled_quantity=quantity,
                average_price=Decimal("100"),
                status=OrderStatus.FILLED,
            )
        ]


class FakeExchange:
    def __init__(self, remaining):
        self.remaining = remaining
        self.protection_quantities: list[Decimal] = []
        self.protection_intents: list[ExecutionIntent] = []
        self.canceled: list[str] = []
        self.tightened: list[tuple[str, Decimal]] = []

    async def get_positions(self):
        return [] if self.remaining is None else [self.remaining]

    async def upsert_protection(
        self, intent: ExecutionIntent, filled_quantity: Decimal, average_price: Decimal
    ):
        del average_price
        self.protection_intents.append(intent)
        self.protection_quantities.append(filled_quantity)
        return [_order("STOP_MARKET"), _order("TAKE_PROFIT_MARKET")]

    async def cancel_position_protection(self, position):
        self.canceled.append(position.symbol)

    async def tighten_stop(self, position, new_stop):
        self.tightened.append((position.symbol, new_stop))
        return _order("STOP_MARKET").model_copy(update={"stop_price": new_stop})

    async def best_entry_price(self, symbol: str, side: str) -> Decimal:
        del symbol, side
        return Decimal("100")


def _cycle(exchange: FakeExchange) -> TradingCycle:
    cycle = TradingCycle.__new__(TradingCycle)
    cycle.exchange = exchange
    cycle.exits = FakeExitManager()
    cycle.settings = SimpleNamespace(max_leverage=3)
    return cycle


def _reduce_action(position_state) -> PortfolioPlanAction:
    return PortfolioPlanAction(
        action_id=uuid4(),
        allocation_id=uuid4(),
        symbol=position_state.symbol,
        action=PortfolioPlanActionType.REDUCE,
        side=position_state.side,
        current_quantity=position_state.quantity,
        target_quantity=Decimal("5"),
        quantity_delta=Decimal("5"),
        target_risk_usdt=Decimal("10"),
        stop_price=position_state.stop_price,
        target_price=Decimal("106"),
        confidence=Decimal("0.9"),
        priority=1,
    )


def test_portfolio_action_fill_detection_ignores_unfilled_limit_order() -> None:
    action = PortfolioPlanAction(
        action_id=uuid4(),
        allocation_id=uuid4(),
        symbol="BTCUSDT",
        action=PortfolioPlanActionType.OPEN,
        side=PositionSide.LONG,
        target_quantity=Decimal("1"),
        quantity_delta=Decimal("1"),
    )
    order = _order("LIMIT").model_copy(
        update={"filled_quantity": Decimal("0"), "status": OrderStatus.SUBMITTED}
    )

    assert TradingCycle._portfolio_action_filled(action, [order]) is False
    assert TradingCycle._portfolio_action_filled(
        action, [order.model_copy(update={"filled_quantity": Decimal("1")})]
    ) is True


def test_price_guard_miss_is_a_soft_no_fill_but_unknown_order_status_is_not() -> None:
    assert TradingCycle._is_soft_entry_guard_error(
        ExchangeError("current price moved outside approved entry guard")
    )
    assert not TradingCycle._is_soft_entry_guard_error(
        ExchangeUnknownStatusError(
            "current price moved outside approved entry guard",
            client_order_id="frc-unknown",
        )
    )


@pytest.mark.asyncio
async def test_partial_reduce_refreshes_protection_for_remaining_quantity() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("10"),
        entry_price=Decimal("100"),
        mark_price=Decimal("101"),
        stop_price=Decimal("98"),
    )
    remaining = current.model_copy(update={"quantity": Decimal("5")})
    exchange = FakeExchange(remaining)
    result = await _cycle(exchange)._execute_portfolio_action(
        _reduce_action(current), {current.symbol: current}, _decision()
    )

    assert len(result) == 3
    assert exchange.protection_quantities == [Decimal("5")]
    assert exchange.protection_intents[0].tp2_price == Decimal("106")
    assert exchange.canceled == []


@pytest.mark.asyncio
async def test_partial_close_preserves_existing_take_profit_targets() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("10"),
        tp1_price=Decimal("102"),
        tp2_price=Decimal("108"),
    )
    remaining = current.model_copy(update={"quantity": Decimal("5")})
    action = _reduce_action(current).model_copy(
        update={
            "action": PortfolioPlanActionType.CLOSE,
            "target_quantity": Decimal("0"),
            "quantity_delta": Decimal("10"),
            "target_price": None,
        }
    )
    exchange = FakeExchange(remaining)

    await _cycle(exchange)._execute_portfolio_action(
        action, {current.symbol: current}, _decision()
    )

    assert exchange.protection_intents[0].tp1_price == Decimal("102")
    assert exchange.protection_intents[0].tp2_price == Decimal("108")


@pytest.mark.asyncio
async def test_full_close_cancels_all_remaining_protection() -> None:
    current = position(symbol="BTCUSDT", side=PositionSide.LONG, quantity=Decimal("5"))
    action = _reduce_action(current).model_copy(
        update={
            "action": PortfolioPlanActionType.CLOSE,
            "target_quantity": Decimal("0"),
            "quantity_delta": Decimal("5"),
        }
    )
    exchange = FakeExchange(None)
    result = await _cycle(exchange)._execute_portfolio_action(
        action, {current.symbol: current}, _decision()
    )

    assert len(result) == 1
    assert exchange.protection_quantities == []
    assert exchange.canceled == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_stop_only_portfolio_update_preserves_take_profit_orders() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        stop_price=Decimal("98"),
        tp1_price=Decimal("102"),
        tp2_price=Decimal("108"),
    )
    action = PortfolioPlanAction(
        action_id=uuid4(),
        allocation_id=uuid4(),
        symbol=current.symbol,
        action=PortfolioPlanActionType.TIGHTEN_STOP,
        side=current.side,
        current_quantity=current.quantity,
        target_quantity=current.quantity,
        target_risk_usdt=current.initial_risk_usdt,
        stop_price=Decimal("99"),
        target_price=current.tp2_price,
        confidence=Decimal("0.9"),
        priority=1,
        reasons=["hard_stop_tightened"],
    )
    exchange = FakeExchange(current)

    result = await _cycle(exchange)._execute_portfolio_action(
        action, {current.symbol: current}, _decision()
    )

    assert len(result) == 1
    assert exchange.tightened == [("BTCUSDT", Decimal("99"))]
    assert exchange.protection_intents == []


def test_portfolio_target_update_does_not_recreate_completed_tp1() -> None:
    current = position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        stop_price=Decimal("100.2"),
        tp1_price=None,
        tp2_price=Decimal("108"),
    )
    action = PortfolioPlanAction(
        action_id=uuid4(),
        allocation_id=uuid4(),
        symbol=current.symbol,
        action=PortfolioPlanActionType.TIGHTEN_STOP,
        side=current.side,
        current_quantity=current.quantity,
        target_quantity=current.quantity,
        target_risk_usdt=current.initial_risk_usdt,
        stop_price=current.stop_price,
        target_price=Decimal("110"),
        confidence=Decimal("0.9"),
        priority=1,
        reasons=["take_profit_updated"],
    )

    intent = _cycle(FakeExchange(current))._protection_intent(
        action, current, decision_id=_decision().decision_id
    )

    assert intent.tp1_price is None
    assert intent.tp2_price == Decimal("110")
