from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from redis.asyncio import Redis

from trading_system.config import Settings
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.exchange.market_stream import BinanceMarketStream
from trading_system.hft.execution import HftPaperExecutor
from trading_system.hft.models import HftFill, HftStatus
from trading_system.hft.order_book import DiffOrderBook
from trading_system.hft.risk import HftRiskManager
from trading_system.hft.signal import HftSignalEngine

logger = logging.getLogger("trading-worker.hft")
HftState = Literal["IDLE", "CONNECTING", "READY", "RUNNING", "BLOCKED", "DEGRADED"]


class HftRunner:
    def __init__(
        self,
        settings: Settings,
        stream: BinanceMarketStream,
        exchange: BinanceUSDMarketClient,
        redis: Redis,
    ) -> None:
        self.settings = settings
        self.stream = stream
        self.exchange = exchange
        self.redis = redis
        self.symbols = [symbol.upper() for symbol in settings.hft_symbols]
        self.books = {symbol: DiffOrderBook(symbol) for symbol in self.symbols}
        self.signal_engine = HftSignalEngine(
            imbalance_threshold=Decimal(str(settings.hft_imbalance_threshold)),
            max_spread_pct=Decimal(str(settings.hft_max_spread_pct)),
            min_depth_usdt=Decimal(str(settings.hft_min_depth_usdt)),
        )
        self.risk = HftRiskManager(
            max_inventory_usdt=Decimal(str(settings.hft_max_inventory_usdt)),
            cooldown_seconds=float(settings.hft_cooldown_seconds),
            market_stale_seconds=float(settings.hft_market_stale_seconds),
            max_consecutive_losses=settings.hft_max_consecutive_losses,
        )
        self.executor = HftPaperExecutor()
        self.last_processed_monotonic: dict[str, float] = {}
        self._stopped = False
        self._marks: dict[str, Decimal] = {}
        self._last_detail = "未连接盘口流"

    def stop(self) -> None:
        self._stopped = True
        self.stream.stop()

    async def run_forever(self) -> None:
        if self.settings.binance_environment != "testnet":
            await self._set_status("BLOCKED", "HFT 仅允许在 Binance 测试网运行")
            await self._idle_forever()
            return
        if not self.settings.hft_dry_run:
            await self._set_status("BLOCKED", "HFT dry-run 已关闭，系统拒绝任何真实下单")
            await self._idle_forever()
            return
        if not self.symbols:
            await self._set_status("IDLE", "未配置 HFT 合约")
            await self._idle_forever()
            return

        await self._set_status("CONNECTING", "等待 Binance Futures 盘口差分流")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        collector = asyncio.create_task(self._collect(queue), name="hft-depth-collector")
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="hft-heartbeat")
        try:
            while not self._stopped:
                item = await queue.get()
                if item.get("_stream_event") == "connected":
                    self._reset_books()
                    synced = await self._synchronize_books()
                    detail = f"盘口已连接，完成 {synced}/{len(self.symbols)} 个快照同步"
                    await self._set_status("READY" if synced else "DEGRADED", detail)
                    continue
                if item.get("_stream_event") == "stopped":
                    return
                await self.process_depth_event(item)
        finally:
            collector.cancel()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await collector
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def process_depth_event(self, event: dict[str, Any]) -> HftFill | None:
        symbol = str(event.get("s", "")).upper()
        book = self.books.get(symbol)
        if book is None:
            return None
        status = book.apply_event(event, time.monotonic())
        if status == "gap":
            book.reset()
            await self._set_status("DEGRADED", f"{symbol} 盘口断档，正在重新同步快照")
            await self._synchronize_symbol(symbol)
            return None
        if status != "applied":
            return None
        self._marks[symbol] = book.metrics().mid_price
        min_interval = float(self.settings.hft_event_interval_ms) / 1000
        now = time.monotonic()
        previous = self.last_processed_monotonic.get(symbol)
        if previous is not None and now - previous < min_interval:
            return None
        self.last_processed_monotonic[symbol] = now
        self._refresh_limits()
        signal = self.signal_engine.evaluate(book)
        decision = self.risk.check(
            signal,
            inventory_qty=self.executor.inventory_qty(symbol),
            desired_notional_usdt=Decimal(str(self.settings.hft_order_notional_usdt)),
            now_monotonic=now,
        )
        if not decision.allowed:
            self._last_detail = f"{symbol}: {decision.reason}"
            return None
        fill = self.executor.execute(signal, decision.quantity)
        if fill.closed_quantity > 0:
            self.risk.record_fill(
                closed_quantity=fill.closed_quantity,
                realized_pnl_usdt=fill.realized_pnl_usdt,
            )
        else:
            self.risk.record_opening_fill()
        self._last_detail = (
            f"{symbol} {fill.action} {fill.quantity.normalize()} @ {fill.price.normalize()}"
        )
        await self._set_status("RUNNING", self._last_detail)
        return fill

    async def _collect(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            async for event in self.stream.depth_events(self.symbols):
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("HFT depth collector stopped")
            await queue.put({"_stream_event": "stopped"})

    async def _synchronize_books(self) -> int:
        results = await asyncio.gather(
            *(self._synchronize_symbol(symbol) for symbol in self.symbols),
            return_exceptions=True,
        )
        return sum(result is True for result in results)

    async def _synchronize_symbol(self, symbol: str) -> bool:
        try:
            snapshot = await self.exchange.get_order_book_snapshot(symbol, limit=1_000)
            self.books[symbol].apply_snapshot(snapshot)
            return True
        except Exception as error:
            self._last_detail = f"{symbol} 快照同步失败：{type(error).__name__}"
            logger.warning(
                "HFT snapshot sync failed symbol=%s error=%s", symbol, type(error).__name__
            )
            return False

    def _reset_books(self) -> None:
        for book in self.books.values():
            book.reset()
        self.last_processed_monotonic.clear()

    def _refresh_limits(self) -> None:
        self.signal_engine.update_limits(
            imbalance_threshold=Decimal(str(self.settings.hft_imbalance_threshold)),
            max_spread_pct=Decimal(str(self.settings.hft_max_spread_pct)),
            min_depth_usdt=Decimal(str(self.settings.hft_min_depth_usdt)),
        )
        self.risk.update_limits(
            max_inventory_usdt=Decimal(str(self.settings.hft_max_inventory_usdt)),
            cooldown_seconds=float(self.settings.hft_cooldown_seconds),
            market_stale_seconds=float(self.settings.hft_market_stale_seconds),
            max_consecutive_losses=self.settings.hft_max_consecutive_losses,
        )

    async def _heartbeat_loop(self) -> None:
        while not self._stopped:
            try:
                await self.redis.set(
                    "hft:heartbeat", datetime.now(UTC).isoformat(), ex=60
                )
            except Exception:
                logger.debug("HFT heartbeat update failed", exc_info=True)
            await asyncio.sleep(15)

    async def _set_status(self, state: HftState, detail: str) -> None:
        snapshot = self.executor.aggregate_snapshot(self._marks)
        status = HftStatus(
            state=state,
            detail=detail[:400],
            dry_run=self.settings.hft_dry_run,
            symbols=self.symbols,
            last_update_id={
                symbol: book.last_update_id
                for symbol, book in self.books.items()
                if book.last_update_id is not None
            },
            fills=snapshot.fills,
            realized_pnl_usdt=snapshot.realized_pnl_usdt,
            unrealized_pnl_usdt=snapshot.unrealized_pnl_usdt,
            fees_usdt=snapshot.fees_usdt,
            consecutive_losses=self.risk.consecutive_losses,
        )
        try:
            await self.redis.set(
                "hft:status",
                json.dumps(status.model_dump(mode="json"), ensure_ascii=False),
                ex=60,
            )
        except Exception:
            logger.debug("HFT status update failed", exc_info=True)

    async def _idle_forever(self) -> None:
        while not self._stopped:
            try:
                await self.redis.set(
                    "hft:heartbeat", datetime.now(UTC).isoformat(), ex=60
                )
            except Exception:
                logger.debug("HFT idle heartbeat update failed", exc_info=True)
            await asyncio.sleep(30)
