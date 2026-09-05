from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_system.hft.models import HftBookMetrics


class OrderBookGapError(RuntimeError):
    """The diff stream no longer has a contiguous update sequence."""


class DiffOrderBook:
    """In-memory Binance diff book synchronized from a REST snapshot."""

    def __init__(self, symbol: str, *, depth_levels: int = 10) -> None:
        self.symbol = symbol.upper()
        self.depth_levels = max(1, depth_levels)
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self._has_applied_event = False
        self.event_time_ms = 0
        self.received_monotonic = 0.0

    @property
    def synchronized(self) -> bool:
        return self.last_update_id is not None

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self._has_applied_event = False
        self.event_time_ms = 0
        self.received_monotonic = 0.0

    def apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        update_id = int(snapshot["lastUpdateId"])
        self.bids = self._levels(snapshot.get("bids", []))
        self.asks = self._levels(snapshot.get("asks", []))
        self.last_update_id = update_id
        self._has_applied_event = False
        self.event_time_ms = 0

    def apply_event(self, event: dict[str, Any], received_monotonic: float) -> str:
        """Apply one event and return ``applied``, ``stale``, or ``gap``.

        Binance requires the first event to bridge ``lastUpdateId + 1`` and
        every later event to have ``pu`` equal to the previous event's ``u``.
        Returning a gap lets the runner fetch a fresh snapshot before taking
        another trading decision.
        """

        symbol = str(event.get("s", self.symbol)).upper()
        if symbol != self.symbol:
            return "ignored"
        try:
            first_id = int(event["U"])
            final_id = int(event["u"])
        except (KeyError, TypeError, ValueError):
            return "ignored"
        if final_id < first_id:
            return "ignored"
        if self.last_update_id is None:
            return "not_ready"
        if final_id <= self.last_update_id:
            return "stale"

        previous_id = self.last_update_id
        previous_event_id = event.get("pu")
        if self._has_applied_event and previous_event_id is None:
            return "gap"
        if previous_event_id is not None:
            try:
                previous_event_id = int(previous_event_id)
            except (TypeError, ValueError):
                return "gap"

        if self._has_applied_event and previous_event_id != previous_id:
            return "gap"
        if first_id > previous_id + 1 or not (first_id <= previous_id + 1 <= final_id):
            return "gap"

        self._apply_updates(self.bids, event.get("b", []))
        self._apply_updates(self.asks, event.get("a", []))
        self.last_update_id = final_id
        self._has_applied_event = True
        self.event_time_ms = int(event.get("E") or event.get("T") or 0)
        self.received_monotonic = received_monotonic
        return "applied"

    def metrics(self) -> HftBookMetrics:
        if self.last_update_id is None:
            raise ValueError(f"{self.symbol} order book is not synchronized")
        if not self.bids or not self.asks:
            raise ValueError(f"{self.symbol} order book has no two-sided quote")
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        if best_bid <= 0 or best_ask <= 0 or best_bid > best_ask:
            raise ValueError(f"{self.symbol} order book quote is invalid")
        midpoint = (best_bid + best_ask) / Decimal("2")
        bid_depth = sum(
            (
                price * quantity
                for price, quantity in sorted(self.bids.items(), reverse=True)[: self.depth_levels]
            ),
            Decimal("0"),
        )
        ask_depth = sum(
            (
                price * quantity
                for price, quantity in sorted(self.asks.items())[: self.depth_levels]
            ),
            Decimal("0"),
        )
        total_depth = bid_depth + ask_depth
        imbalance = (
            (bid_depth - ask_depth) / total_depth if total_depth > 0 else Decimal("0")
        )
        microprice = (
            (best_ask * bid_depth + best_bid * ask_depth) / total_depth
            if total_depth > 0
            else midpoint
        )
        return HftBookMetrics(
            symbol=self.symbol,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=midpoint,
            microprice=microprice,
            spread_pct=(best_ask - best_bid) / midpoint,
            bid_depth_usdt=bid_depth,
            ask_depth_usdt=ask_depth,
            imbalance=imbalance,
            last_update_id=self.last_update_id,
            event_time_ms=max(0, self.event_time_ms),
            received_monotonic=max(0.0, self.received_monotonic),
        )

    @staticmethod
    def _levels(raw_levels: object) -> dict[Decimal, Decimal]:
        result: dict[Decimal, Decimal] = {}
        if not isinstance(raw_levels, Iterable) or isinstance(raw_levels, (str, bytes)):
            return result
        for row in raw_levels:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                price = Decimal(str(row[0]))
                quantity = Decimal(str(row[1]))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price > 0 and quantity > 0:
                result[price] = quantity
        return result

    @staticmethod
    def _apply_updates(book: dict[Decimal, Decimal], raw_levels: object) -> None:
        if not isinstance(raw_levels, Iterable) or isinstance(raw_levels, (str, bytes)):
            return
        for row in raw_levels:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            try:
                price = Decimal(str(row[0]))
                quantity = Decimal(str(row[1]))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            if quantity <= 0:
                book.pop(price, None)
            else:
                book[price] = quantity
