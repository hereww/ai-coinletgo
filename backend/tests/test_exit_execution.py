from decimal import Decimal

import pytest

from tests.test_execution_manager import FakeExchange, order
from trading_system.domain.enums import OrderStatus
from trading_system.execution.exit import ExitExecutionManager


async def no_sleep(seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_limit_exit_falls_back_to_market_after_thirty_seconds() -> None:
    exchange = FakeExchange()
    placed: list[str] = []

    async def place_limit_exit(position, quantity, price, operation_id):
        del position, quantity, price
        placed.append(operation_id)
        return order("exit-limit", OrderStatus.SUBMITTED, Decimal("0"))

    exchange.place_limit_exit = place_limit_exit  # type: ignore[method-assign]
    position = exchange_position()
    result = await ExitExecutionManager(
        exchange,
        limit_timeout_seconds=1,
        poll_seconds=1,
        sleep=no_sleep,
    ).execute(position, Decimal("1"), "operator-close", Decimal("100"))

    assert placed == ["operator-close"]
    assert [item.client_order_id for item in result] == [
        "exit-limit",
        "operator-close:market-fallback",
    ]
    assert result[-1].status == OrderStatus.FILLED
    assert exchange.protection_canceled == [position]
    assert exchange.take_profits_canceled == []


@pytest.mark.asyncio
async def test_cancel_response_fill_reduces_market_fallback_quantity() -> None:
    exchange = FakeExchange()

    async def place_limit_exit(position, quantity, price, operation_id):
        del position, quantity, price, operation_id
        return order("exit-limit", OrderStatus.SUBMITTED, Decimal("0"))

    async def cancel_order(symbol, client_order_id):
        del symbol
        return order(client_order_id, OrderStatus.CANCELED, Decimal("0.4"))

    exchange.place_limit_exit = place_limit_exit  # type: ignore[method-assign]
    exchange.cancel_order = cancel_order  # type: ignore[method-assign]
    position = exchange_position()
    result = await ExitExecutionManager(
        exchange,
        limit_timeout_seconds=1,
        poll_seconds=1,
        sleep=no_sleep,
    ).execute(position, Decimal("1"), "operator-close", Decimal("100"))

    assert result[-1].filled_quantity == Decimal("0.6")
    assert exchange.protection_canceled == [position]


def exchange_position():
    from tests.factories import position

    return position(quantity=Decimal("1"))
