from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from trading_system.domain.enums import OrderStatus, PositionSide
from trading_system.domain.models import ExecutionIntent, OrderState, PositionState
from trading_system.exchange.base import ExchangeError, ExchangeGateway, ExchangeUnknownStatusError

Sleep = Callable[[float], Awaitable[None]]
EntryGuard = Callable[[], Awaitable[bool]]


async def _entries_allowed() -> bool:
    return True


class ProtectionError(RuntimeError):
    pass


class EntryNotSubmittedError(ExchangeError):
    """An entry failed before Binance could create an exposure.

    This is deliberately narrower than a generic exchange error.  It is used
    only for pre-flight work and Binance's explicit 4xx order rejection;
    cancellations, partial fills, protection updates, and ambiguous writes
    keep their existing fail-closed behaviour.
    """


class ExecutionManager:
    def __init__(
        self,
        exchange: ExchangeGateway,
        *,
        reprice_seconds: int = 30,
        max_reprices: int = 2,
        fill_poll_seconds: int = 1,
        sleep: Sleep = asyncio.sleep,
        entry_guard: EntryGuard = _entries_allowed,
    ) -> None:
        self.exchange = exchange
        self.reprice_seconds = reprice_seconds
        self.max_reprices = max_reprices
        self.fill_poll_seconds = fill_poll_seconds
        self.sleep = sleep
        self.entry_guard = entry_guard
        self.last_emergency_orders: list[OrderState] = []

    async def execute(self, intent: ExecutionIntent) -> tuple[OrderState, list[OrderState]]:
        self.last_emergency_orders = []
        if intent.expires_at <= datetime.now(UTC):
            raise EntryNotSubmittedError("execution intent expired")
        try:
            await self.exchange.configure_symbol(intent.symbol, intent.leverage)
        except ExchangeUnknownStatusError:
            raise
        except ExchangeError as error:
            # Changing margin/leverage cannot create a position.  An error
            # here is therefore safe to audit as a rejected opportunity and
            # retry next cycle rather than pausing every future entry.
            raise EntryNotSubmittedError(
                f"entry preparation failed: {str(error)[:300]}",
                code=error.code,
                http_status=error.http_status,
                headers=error.headers,
                retry_after_seconds=error.retry_after_seconds,
            ) from error
        entry_side = "BUY" if intent.side == PositionSide.LONG else "SELL"
        last_order: OrderState | None = None
        protected_quantity = Decimal("0")
        protection_orders: list[OrderState] = []

        for attempt in range(self.max_reprices + 1):
            if not await self.entry_guard():
                raise ExchangeError("system mode no longer allows entries")
            try:
                price = await self.exchange.best_entry_price(intent.symbol, entry_side)
            except ExchangeUnknownStatusError:
                raise
            except ExchangeError as error:
                # This is a read-only quote lookup.  No entry exists yet.
                raise EntryNotSubmittedError(
                    f"entry quote unavailable: {str(error)[:300]}",
                    code=error.code,
                    http_status=error.http_status,
                    headers=error.headers,
                    retry_after_seconds=error.retry_after_seconds,
                ) from error
            if not self._inside_entry_guard(intent, price):
                raise ExchangeError("current price moved outside approved entry guard")
            client_id = self._entry_client_id(intent, attempt)
            try:
                order = await self.exchange.place_limit_entry(intent, client_id, price)
            except ExchangeUnknownStatusError:
                raise
            except ExchangeError as error:
                # The Binance adapter resolves ambiguous POST failures into
                # ExchangeUnknownStatusError.  A direct 4xx here is an
                # explicit exchange rejection, so Binance did not accept the
                # entry and there is no new position to reconcile.
                if error.http_status is not None and 400 <= error.http_status < 500:
                    raise EntryNotSubmittedError(
                        f"entry order explicitly rejected: {str(error)[:300]}",
                        code=error.code,
                        http_status=error.http_status,
                        headers=error.headers,
                        retry_after_seconds=error.retry_after_seconds,
                    ) from error
                raise
            last_order = order
            protected_quantity, protection_orders = await self._protect_new_fills(
                intent,
                order,
                protected_quantity,
                protection_orders,
            )
            if order.status == OrderStatus.FILLED:
                return order, protection_orders
            elapsed = 0
            while elapsed < self.reprice_seconds:
                wait_seconds = min(self.fill_poll_seconds, self.reprice_seconds - elapsed)
                await self.sleep(wait_seconds)
                elapsed += wait_seconds
                order = await self.exchange.get_order(intent.symbol, client_id)
                last_order = order

                protected_quantity, protection_orders = await self._protect_new_fills(
                    intent,
                    order,
                    protected_quantity,
                    protection_orders,
                )

                if order.status == OrderStatus.FILLED:
                    return order, protection_orders
                if order.status in {
                    OrderStatus.CANCELED,
                    OrderStatus.REJECTED,
                    OrderStatus.EXPIRED,
                }:
                    break
                if not await self.entry_guard():
                    order = await self.exchange.cancel_order(intent.symbol, client_id)
                    last_order = order
                    protected_quantity, protection_orders = await self._protect_new_fills(
                        intent,
                        order,
                        protected_quantity,
                        protection_orders,
                    )
                    return order, protection_orders
            if order.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
                order = await self.exchange.cancel_order(intent.symbol, client_id)
                last_order = order
                protected_quantity, protection_orders = await self._protect_new_fills(
                    intent,
                    order,
                    protected_quantity,
                    protection_orders,
                )
            if order.filled_quantity > 0:
                return order, protection_orders
            if intent.expires_at <= datetime.now(UTC):
                break

        if last_order is None:
            raise ExchangeError("entry order was not submitted")
        return last_order, protection_orders

    @staticmethod
    def _inside_entry_guard(intent: ExecutionIntent, price: Decimal) -> bool:
        return intent.entry_min <= price <= intent.entry_max

    @staticmethod
    def _entry_client_id(intent: ExecutionIntent, attempt: int) -> str:
        token = str(intent.intent_id).replace("-", "")[:18]
        return f"frc_{token}_e{attempt}"

    async def _protect_new_fills(
        self,
        intent: ExecutionIntent,
        order: OrderState,
        protected_quantity: Decimal,
        protection_orders: list[OrderState],
    ) -> tuple[Decimal, list[OrderState]]:
        if order.filled_quantity <= protected_quantity:
            return protected_quantity, protection_orders
        try:
            protection_orders = await self.exchange.upsert_protection(
                intent, order.filled_quantity, order.average_price
            )
        except ExchangeError as error:
            await self._emergency_close_partial(intent, order, "protection_failed")
            raise ProtectionError("filled quantity could not be protected") from error
        return order.filled_quantity, protection_orders

    async def _emergency_close_partial(
        self, intent: ExecutionIntent, order: OrderState, reason: str
    ) -> None:
        if order.filled_quantity <= 0:
            return
        entry_price = order.average_price if order.average_price > 0 else intent.limit_price
        position = PositionState(
            position_id=f"partial-{intent.intent_id}",
            symbol=intent.symbol,
            side=intent.side,
            quantity=order.filled_quantity,
            entry_price=entry_price,
            mark_price=entry_price,
            stop_price=intent.stop_price,
            initial_risk_usdt=order.filled_quantity * abs(entry_price - intent.stop_price),
            protected=False,
        )
        # Keep any exchange-side protection in place until a subsequent
        # reconciliation confirms that the emergency close actually flattened
        # the position.  Cancelling here can leave a partial remainder naked.
        self.last_emergency_orders = await self.exchange.close_position_market_orders(
            position, reason
        )
