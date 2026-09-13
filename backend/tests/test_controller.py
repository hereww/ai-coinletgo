from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trading_system.api.controller import SystemController
from trading_system.domain.enums import HealthState, SystemMode
from trading_system.domain.models import HealthComponent


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
    service.settings = SimpleNamespace(
        binance_environment="testnet",
        model_strategy_enabled=True,
        portfolio_strategy_enabled=False,
    )
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


@pytest.mark.asyncio
async def test_queue_cycle_allows_local_strategy_without_model_relay() -> None:
    service, repository, redis = controller(mode=SystemMode.TESTNET)
    service.settings.model_strategy_enabled = False
    service.model.configured = False

    operation_id = await service.queue_cycle()

    assert operation_id.startswith("manual-cycle-")
    assert repository.mode == SystemMode.TESTNET
    assert "trading-cycle:manual-request" in redis.values


@pytest.mark.asyncio
async def test_queue_cycle_keeps_portfolio_strategy_model_requirement() -> None:
    service, repository, redis = controller(mode=SystemMode.TESTNET)
    service.settings.model_strategy_enabled = False
    service.settings.portfolio_strategy_enabled = True
    service.model.configured = False

    with pytest.raises(ValueError, match="model relay is not configured"):
        await service.queue_cycle()

    assert repository.mode == SystemMode.TESTNET
    assert redis.values == {}


@pytest.mark.asyncio
async def test_local_strategy_health_gate_does_not_require_model_relay() -> None:
    service, _, _ = controller(mode=SystemMode.TESTNET)
    service.settings.model_strategy_enabled = False
    service.settings.portfolio_strategy_enabled = False
    service._database_health = AsyncMock(return_value=_health_component("database"))
    service._redis_health = AsyncMock(return_value=_health_component("redis"))
    service._exchange_health = AsyncMock(return_value=_health_component("binance"))
    service._model_health = AsyncMock(
        return_value=_health_component("model_relay", HealthState.NOT_CONFIGURED)
    )
    service._auth_health = lambda: _health_component("auth")

    components = await service._health_components(deep=False)

    assert [component.name for component in components] == [
        "database",
        "redis",
        "binance",
        "model_relay",
        "auth",
    ]
    assert service._health_ready(components) is True


@pytest.mark.asyncio
async def test_portfolio_health_gate_still_requires_model_relay() -> None:
    service, _, _ = controller(mode=SystemMode.TESTNET)
    service.settings.model_strategy_enabled = False
    service.settings.portfolio_strategy_enabled = True
    service._database_health = AsyncMock(return_value=_health_component("database"))
    service._redis_health = AsyncMock(return_value=_health_component("redis"))
    service._exchange_health = AsyncMock(return_value=_health_component("binance"))
    service._model_health = AsyncMock(
        return_value=_health_component("model_relay", HealthState.NOT_CONFIGURED)
    )
    service._auth_health = lambda: _health_component("auth")

    components = await service._health_components(deep=False)

    assert [component.name for component in components] == [
        "database",
        "redis",
        "binance",
        "model_relay",
        "auth",
    ]
    assert service._health_ready(components) is False


def _health_component(
    name: str, state: HealthState = HealthState.HEALTHY
) -> HealthComponent:
    return HealthComponent(name=name, state=state)
