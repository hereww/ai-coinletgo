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
        self.canceled: list[str] = []

    async def get_positions(self):
        return [] if self.remaining is None else [self.remaining]

    async def upsert_protection(
        self, intent: ExecutionIntent, filled_quantity: Decimal, average_price: Decimal
    ):
        del intent, average_price
        self.protection_quantities.append(filled_quantity)
        return [_order("STOP_MARKET"), _order("TAKE_PROFIT_MARKET")]

    async def cancel_position_protection(self, position):
        self.canceled.append(position.symbol)

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
    assert exchange.canceled == []


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
