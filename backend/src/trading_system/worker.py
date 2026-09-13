from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime

from redis.asyncio import Redis

from trading_system.ai.client import ResponsesModelClient
from trading_system.config import get_settings
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.exchange.market_stream import (
    BinanceMarketStream,
    BinancePublicMarketCache,
    BinanceUserDataStream,
)
from trading_system.hft.runner import HftRunner
from trading_system.notifications.reports import TelegramReportScheduler
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.orchestration.cycle import TradingCycle
from trading_system.orchestration.protection import PositionProtectionMonitor
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trading-worker")


def seconds_until_next_cycle(
    interval_minutes: int = 5, *, now: datetime | None = None
) -> float:
    """Return seconds to the next epoch-aligned interval boundary plus five seconds."""
    current = now or datetime.now(UTC)
    interval_seconds = interval_minutes * 60
    current_seconds = current.timestamp()
    next_run = ((int(current_seconds - 5) // interval_seconds) + 1) * interval_seconds + 5
    return max(1.0, next_run - current_seconds)


async def run_worker() -> None:
    settings = get_settings()
    database = Database(settings.database_connection_url)
    if settings.app_env != "production":
        await database.create_schema()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    repository = Repository(database, settings.app_timezone)
    await repository.apply_runtime_config(settings)
    exchange = BinanceUSDMarketClient(settings)
    market_exchange = BinanceUSDMarketClient(
        settings,
        public_base_url=settings.binance_live_base_url,
        # Model research uses the production public market-data API. Account,
        # position, protection, and execution requests remain on testnet, so
        # scanning cannot consume the trading API's REST rate-limit bucket.
        proxy_url=settings.http_proxy_url,
    )
    trading_market = BinancePublicMarketCache(
        settings.binance_ws_url,
        proxy_url=settings.binance_http_proxy_url,
    )
    analysis_market = BinancePublicMarketCache(
        settings.binance_ws_live_url,
        proxy_url=settings.http_proxy_url,
    )
    exchange.attach_market_cache(trading_market)
    market_exchange.attach_market_cache(analysis_market)
    if exchange.configured and (
        not settings.binance_proxy_enabled or settings.binance_http_proxy_configured
    ):
        trading_market.start()
    if not settings.http_proxy_enabled or settings.http_proxy_configured:
        analysis_market.start()
    model = ResponsesModelClient(settings)
    notifier = TelegramNotifier(settings)
    cycle = TradingCycle(
        settings,
        redis,
        repository,
        exchange,
        model,
        notifier,
        market_exchange=market_exchange,
    )
    protection = PositionProtectionMonitor(
        settings, repository, exchange, notifier, redis=redis
    )
    reports = TelegramReportScheduler(settings, repository, exchange, notifier)
    logger.info("worker started in %s mode", settings.binance_environment)
    protection_task = asyncio.create_task(
        _supervise(
            "position-protection-monitor",
            protection.run_forever,
            notifier,
        ),
        name="supervisor-position-protection-monitor",
    )
    report_task = asyncio.create_task(
        _supervise("telegram-report-scheduler", reports.run_forever, notifier),
        name="supervisor-telegram-report-scheduler",
    )
    heartbeat_task = asyncio.create_task(_heartbeat(redis), name="worker-heartbeat")
    user_stream = (
        BinanceUserDataStream(
            settings.binance_base_url,
            settings.binance_ws_url,
            exchange.api_key,
            proxy_url=settings.binance_http_proxy_url,
        )
        if exchange.configured
        and (not settings.binance_proxy_enabled or settings.binance_http_proxy_configured)
        else None
    )
    user_stream_task = (
        asyncio.create_task(
            _supervise(
                "binance-user-data-stream",
                lambda: _watch_user_stream(user_stream, protection),
                notifier,
            ),
            name="supervisor-binance-user-data-stream",
        )
        if user_stream is not None
        else None
    )
    user_stream_heartbeat_task = (
        asyncio.create_task(
            _user_stream_heartbeat(redis, user_stream),
            name="worker-user-stream-heartbeat",
        )
        if user_stream is not None
        else None
    )
    hft_stream = (
        BinanceMarketStream(
            settings.binance_ws_url,
            proxy_url=settings.binance_http_proxy_url,
        )
        if settings.hft_enabled
        and settings.binance_environment == "testnet"
        and exchange.configured
        and (not settings.binance_proxy_enabled or settings.binance_http_proxy_configured)
        else None
    )
    hft_runner = (
        HftRunner(settings, hft_stream, exchange, redis)
        if hft_stream is not None
        else None
    )
    hft_task = (
        asyncio.create_task(
            _supervise(
                "hft-shadow-runner",
                hft_runner.run_forever,
                notifier,
            ),
            name="supervisor-hft-shadow-runner",
        )
        if hft_runner is not None
        else None
    )
    first_cycle = True
    try:
        while True:
            if not first_cycle:
                await _wait_for_cycle(redis, settings=settings, repository=repository)
            first_cycle = False
            started = asyncio.get_running_loop().time()
            try:
                result = await cycle.run()
                logger.info(
                    "cycle finished duration_seconds=%.2f snapshots=%d candidates=%d "
                    "signals=%d approved=%d executed=%d detail=%s",
                    asyncio.get_running_loop().time() - started,
                    result.snapshots,
                    result.candidates,
                    result.signals,
                    result.approved,
                    result.executed,
                    result.detail,
                )
            except Exception:
                logger.exception("trading cycle failed")
                await notifier.send("Worker 异常", "交易循环发生未处理错误，本轮未新增风险。")
    finally:
        protection_task.cancel()
        report_task.cancel()
        if user_stream_task is not None:
            user_stream_task.cancel()
        if user_stream_heartbeat_task is not None:
            user_stream_heartbeat_task.cancel()
        if hft_runner is not None:
            hft_runner.stop()
        if hft_task is not None:
            hft_task.cancel()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await protection_task
        with suppress(asyncio.CancelledError):
            await report_task
        if user_stream_task is not None:
            with suppress(asyncio.CancelledError):
                await user_stream_task
        if user_stream_heartbeat_task is not None:
            with suppress(asyncio.CancelledError):
                await user_stream_heartbeat_task
        if hft_task is not None:
            with suppress(asyncio.CancelledError):
                await hft_task
        await analysis_market.close()
        await trading_market.close()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        if user_stream is not None:
            await user_stream.close()
        await notifier.close()
        await model.close()
        await market_exchange.close()
        await exchange.close()
        await redis.aclose()
        await database.dispose()


async def _wait_for_cycle(
    redis: Redis,
    *,
    settings: object | None = None,
    repository: Repository | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    interval = int(getattr(settings, "scan_interval_minutes", 5))
    deadline = loop.time() + seconds_until_next_cycle(interval)
    next_reload = loop.time()
    logger.info(
        "next scheduled trading cycle interval_minutes=%d wait_seconds=%.1f",
        interval,
        max(0.0, deadline - loop.time()),
    )
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            requested = await redis.get("trading-cycle:manual-request")
            if requested:
                if await _manual_request_is_in_current_cadence(redis, interval):
                    await redis.delete("trading-cycle:manual-request")
                    logger.info(
                        "manual trading cycle coalesced into the current scheduled cadence: %s",
                        requested,
                    )
                    continue
                await redis.delete("trading-cycle:manual-request")
                logger.info("manual trading cycle requested: %s", requested)
                return
        except Exception:
            logger.exception("manual cycle request check failed")
        if settings is not None and repository is not None and loop.time() >= next_reload:
            next_reload = loop.time() + 30
            try:
                await repository.apply_runtime_config(settings)  # type: ignore[arg-type]
                configured_interval = int(getattr(settings, "scan_interval_minutes", interval))
                if configured_interval != interval:
                    previous = interval
                    interval = configured_interval
                    deadline = loop.time() + seconds_until_next_cycle(interval)
                    logger.info(
                        "scan interval reloaded previous_minutes=%d interval_minutes=%d "
                        "wait_seconds=%.1f",
                        previous,
                        interval,
                        max(0.0, deadline - loop.time()),
                    )
            except Exception:
                logger.exception("runtime scan interval reload failed; keeping current schedule")
        await asyncio.sleep(min(2.0, remaining))


async def _manual_request_is_in_current_cadence(redis: Redis, interval: int) -> bool:
    """Coalesce manual requests that would duplicate the current configured slot."""
    try:
        raw = await redis.get("trading-cycle:model-last-slot")
        if raw is None:
            return False
        window = max(5, int(interval)) * 60
        current_slot = int((time.time() - 5) // window)
        return int(raw) == current_slot
    except (TypeError, ValueError):
        return False
    except Exception:
        logger.exception("manual cadence state check failed; preserving manual request")
        return False


async def _watch_user_stream(
    stream: BinanceUserDataStream, protection: PositionProtectionMonitor
) -> None:
    async for event in stream.events():
        if event.get("e") in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"}:
            await protection.run_once(source="event")


async def _supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    notifier: TelegramNotifier,
) -> None:
    """Restart a failed auxiliary loop while keeping the main worker alive."""
    while True:
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("%s stopped unexpectedly", name)
            with suppress(Exception):
                await notifier.send("Worker 子任务异常", f"{name}: {type(error).__name__}")
            await asyncio.sleep(5)
        else:
            with suppress(Exception):
                await notifier.send("Worker 子任务退出", f"{name} 已退出，准备重启")
            await asyncio.sleep(1)


async def _heartbeat(redis: Redis) -> None:
    while True:
        try:
            await redis.set("worker:heartbeat", datetime.now(UTC).isoformat(), ex=60)
        except Exception:
            logger.exception("worker heartbeat update failed")
        await asyncio.sleep(15)


async def _user_stream_heartbeat(redis: Redis, stream: BinanceUserDataStream) -> None:
    while True:
        try:
            state = "connected" if stream.connected else "disconnected"
            await redis.set("worker:user-stream", state, ex=60)
        except Exception:
            logger.exception("user stream heartbeat update failed")
        await asyncio.sleep(15)


if __name__ == "__main__":
    asyncio.run(run_worker())
