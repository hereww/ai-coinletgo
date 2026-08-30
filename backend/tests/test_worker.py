from __future__ import annotations

from datetime import UTC, datetime

import pytest

import trading_system.worker as worker
from trading_system.worker import (
    _manual_request_is_in_current_cadence,
    _wait_for_cycle,
    seconds_until_next_cycle,
)


class ManualRequestRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def get(self, key: str) -> str | None:
        return "manual-cycle-1" if key == "trading-cycle:manual-request" else None

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.mark.asyncio
async def test_manual_cycle_request_wakes_worker_without_waiting_for_boundary() -> None:
    redis = ManualRequestRedis()
    await _wait_for_cycle(redis)  # type: ignore[arg-type]
    assert redis.deleted == ["trading-cycle:manual-request"]


def test_cycle_schedule_uses_custom_epoch_aligned_interval() -> None:
    now = datetime(2026, 8, 23, 8, 14, 0, tzinfo=UTC)
    assert seconds_until_next_cycle(15, now=now) == 65
    assert seconds_until_next_cycle(30, now=now) == 16 * 60 + 5


@pytest.mark.asyncio
async def test_manual_request_is_coalesced_inside_current_model_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def get(self, key: str) -> str | None:
            assert key == "trading-cycle:model-last-slot"
            return "1920000"

    monkeypatch.setattr(worker.time, "time", lambda: 1920000 * 15 * 60 + 5)
    assert await _manual_request_is_in_current_cadence(Redis(), 15) is True  # type: ignore[arg-type]
