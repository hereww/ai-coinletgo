from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trading_system.api.controller import SystemController
from trading_system.orchestration.cycle import CycleResult, TradingCycle


class StatusRedis:
    def __init__(self, *, lock: bool = False, active: bool = False) -> None:
        self.lock = lock
        self.active = active
        self.deleted: list[str] = []
        self.status: dict[str, object] | None = None
        self.heartbeat: str | None = None

    async def set(self, key: str, value: str, *, ex: int) -> None:
        del ex
        if key == "trading-cycle:last-status":
            self.status = json.loads(value)

    async def exists(self, key: str) -> int:
        return int(
            (key == "trading-cycle" and self.lock)
            or (key == "trading-cycle:active" and self.active)
        )

    async def get(self, key: str) -> str | None:
        if key == "trading-cycle:last-status" and self.status is not None:
            return json.dumps(self.status)
        if key == "worker:heartbeat":
            return self.heartbeat
        return None

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        if key == "trading-cycle":
            self.lock = False
        return 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "state"),
    [
        ("another worker owns the cycle lock", "WORKER_BUSY"),
        ("position reconciliation required", "BLOCKED_RECONCILIATION"),
        (
            "position reconciliation pending; manual operator action required",
            "BLOCKED_RECONCILIATION",
        ),
    ],
)
async def test_cycle_status_distinguishes_non_model_blockers(
    detail: str, state: str
) -> None:
    redis = StatusRedis()
    cycle = TradingCycle.__new__(TradingCycle)
    cycle.redis = redis
    result = await cycle._finish_cycle(
        CycleResult(detail=detail), datetime.now(UTC)
    )

    assert result.detail == detail
    assert redis.status is not None
    assert redis.status["state"] == state


@pytest.mark.asyncio
async def test_cycle_status_distinguishes_exchange_unavailable() -> None:
    redis = StatusRedis()
    cycle = TradingCycle.__new__(TradingCycle)
    cycle.redis = redis
    result = await cycle._finish_cycle(
        CycleResult(
            exchange_unavailable=True,
            detail="交易所请求失败，本轮未调用模型：Binance REST backoff active",
        ),
        datetime.now(UTC),
    )

    assert result.detail.startswith("交易所请求失败")
    assert redis.status is not None
    assert redis.status["state"] == "EXCHANGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_stale_cycle_lock_is_cleared_without_active_marker() -> None:
    redis = StatusRedis(lock=True)
    redis.status = {"state": "COMPLETED"}
    cycle = TradingCycle.__new__(TradingCycle)
    cycle.redis = redis

    await cycle._recover_stale_cycle_lock()

    assert redis.deleted == ["trading-cycle"]


@pytest.mark.asyncio
async def test_active_cycle_lock_is_never_cleared() -> None:
    redis = StatusRedis(lock=True, active=True)
    cycle = TradingCycle.__new__(TradingCycle)
    cycle.redis = redis

    await cycle._recover_stale_cycle_lock()

    assert redis.deleted == []


@pytest.mark.asyncio
async def test_stale_running_cycle_is_marked_worker_interrupted() -> None:
    redis = StatusRedis()
    redis.status = {
        "state": "RUNNING",
        "detail": "交易周期正在执行行情检查与组合决策",
        "started_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "finished_at": None,
        "snapshots": 6,
        "candidates": 2,
        "signals": 0,
        "approved": 0,
        "executed": 0,
        "failed": False,
    }
    controller = SystemController.__new__(SystemController)
    controller.redis = redis
    controller.settings = SimpleNamespace(model_timeout_seconds=120)

    status = await controller._cycle_status()

    assert status["state"] == "WORKER_INTERRUPTED"
    assert status["failed"] is True


@pytest.mark.asyncio
async def test_running_cycle_with_fresh_worker_heartbeat_stays_running() -> None:
    redis = StatusRedis()
    redis.status = {
        "state": "RUNNING",
        "detail": "交易周期正在执行行情检查与组合决策",
        "started_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "finished_at": None,
        "snapshots": 6,
        "candidates": 2,
        "signals": 0,
        "approved": 0,
        "executed": 0,
        "failed": False,
    }
    redis.heartbeat = datetime.now(UTC).isoformat()
    controller = SystemController.__new__(SystemController)
    controller.redis = redis
    controller.settings = SimpleNamespace(model_timeout_seconds=120)

    status = await controller._cycle_status()

    assert status["state"] == "RUNNING"
