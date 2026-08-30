from types import SimpleNamespace

import pytest

from trading_system.api.controller import SystemController
from trading_system.domain.enums import SystemMode


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True


class FakeRepository:
    def __init__(self, mode: SystemMode) -> None:
        self.mode = mode
        self.set_mode_calls: list[tuple[SystemMode, str | None]] = []

    async def get_mode(self, default: SystemMode, environment: str) -> SystemMode:
        return self.mode

    async def set_mode(
        self, mode: SystemMode, *, halt_reason: str | None = None
    ) -> SystemMode:
        self.mode = mode
        self.set_mode_calls.append((mode, halt_reason))
        return mode


def controller(*, mode: SystemMode) -> tuple[SystemController, FakeRepository, FakeRedis]:
    repository = FakeRepository(mode)
    redis = FakeRedis()
    service = object.__new__(SystemController)
    service.settings = SimpleNamespace(binance_environment="testnet")
    service.exchange = SimpleNamespace(configured=True)
    service.model = SimpleNamespace(configured=True)
    service.repository = repository
    service.redis = redis
    return service, repository, redis


@pytest.mark.asyncio
async def test_queue_cycle_resumes_paused_testnet_before_queueing() -> None:
    service, repository, redis = controller(mode=SystemMode.PAUSED)

    operation_id = await service.queue_cycle()

    assert operation_id.startswith("manual-cycle-")
    assert repository.mode == SystemMode.TESTNET
    assert repository.set_mode_calls == [(SystemMode.TESTNET, None)]
    assert "trading-cycle:manual-request" in redis.values


@pytest.mark.asyncio
async def test_queue_cycle_keeps_risk_halted_testnet_blocked() -> None:
    service, repository, redis = controller(mode=SystemMode.RISK_HALTED)

    with pytest.raises(ValueError, match="RISK_HALTED"):
        await service.queue_cycle()

    assert repository.mode == SystemMode.RISK_HALTED
    assert redis.values == {}
