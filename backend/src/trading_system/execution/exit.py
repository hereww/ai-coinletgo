from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from decimal import Decimal

from trading_system.domain.enums import OrderStatus
from trading_system.domain.models import OrderState, PositionState
from trading_system.exchange.base import ExchangeError, ExchangeGateway

Sleep = Callable[[float], Awaitable[None]]


class ExitExecutionManager:
    """Serialize exits per position and fall back from limit to market after a deadline."""

    def __init__(
        self,
        exchange: ExchangeGateway,
        *,
        limit_timeout_seconds: int = 30,
        poll_seconds: int = 1,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.exchange = exchange
        self.limit_timeout_seconds = limit_timeout_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def execute(
        self,
        position: PositionState,
        quantity: Decimal,
        operation_id: str,
        limit_price: Decimal,
    ) -> list[OrderState]:
        quantity = min(quantity, position.quantity)
        if quantity <= 0:
            raise ExchangeError("exit quantity must be positive")
        async with self._locks[position.position_id]:
            order = await self.exchange.place_limit_exit(
                position, quantity, limit_price, operation_id
            )
            orders = [order]
            latest = order
            elapsed = 0
            while (
                elapsed < self.limit_timeout_seconds
                and latest.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
            ):
                wait = min(self.poll_seconds, self.limit_timeout_seconds - elapsed)
                await self.sleep(wait)
                elapsed += wait
                latest = await self.exchange.get_order(position.symbol, order.client_order_id)
                orders[-1] = latest

            remaining = max(Decimal("0"), quantity - latest.filled_quantity)
            total_filled = latest.filled_quantity
            if remaining > 0:
                if latest.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
                    try:
                        latest = await self.exchange.cancel_order(
                            position.symbol, order.client_order_id
                        )
                        orders[-1] = latest
                        remaining = max(Decimal("0"), quantity - latest.filled_quantity)
                    except ExchangeError:
                        # A timeout can race with a fill. Re-read before deciding whether a market
                        # remainder is still required, preventing an accidental over-close.
                        latest = await self.exchange.get_order(
                            position.symbol, order.client_order_id
                        )
                        orders[-1] = latest
                        remaining = max(Decimal("0"), quantity - latest.filled_quantity)
                if remaining > 0:
                    market_orders = await self.exchange.close_position_quantity_market_orders(
                        position, remaining, f"{operation_id}:market-fallback"
                    )
                    orders.extend(market_orders)
                    total_filled += sum(
                        (item.filled_quantity for item in market_orders),
                        Decimal("0"),
                    )
            if total_filled > 0:
                if quantity >= position.quantity:
                    if total_filled >= quantity and await self._position_is_flat(position):
                        await self.exchange.cancel_position_protection(position)
                else:
                    await self.exchange.cancel_position_take_profits(position)
            return orders

    async def _position_is_flat(self, position: PositionState) -> bool:
        try:
            positions = await self.exchange.get_positions()
        except Exception:
            return False
        return not any(
            item.symbol == position.symbol
            and item.side == position.side
            and item.quantity > 0
            for item in positions
        )
