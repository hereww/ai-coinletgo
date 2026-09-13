from __future__ import annotations

from decimal import Decimal

import pytest

from trading_system.domain.models import UniverseSymbol
from trading_system.orchestration.cycle import TradingCycle


class FailingExchange:
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[object]:
        raise RuntimeError(f"{interval} unavailable")

    async def get_open_interest(self, symbol: str) -> Decimal:
        raise RuntimeError("open interest unavailable")

    async def get_book_depth(self, symbol: str) -> Decimal:
        raise RuntimeError("book depth unavailable")


class MemoryRedis:
    async def get(self, key: str) -> None:
        raise AssertionError("Redis cache should not be touched when requests fail")

    async def set(self, key: str, value: str, *, ex: int) -> None:
        raise AssertionError("Redis cache should not be touched when requests fail")


class AuditRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def audit(self, **event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_snapshot_failures_are_audited_per_symbol_and_stage() -> None:
    cycle = TradingCycle.__new__(TradingCycle)
    repository = AuditRepository()
    cycle.market_exchange = FailingExchange()
    cycle.redis = MemoryRedis()
    cycle.repository = repository
    universe = [
        UniverseSymbol(
            symbol="BTCUSDT",
            status="TRADING",
            listing_days=100,
            quote_volume_24h=Decimal("1000000"),
            best_bid=Decimal("99"),
            best_ask=Decimal("101"),
            mark_price=Decimal("100"),
            index_price=Decimal("100"),
            funding_rate=Decimal("0"),
        )
    ]

    snapshots = await cycle._build_snapshots(universe)

    assert snapshots == []
    assert [event["resource"] for event in repository.events] == [
        "BTCUSDT",
        "BTCUSDT",
        "BTCUSDT",
        "BTCUSDT",
        "BTCUSDT",
    ]
    assert [event["detail"]["stage"] for event in repository.events] == [
        "candles_15m",
        "candles_1h",
        "candles_4h",
        "open_interest",
        "book_depth",
    ]
    assert all(event["action"] == "market_data_snapshot_failed" for event in repository.events)
    assert all(event["detail"]["reason_zh"] for event in repository.events)
