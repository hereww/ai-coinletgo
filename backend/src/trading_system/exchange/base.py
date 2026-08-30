from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    ExecutionIntent,
    OrderState,
    PositionState,
    UniverseSymbol,
)


class ExchangeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.headers = headers or {}
        self.retry_after_seconds = retry_after_seconds


class ExchangeUnknownStatusError(ExchangeError):
    """A write request may have reached Binance but its result is unknown."""

    def __init__(
        self,
        message: str,
        *,
        client_order_id: str,
        code: int | None = None,
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            http_status=http_status,
            headers=headers,
            retry_after_seconds=retry_after_seconds,
        )
        self.client_order_id = client_order_id


class ExchangeGateway(ABC):
    @abstractmethod
    async def health_check(self) -> tuple[bool, str]: ...

    @abstractmethod
    async def get_account_state(self) -> AccountState: ...

    @abstractmethod
    async def get_positions(self) -> list[PositionState]: ...

    @abstractmethod
    async def get_universe(self, limit: int) -> list[UniverseSymbol]: ...

    @abstractmethod
    async def get_filters(self, symbol: str) -> ExchangeFilters: ...

    @abstractmethod
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[Candle]: ...

    @abstractmethod
    async def configure_symbol(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    async def best_entry_price(self, symbol: str, side: str) -> Decimal: ...

    @abstractmethod
    async def place_limit_entry(
        self, intent: ExecutionIntent, client_order_id: str, price: Decimal
    ) -> OrderState: ...

    @abstractmethod
    async def get_order(self, symbol: str, client_order_id: str) -> OrderState: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderState: ...

    @abstractmethod
    async def upsert_protection(
        self, intent: ExecutionIntent, filled_quantity: Decimal, average_price: Decimal
    ) -> list[OrderState]: ...

    @abstractmethod
    async def close_position_market(self, position: PositionState, reason: str) -> OrderState: ...

    async def close_position_market_orders(
        self, position: PositionState, reason: str
    ) -> list[OrderState]:
        """Close a position and return every exchange order used.

        Gateways that need to split a market close because of exchange quantity
        limits should override this method.  The compatibility wrapper keeps
        existing test/fake gateways usable while allowing callers to persist all
        child orders in production.
        """
        return [await self.close_position_market(position, reason)]

    async def place_limit_exit(
        self,
        position: PositionState,
        quantity: Decimal,
        price: Decimal,
        operation_id: str,
    ) -> OrderState:
        raise NotImplementedError("exchange does not support limit exits")

    @abstractmethod
    async def close_position_quantity_market(
        self,
        position: PositionState,
        quantity: Decimal,
        operation_id: str,
    ) -> OrderState: ...

    async def close_position_quantity_market_orders(
        self,
        position: PositionState,
        quantity: Decimal,
        operation_id: str,
    ) -> list[OrderState]:
        """Close a quantity and return every child market order."""
        return [
            await self.close_position_quantity_market(position, quantity, operation_id)
        ]

    @abstractmethod
    async def cancel_position_take_profits(self, position: PositionState) -> None: ...

    @abstractmethod
    async def cancel_position_protection(self, position: PositionState) -> None: ...

    @abstractmethod
    async def tighten_stop(self, position: PositionState, new_stop: Decimal) -> OrderState: ...

    @abstractmethod
    async def cancel_all_entry_orders(self) -> None: ...

    async def cancel_orphan_protection_orders(
        self, active_positions: set[tuple[str, str]]
    ) -> int:
        del active_positions
        return 0
