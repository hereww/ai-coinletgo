from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.domain.enums import OrderStatus, PositionSide
from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    ExecutionIntent,
    OrderState,
    PositionState,
    UniverseSymbol,
)
from trading_system.exchange.base import ExchangeError, ExchangeGateway
from trading_system.execution.manager import (
    EntryNotSubmittedError,
    ExecutionManager,
    ProtectionError,
)


class FakeExchange(ExchangeGateway):
    def __init__(self, *, fail_protection: bool = False) -> None:
        self.fail_protection = fail_protection
        self.entry_ids: list[str] = []
        self.closed: list[PositionState] = []
        self.take_profits_canceled: list[PositionState] = []
        self.protection_canceled: list[PositionState] = []

    async def health_check(self) -> tuple[bool, str]:
        return True, "ok"

    async def get_account_state(self) -> AccountState:
        raise NotImplementedError

    async def get_positions(self) -> list[PositionState]:
        return []

    async def get_universe(self, limit: int) -> list[UniverseSymbol]:
        return []

    async def get_filters(self, symbol: str) -> ExchangeFilters:
        raise NotImplementedError

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        return []

    async def configure_symbol(self, symbol: str, leverage: int) -> None:
        return None

    async def best_entry_price(self, symbol: str, side: str) -> Decimal:
        return Decimal("100")

    async def place_limit_entry(
        self, intent: ExecutionIntent, client_order_id: str, price: Decimal
    ) -> OrderState:
        self.entry_ids.append(client_order_id)
        return order(client_order_id, OrderStatus.SUBMITTED, Decimal("0"))

    async def get_order(self, symbol: str, client_order_id: str) -> OrderState:
        return order(client_order_id, OrderStatus.PARTIALLY_FILLED, Decimal("0.5"))

    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderState:
        return order(client_order_id, OrderStatus.CANCELED, Decimal("0.5"))

    async def upsert_protection(
        self, intent: ExecutionIntent, filled_quantity: Decimal, average_price: Decimal
    ) -> list[OrderState]:
        if self.fail_protection:
            raise ExchangeError("protection rejected")
        return [order("stop", OrderStatus.SUBMITTED, Decimal("0"), "STOP_MARKET")]

    async def close_position_market(self, position: PositionState, reason: str) -> OrderState:
        self.closed.append(position)
        return order("close", OrderStatus.FILLED, position.quantity)

    async def close_position_quantity_market(
        self, position: PositionState, quantity: Decimal, operation_id: str
    ) -> OrderState:
        return order(operation_id, OrderStatus.FILLED, quantity)

    async def cancel_position_take_profits(self, position: PositionState) -> None:
        self.take_profits_canceled.append(position)

    async def cancel_position_protection(self, position: PositionState) -> None:
        self.protection_canceled.append(position)

    async def tighten_stop(self, position: PositionState, new_stop: Decimal) -> OrderState:
        return order("tighten", OrderStatus.SUBMITTED, Decimal("0"), "STOP_MARKET")

    async def cancel_all_entry_orders(self) -> None:
        return None


def order(
    client_id: str,
    status: OrderStatus,
    filled: Decimal,
    order_type: str = "LIMIT",
) -> OrderState:
    return OrderState(
        client_order_id=client_id,
        exchange_order_id="1",
        symbol="BTCUSDT",
        side="BUY",
        position_side=PositionSide.LONG,
        order_type=order_type,
        quantity=Decimal("1"),
        filled_quantity=filled,
        average_price=Decimal("100") if filled else Decimal("0"),
        status=status,
    )


def intent() -> ExecutionIntent:
    return ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        intent_id="d7d53c60-1a2a-411d-a6bc-6b0665c43bbc",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def no_sleep(seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_partial_fill_is_protected_before_return() -> None:
    exchange = FakeExchange()
    entry, protection = await ExecutionManager(exchange, reprice_seconds=1, sleep=no_sleep).execute(
        intent()
    )
    assert entry.filled_quantity == Decimal("0.5")
    assert protection[0].order_type == "STOP_MARKET"
    assert exchange.closed == []


@pytest.mark.asyncio
async def test_protection_failure_closes_partial_immediately() -> None:
    exchange = FakeExchange(fail_protection=True)
    with pytest.raises(ProtectionError):
        await ExecutionManager(exchange, reprice_seconds=1, sleep=no_sleep).execute(intent())
    assert len(exchange.closed) == 1
    assert exchange.closed[0].quantity == Decimal("0.5")
    assert exchange.closed[0].protected is False
    # A failed emergency close must not cancel exchange-side protection;
    # reconciliation will remove orphan protections only after the position is
    # confirmed flat.
    assert exchange.protection_canceled == []


@pytest.mark.asyncio
async def test_entry_client_id_is_deterministic_for_retry() -> None:
    exchange = FakeExchange()
    manager = ExecutionManager(exchange, reprice_seconds=1, sleep=no_sleep)
    await manager.execute(intent())
    await manager.execute(intent())
    assert exchange.entry_ids == ["frc_d7d53c601a2a411da6_e0"] * 2


@pytest.mark.asyncio
async def test_cancel_response_additional_fill_is_protected_before_return() -> None:
    exchange = FakeExchange()
    protected: list[Decimal] = []

    async def get_order(symbol: str, client_order_id: str) -> OrderState:
        del symbol
        return order(client_order_id, OrderStatus.PARTIALLY_FILLED, Decimal("0.2"))

    async def cancel_order(symbol: str, client_order_id: str) -> OrderState:
        del symbol
        return order(client_order_id, OrderStatus.CANCELED, Decimal("0.5"))

    async def upsert_protection(
        execution_intent: ExecutionIntent,
        filled_quantity: Decimal,
        average_price: Decimal,
    ) -> list[OrderState]:
        del execution_intent, average_price
        protected.append(filled_quantity)
        return [order("stop", OrderStatus.SUBMITTED, Decimal("0"), "STOP_MARKET")]

    exchange.get_order = get_order  # type: ignore[method-assign]
    exchange.cancel_order = cancel_order  # type: ignore[method-assign]
    exchange.upsert_protection = upsert_protection  # type: ignore[method-assign]

    entry, _ = await ExecutionManager(
        exchange, reprice_seconds=1, sleep=no_sleep
    ).execute(intent())

    assert entry.filled_quantity == Decimal("0.5")
    assert protected == [Decimal("0.2"), Decimal("0.5")]


@pytest.mark.asyncio
async def test_mode_guard_stops_repricing_after_pause() -> None:
    exchange = FakeExchange()
    checks = 0

    async def entry_guard() -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    entry, _ = await ExecutionManager(
        exchange,
        reprice_seconds=1,
        sleep=no_sleep,
        entry_guard=entry_guard,
    ).execute(intent())

    assert entry.status == OrderStatus.CANCELED
    assert exchange.entry_ids == ["frc_d7d53c601a2a411da6_e0"]


@pytest.mark.asyncio
async def test_explicit_entry_rejection_is_marked_as_no_submission() -> None:
    exchange = FakeExchange()

    async def reject_entry(
        execution_intent: ExecutionIntent, client_order_id: str, price: Decimal
    ) -> OrderState:
        del execution_intent, client_order_id, price
        raise ExchangeError(
            "400 [-1111]: Precision is over the maximum defined",
            code=-1111,
            http_status=400,
        )

    exchange.place_limit_entry = reject_entry  # type: ignore[method-assign]

    with pytest.raises(EntryNotSubmittedError, match="explicitly rejected"):
        await ExecutionManager(exchange, reprice_seconds=1, sleep=no_sleep).execute(intent())
    assert exchange.entry_ids == []
