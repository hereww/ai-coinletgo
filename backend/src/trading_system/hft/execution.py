from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_system.hft.models import HftExecutionSnapshot, HftFill, HftSignal


@dataclass
class _Position:
    quantity: Decimal = Decimal("0")
    average_entry: Decimal = Decimal("0")


class HftPaperExecutor:
    """A deterministic taker-style paper executor for shadow validation."""

    def __init__(self, *, fee_rate: Decimal = Decimal("0.0004")) -> None:
        self.fee_rate = fee_rate
        self._positions: dict[str, _Position] = {}
        self.realized_pnl_usdt = Decimal("0")
        self.fees_usdt = Decimal("0")
        self.fill_count = 0

    def inventory_qty(self, symbol: str) -> Decimal:
        return self._positions.get(symbol.upper(), _Position()).quantity

    def inventory_notional_usdt(self, symbol: str, price: Decimal) -> Decimal:
        return abs(self.inventory_qty(symbol) * price)

    def execute(self, signal: HftSignal, quantity: Decimal) -> HftFill:
        action = signal.action
        if action == "HOLD":
            raise ValueError("paper executor requires BUY or SELL signal")
        if quantity <= 0:
            raise ValueError("paper fill quantity must be positive")
        symbol = signal.symbol.upper()
        position = self._positions.setdefault(symbol, _Position())
        price = signal.book.best_ask if action == "BUY" else signal.book.best_bid
        signed_quantity = quantity if action == "BUY" else -quantity
        inventory_before = position.quantity
        average_before = position.average_entry
        close_quantity = Decimal("0")
        gross_realized = Decimal("0")
        if inventory_before != 0 and inventory_before * signed_quantity < 0:
            close_quantity = min(abs(inventory_before), abs(signed_quantity))
            gross_realized = (
                (price - average_before) * close_quantity
                if inventory_before > 0
                else (average_before - price) * close_quantity
            )

        inventory_after = inventory_before + signed_quantity
        if inventory_before == 0 or inventory_before * signed_quantity > 0:
            total_quantity = abs(inventory_before) + abs(signed_quantity)
            position.average_entry = (
                (abs(inventory_before) * average_before + abs(signed_quantity) * price)
                / total_quantity
            )
        elif inventory_after == 0:
            position.average_entry = Decimal("0")
        elif inventory_after * inventory_before < 0:
            position.average_entry = price
        position.quantity = inventory_after

        notional = abs(price * quantity)
        fee = notional * self.fee_rate
        net_pnl = gross_realized - fee
        self.realized_pnl_usdt += net_pnl
        self.fees_usdt += fee
        self.fill_count += 1
        return HftFill(
            symbol=symbol,
            action=action,
            quantity=quantity,
            price=price,
            notional_usdt=notional,
            fee_usdt=fee,
            closed_quantity=close_quantity,
            gross_realized_pnl_usdt=gross_realized,
            realized_pnl_usdt=net_pnl,
            inventory_before=inventory_before,
            inventory_after=inventory_after,
            average_entry_after=position.average_entry,
        )

    def snapshot(self, symbol: str, mark_price: Decimal) -> HftExecutionSnapshot:
        symbol_key = symbol.upper()
        position = self._positions.get(symbol_key, _Position())
        unrealized = self._unrealized(position, mark_price)
        return HftExecutionSnapshot(
            inventory_qty=position.quantity,
            inventory_notional_usdt=abs(position.quantity * mark_price),
            average_entry_price=position.average_entry,
            realized_pnl_usdt=self.realized_pnl_usdt,
            unrealized_pnl_usdt=unrealized,
            fees_usdt=self.fees_usdt,
            total_pnl_usdt=self.realized_pnl_usdt + unrealized,
            fills=self.fill_count,
        )

    def aggregate_snapshot(self, marks: dict[str, Decimal]) -> HftExecutionSnapshot:
        unrealized = Decimal("0")
        inventory_notional = Decimal("0")
        inventory_qty = Decimal("0")
        for symbol, position in self._positions.items():
            mark = marks.get(symbol)
            if mark is None:
                continue
            unrealized += self._unrealized(position, mark)
            inventory_notional += abs(position.quantity * mark)
            inventory_qty += position.quantity
        return HftExecutionSnapshot(
            inventory_qty=inventory_qty,
            inventory_notional_usdt=inventory_notional,
            average_entry_price=Decimal("0"),
            realized_pnl_usdt=self.realized_pnl_usdt,
            unrealized_pnl_usdt=unrealized,
            fees_usdt=self.fees_usdt,
            total_pnl_usdt=self.realized_pnl_usdt + unrealized,
            fills=self.fill_count,
        )

    @staticmethod
    def _unrealized(position: _Position, mark_price: Decimal) -> Decimal:
        if position.quantity > 0:
            return (mark_price - position.average_entry) * position.quantity
        if position.quantity < 0:
            return (position.average_entry - mark_price) * abs(position.quantity)
        return Decimal("0")
