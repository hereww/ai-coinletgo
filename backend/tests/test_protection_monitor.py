from decimal import Decimal

import pytest

from tests.factories import position, snapshot
from trading_system.config import Settings
from trading_system.domain.enums import SystemMode
from trading_system.exchange.base import ExchangeError
from trading_system.orchestration.protection import PositionProtectionMonitor


def test_one_r_moves_stop_to_fee_covered_break_even() -> None:
    current = position(
        entry_price=Decimal("100"),
        mark_price=Decimal("101.2"),
        stop_price=Decimal("99"),
        current_r=Decimal("1.2"),
    )
    assert PositionProtectionMonitor._managed_stop(current, snapshot()) == Decimal("100.1500")


def test_two_r_applies_atr_trailing_without_widening() -> None:
    current = position(
        entry_price=Decimal("100"),
        mark_price=Decimal("103"),
        stop_price=Decimal("100.15"),
        current_r=Decimal("3"),
    )
    assert PositionProtectionMonitor._managed_stop(
        current, snapshot(atr_15m=Decimal("1"))
    ) == Decimal("101.5")
    current.stop_price = Decimal("102")
    assert PositionProtectionMonitor._managed_stop(current, snapshot()) is None


class StubRepository:
    def __init__(self, known: set[tuple[str, str]]) -> None:
        self.known = known
        self.mode = SystemMode.TESTNET
        self.synced = None

    async def hydrate_positions(self, positions):
        return positions

    async def known_open_position_keys(self):
        return self.known

    async def get_mode(self, default, environment):
        del default, environment
        return self.mode

    async def set_mode(self, mode, *, halt_reason=None):
        del halt_reason
        self.mode = mode
        return mode

    async def sync_positions(self, positions):
        self.synced = positions


class StubExchange:
    configured = True

    def __init__(self, positions):
        self.positions = positions

    async def get_positions(self):
        return self.positions


class ExchangeWithOrphanCleanup(StubExchange):
    def __init__(self, positions):
        super().__init__(positions)
        self.orphan_cleanup_calls = 0

    async def cancel_orphan_protection_orders(self, active_positions):
        del active_positions
        self.orphan_cleanup_calls += 1
        return 0


class StubNotifier:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, title, body):
        self.messages.append((title, body))


class ActiveCycleRedis:
    async def exists(self, key):
        assert key == "trading-cycle"
        return 1


@pytest.mark.asyncio
async def test_unknown_exchange_position_requires_reconciliation() -> None:
    repository = StubRepository(set())
    notifier = StubNotifier()
    monitor = PositionProtectionMonitor(
        Settings(),
        repository,  # type: ignore[arg-type]
        StubExchange([position(protected=True)]),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
    )

    await monitor.run_once()

    assert repository.mode == SystemMode.RECONCILIATION_REQUIRED
    assert repository.synced is None
    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_unknown_exchange_position_is_deferred_while_cycle_is_active() -> None:
    repository = StubRepository(set())
    notifier = StubNotifier()
    monitor = PositionProtectionMonitor(
        Settings(),
        repository,  # type: ignore[arg-type]
        StubExchange([position(protected=True)]),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        redis=ActiveCycleRedis(),  # type: ignore[arg-type]
    )

    await monitor.run_once()

    assert repository.mode == SystemMode.TESTNET
    assert repository.synced is None
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_exchange_closed_position_is_synchronized_without_halt() -> None:
    repository = StubRepository({("ETHUSDT", "LONG")})
    monitor = PositionProtectionMonitor(
        Settings(),
        repository,  # type: ignore[arg-type]
        StubExchange([]),  # type: ignore[arg-type]
        StubNotifier(),  # type: ignore[arg-type]
    )

    await monitor.run_once()

    assert repository.mode == SystemMode.TESTNET
    assert repository.synced == []


@pytest.mark.asyncio
async def test_empty_account_orphan_cleanup_is_rate_limited() -> None:
    repository = StubRepository(set())
    exchange = ExchangeWithOrphanCleanup([])
    monitor = PositionProtectionMonitor(
        Settings(),
        repository,  # type: ignore[arg-type]
        exchange,  # type: ignore[arg-type]
        StubNotifier(),  # type: ignore[arg-type]
        orphan_cleanup_interval_seconds=300,
    )

    await monitor.run_once()
    await monitor.run_once()

    assert exchange.orphan_cleanup_calls == 1


@pytest.mark.asyncio
async def test_stop_adjustment_failure_is_cooled_down_without_freezing_entries() -> None:
    current = position(
        entry_price=Decimal("100"),
        mark_price=Decimal("101.2"),
        stop_price=Decimal("99"),
        current_r=Decimal("1.2"),
        protected=True,
    )

    class RepositoryWithSnapshots(StubRepository):
        async def latest_market(self, limit):
            del limit
            return [snapshot(symbol=current.symbol).model_dump(mode="json")]

        async def save_orders(self, orders):
            raise AssertionError(f"failed adjustment must not save orders: {orders}")

    class FailingExchange(StubExchange):
        def __init__(self, positions):
            super().__init__(positions)
            self.tighten_calls = 0

        async def tighten_stop(self, current_position, new_stop):
            del current_position, new_stop
            self.tighten_calls += 1
            raise ExchangeError(
                "400 [-2021]: Order would immediately trigger.",
                code=-2021,
                http_status=400,
            )

        async def cancel_orphan_protection_orders(self, active_positions):
            del active_positions
            return 0

    repository = RepositoryWithSnapshots({(current.symbol, current.side.value)})
    exchange = FailingExchange([current])
    notifier = StubNotifier()
    monitor = PositionProtectionMonitor(
        Settings(),
        repository,  # type: ignore[arg-type]
        exchange,  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        failure_cooldown_seconds=300,
    )

    await monitor.run_once()
    await monitor.run_once()

    assert exchange.tighten_calls == 1
    assert repository.mode == SystemMode.TESTNET
    assert notifier.messages == []
