from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from redis.asyncio import Redis

from trading_system.config import Settings
from trading_system.domain.enums import PositionSide, SystemMode
from trading_system.domain.models import ExecutionIntent, MarketSnapshot, PositionState
from trading_system.exchange.base import ExchangeError
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.persistence.repository import Repository

logger = logging.getLogger("trading-worker.protection")


class PositionProtectionMonitor:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        exchange: BinanceUSDMarketClient,
        notifier: TelegramNotifier,
        *,
        redis: Redis | None = None,
        # Websocket order/account events are the fast path.  The periodic
        # sweep is a safety net and should not compete with strategy cycles or
        # dashboard reads for Binance's signed REST quota.
        interval_seconds: float = 60,
        failure_cooldown_seconds: float = 300,
        adjustment_cooldown_seconds: float = 60,
        orphan_cleanup_interval_seconds: float = 900,
        event_debounce_seconds: float = 15,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.exchange = exchange
        self.notifier = notifier
        self.redis = redis
        self.interval_seconds = interval_seconds
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self.adjustment_cooldown_seconds = adjustment_cooldown_seconds
        # The account-wide openAlgoOrders endpoint is rate limited more
        # aggressively than positionRisk.  Do not poll it every 10 seconds
        # while the account is empty; clean immediately on a position-state
        # transition and otherwise use a slow safety sweep.
        self.orphan_cleanup_interval_seconds = orphan_cleanup_interval_seconds
        self.event_debounce_seconds = event_debounce_seconds
        self._failure_cooldowns: dict[str, float] = {}
        self._adjustment_cooldowns: dict[str, float] = {}
        self._cooldown_logged: set[str] = set()
        self._last_orphan_cleanup_at = 0.0
        self._last_active_position_keys: set[tuple[str, str]] = set()
        self._run_lock = asyncio.Lock()
        self._event_debounce_until = 0.0
        self._retry_not_before = 0.0

    async def run_forever(self) -> None:
        while True:
            try:
                wait_until = max(self._retry_not_before, 0.0)
                now = time.monotonic()
                if wait_until > now:
                    await asyncio.sleep(wait_until - now)
                await self.run_once()
            except ExchangeError as error:
                # A Binance 418/429/-1003 response is a transport cooldown,
                # not a protection failure.  Keep existing exchange-side
                # hard stops in place and avoid emitting a traceback every
                # polling interval while the IP is cooling down.
                delay = max(
                    self.interval_seconds,
                    error.retry_after_seconds or self.failure_cooldown_seconds,
                )
                self._retry_not_before = max(
                    self._retry_not_before, time.monotonic() + delay
                )
                if error.code == -1003 or error.http_status in {418, 429}:
                    logger.warning(
                        "position protection polling deferred after Binance rate limit "
                        "code=%s http_status=%s retry_seconds=%.0f",
                        error.code,
                        error.http_status,
                        delay,
                    )
                else:
                    logger.warning(
                        "position protection polling failed; existing exchange hard stops "
                        "remain active error_type=%s retry_seconds=%.0f",
                        type(error).__name__,
                        delay,
                    )
                    await self.notifier.send(
                        "保护监控暂缓",
                        "交易所保护监控暂时失败；既有硬止损保持有效，系统将在冷却后重试。",
                    )
            except Exception:
                logger.exception("position protection monitor iteration failed")
                await self.notifier.send(
                    "保护监控异常", "仓位保护监控本轮失败；交易所端既有硬止损保持有效。"
                )
            if self._retry_not_before > time.monotonic():
                continue
            await asyncio.sleep(self.interval_seconds)

    async def run_once(self, *, source: str = "periodic") -> None:
        if source == "event":
            now = time.monotonic()
            if now < self._event_debounce_until:
                return
            self._event_debounce_until = now + self.event_debounce_seconds
        try:
            async with self._run_lock:
                await self._run_once()
        except ExchangeError as error:
            await self._defer_exchange_error(error)

    async def _defer_exchange_error(self, error: ExchangeError) -> None:
        delay = max(
            self.interval_seconds,
            error.retry_after_seconds or self.failure_cooldown_seconds,
        )
        self._retry_not_before = max(self._retry_not_before, time.monotonic() + delay)
        if error.code == -1003 or error.http_status in {418, 429}:
            logger.warning(
                "position protection polling deferred after Binance rate limit "
                "code=%s http_status=%s retry_seconds=%.0f",
                error.code,
                error.http_status,
                delay,
            )
            return
        logger.warning(
            "position protection polling failed; existing exchange hard stops remain "
            "active error_type=%s retry_seconds=%.0f",
            type(error).__name__,
            delay,
        )
        await self.notifier.send(
            "保护监控暂缓",
            "交易所保护监控暂时失败；既有硬止损保持有效，系统将在冷却后重试。",
        )

    async def _run_once(self) -> None:
        if not self.exchange.configured:
            return
        positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        known = await self.repository.known_open_position_keys()
        actual = {(position.symbol, position.side.value) for position in positions}
        duplicate_symbol = len({position.symbol for position in positions}) != len(positions)
        # A just-filled order can become visible on Binance a few seconds
        # before TradingCycle persists the corresponding PositionRecord.  The
        # monitor runs concurrently, so treating that tiny window as an
        # unknown operator position would freeze the whole testnet.  Defer
        # only the transient unknown-key case while the cycle lock is held;
        # duplicate long/short exposure still fails closed immediately.
        if actual - known and not duplicate_symbol and await self._cycle_is_active():
            logger.warning(
                "deferring reconciliation for exchange position during active cycle "
                "unknown_keys=%s",
                sorted(actual - known),
            )
            return
        if actual - known or duplicate_symbol:
            current_mode = await self.repository.get_mode(
                SystemMode.TESTNET
                if self.settings.binance_environment == "testnet"
                else SystemMode.LIVE_LOCKED,
                self.settings.binance_environment,
            )
            if current_mode != SystemMode.RECONCILIATION_REQUIRED:
                await self.repository.set_mode(
                    SystemMode.RECONCILIATION_REQUIRED,
                    halt_reason=(
                        "simultaneous long and short position detected"
                        if duplicate_symbol
                        else "exchange contains positions unknown to the database"
                    ),
                )
                await self.notifier.send(
                    "仓位对账异常",
                    "检测到数据库未知的币安仓位，系统已冻结新开仓。",
                )
            return
        if not positions:
            await self._cleanup_orphans_if_due(
                set(),
                force=bool(self._last_active_position_keys),
            )
            self._last_active_position_keys = set()
            await self.repository.sync_positions([])
            return

        unsafe = [position for position in positions if not position.protected]
        if unsafe:
            current_mode = await self.repository.get_mode(
                SystemMode.TESTNET
                if self.settings.binance_environment == "testnet"
                else SystemMode.LIVE_LOCKED,
                self.settings.binance_environment,
            )
            if current_mode == SystemMode.RECONCILIATION_REQUIRED:
                await self.repository.sync_positions(positions)
                return
            await self.repository.set_mode(
                SystemMode.RECONCILIATION_REQUIRED,
                halt_reason="unprotected exchange position detected",
            )
            await self.notifier.send(
                "无保护仓位待人工处置",
                f"检测到 {len(unsafe)} 个无交易所硬止损仓位，已冻结新仓，请完成仓位对账或清仓。",
            )
            return

        # A hard stop is the last-resort safety gate, but a position is not
        # fully managed until both take-profit tranches are present. A prior
        # partial exit can leave stale TP quantities, so repair them here
        # without waiting for the next AI decision.
        incomplete_take_profits = [
            position
            for position in positions
            # If only TP2 remains, TP1 has likely already filled and must not
            # be recreated behind the current market.  A missing TP2, or no
            # TP orders at all, is the repairable state.
            if position.tp2_price is None
        ]
        if not hasattr(self.exchange, "upsert_protection") or not hasattr(
            self.exchange, "get_filters"
        ):
            incomplete_take_profits = []
        protection_repaired = False
        for position in incomplete_take_profits:
            try:
                filters = await self.exchange.get_filters(position.symbol)
                tranche = self._round_down(
                    position.quantity * Decimal("0.4"), filters.step_size
                )
                if tranche < filters.min_quantity:
                    # A TP tranche below Binance's minimum cannot be placed;
                    # retain the hard stop and let a later de-risking action
                    # remove the residual quantity.
                    continue
                orders = await self.exchange.upsert_protection(
                    self._repair_intent(position),
                    position.quantity,
                    position.entry_price,
                )
                await self.repository.save_orders(orders)
                protection_repaired = True
                logger.info(
                    "take-profit protection repaired symbol=%s side=%s quantity=%s orders=%d",
                    position.symbol,
                    position.side.value,
                    position.quantity,
                    len(orders),
                )
            except ExchangeError as error:
                # Keep any existing hard stop in place. Pause new risk until a
                # later monitor pass repairs the missing take-profit orders.
                await self.repository.set_mode(
                    SystemMode.PAUSED,
                    halt_reason="take-profit protection repair failed",
                )
                await self.notifier.send(
                    "止盈保护修复失败",
                    (
                        f"{position.symbol} 止盈保护未完整建立，硬止损保持有效；"
                        f"系统已暂停增险。{error}"
                    ),
                )
                logger.warning(
                    "take-profit protection repair failed symbol=%s side=%s error=%s",
                    position.symbol,
                    position.side.value,
                    error,
                )
                return

        if protection_repaired:
            # Refresh the position model before persisting this pass; otherwise
            # the stale opening snapshot would overwrite the repaired TP state.
            positions = await self.repository.hydrate_positions(
                await self.exchange.get_positions()
            )
        # Binance can expose a just-created Algo order one polling pass later.
        # Re-check the exact monitor-imposed pause on every pass so a verified
        # repair cannot leave testnet entries stuck indefinitely.
        await self._resume_testnet_after_verified_repair(positions)

        snapshots = await self._latest_snapshots()
        changed = False
        for position in positions:
            snapshot = snapshots.get(position.symbol)
            stop = self._managed_stop(position, snapshot)
            if stop is None:
                continue
            key = position.position_id
            now = time.monotonic()
            if self._adjustment_cooldowns.get(key, 0) > now:
                continue
            retry_at = self._failure_cooldowns.get(key, 0)
            if retry_at > now:
                if key not in self._cooldown_logged:
                    logger.info(
                        "stop adjustment retry suppressed symbol=%s side=%s "
                        "current_stop=%s proposed_stop=%s cooldown_remaining_seconds=%.0f",
                        position.symbol,
                        position.side.value,
                        position.stop_price,
                        stop,
                        retry_at - now,
                    )
                    self._cooldown_logged.add(key)
                continue
            try:
                order = await self.exchange.tighten_stop(position, stop)
            except ExchangeError as error:
                hard_stop_preserved = "rollback could not restore hard stop" not in str(error)
                self._failure_cooldowns[key] = now + self.failure_cooldown_seconds
                self._cooldown_logged.discard(key)
                logger.warning(
                    "stop adjustment failed symbol=%s side=%s current_stop=%s "
                    "proposed_stop=%s mark_price=%s binance_code=%s http_status=%s "
                    "error=%s cooldown_seconds=%.0f existing_hard_stop_preserved=%s",
                    position.symbol,
                    position.side.value,
                    position.stop_price,
                    stop,
                    position.mark_price,
                    error.code,
                    error.http_status,
                    error,
                    self.failure_cooldown_seconds,
                    hard_stop_preserved,
                )
                if not hard_stop_preserved:
                    await self.repository.set_mode(
                        SystemMode.RECONCILIATION_REQUIRED,
                        halt_reason="stop replacement rollback failed",
                    )
                    await self.notifier.send(
                        "止损回滚失败",
                        f"{position.symbol} 追踪止损替换与回滚均失败，"
                        "已冻结新开仓，请立即检查币安硬止损。",
                    )
                continue
            except Exception as error:
                self._failure_cooldowns[key] = now + self.failure_cooldown_seconds
                self._cooldown_logged.discard(key)
                logger.exception(
                    "stop adjustment failed symbol=%s side=%s current_stop=%s "
                    "proposed_stop=%s error_type=%s cooldown_seconds=%.0f",
                    position.symbol,
                    position.side.value,
                    position.stop_price,
                    stop,
                    type(error).__name__,
                    self.failure_cooldown_seconds,
                )
                continue
            self._failure_cooldowns.pop(key, None)
            self._cooldown_logged.discard(key)
            self._adjustment_cooldowns[key] = now + self.adjustment_cooldown_seconds
            await self.repository.save_orders([order])
            logger.info(
                "stop adjustment succeeded symbol=%s side=%s previous_stop=%s new_stop=%s",
                position.symbol,
                position.side.value,
                position.stop_price,
                order.stop_price,
            )
            changed = True
        if changed:
            positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        active_keys = {(position.symbol, position.side.value) for position in positions}
        await self._cleanup_orphans_if_due(
            active_keys,
            force=active_keys != self._last_active_position_keys,
        )
        self._last_active_position_keys = active_keys
        await self.repository.sync_positions(positions)

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        from decimal import ROUND_DOWN

        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _repair_intent(position: PositionState) -> ExecutionIntent:
        risk = abs(position.entry_price - position.stop_price)
        if risk <= 0:
            raise ExchangeError("cannot repair take-profit protection without stop distance")
        tp1_completed = PositionProtectionMonitor._tp1_likely_completed(position)
        if position.side == PositionSide.LONG:
            tp1 = (
                None
                if tp1_completed
                else position.tp1_price or position.entry_price + risk
            )
            default_tp2 = position.entry_price + risk * Decimal("2")
            tp2 = position.tp2_price or (
                max(default_tp2, tp1 + risk) if tp1 is not None else default_tp2
            )
        else:
            tp1 = (
                None
                if tp1_completed
                else position.tp1_price or position.entry_price - risk
            )
            default_tp2 = position.entry_price - risk * Decimal("2")
            tp2 = position.tp2_price or (
                min(default_tp2, tp1 - risk) if tp1 is not None else default_tp2
            )
        if tp2 <= 0 or (tp1 is not None and tp1 <= 0):
            raise ExchangeError("cannot repair take-profit protection with invalid target")
        intent_id = uuid5(
            NAMESPACE_URL,
            f"take-profit-repair:{position.position_id}:{position.stop_price}:{tp1}:{tp2}",
        )
        return ExecutionIntent(
            intent_id=intent_id,
            signal_id=intent_id,
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            limit_price=position.entry_price,
            entry_min=position.entry_price,
            entry_max=position.entry_price,
            stop_price=position.stop_price,
            tp1_price=tp1,
            tp2_price=tp2,
            leverage=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    @staticmethod
    def _tp1_likely_completed(position: PositionState) -> bool:
        """Infer the TP2-only stage from the persisted original quantity.

        Binance can briefly omit the surviving TP2 after TP1 fills.  A normal
        first tranche closes 40%, leaving about 60%; treating that state as a
        fresh position would recreate an already-completed TP1.
        """

        return bool(
            position.tp1_price is None
            and position.initial_quantity is not None
            and position.quantity
            <= position.initial_quantity * Decimal("0.65")
        )

    async def _resume_testnet_after_verified_repair(
        self, positions: list[PositionState]
    ) -> None:
        if self.settings.binance_environment != "testnet":
            return
        if not positions or any(
            not position.protected or position.tp2_price is None
            for position in positions
        ):
            return
        state = await self.repository.get_mode_state(
            SystemMode.TESTNET, self.settings.binance_environment
        )
        if (
            state.get("mode") != SystemMode.PAUSED.value
            or state.get("halt_reason") != "take-profit protection repair failed"
        ):
            return
        await self.repository.set_mode(SystemMode.TESTNET, halt_reason=None)
        logger.info("testnet entries resumed after verified take-profit repair")
        await self.notifier.send(
            "止盈保护已恢复",
            "交易所已确认所有仓位具备硬止损和最终止盈，测试网自动交易已恢复。",
        )

    async def _cycle_is_active(self) -> bool:
        if self.redis is None:
            return False
        try:
            return bool(await self.redis.exists("trading-cycle"))
        except Exception:
            logger.debug("trading cycle lock check failed", exc_info=True)
            return False

    async def _cleanup_orphans_if_due(
        self, active_positions: set[tuple[str, str]], *, force: bool = False
    ) -> None:
        cancel_orphans = getattr(self.exchange, "cancel_orphan_protection_orders", None)
        if cancel_orphans is None:
            return
        now = time.monotonic()
        if (
            not force
            and now - self._last_orphan_cleanup_at
            < self.orphan_cleanup_interval_seconds
        ):
            return
        # Record the attempt before the network call.  A rate-limit response
        # must not turn into a tight retry loop that prolongs the ban.
        self._last_orphan_cleanup_at = now
        await cancel_orphans(active_positions)

    async def _latest_snapshots(self) -> dict[str, MarketSnapshot]:
        rows = await self.repository.latest_market(self.settings.universe_size)
        snapshots: dict[str, MarketSnapshot] = {}
        for row in rows:
            try:
                snapshot = MarketSnapshot.model_validate(row)
            except ValueError:
                continue
            snapshots[snapshot.symbol] = snapshot
        return snapshots

    @staticmethod
    def _managed_stop(position: PositionState, snapshot: MarketSnapshot | None) -> Decimal | None:
        if position.current_r < Decimal("1"):
            return None
        cost_buffer = position.entry_price * Decimal("0.0015")
        if position.side == PositionSide.LONG:
            candidate = position.entry_price + cost_buffer
            if position.current_r >= Decimal("2") and snapshot is not None:
                candidate = max(
                    candidate,
                    position.mark_price - snapshot.atr_15m * Decimal("1.5"),
                )
            if position.stop_price < candidate < position.mark_price:
                return candidate
        else:
            candidate = position.entry_price - cost_buffer
            if position.current_r >= Decimal("2") and snapshot is not None:
                candidate = min(
                    candidate,
                    position.mark_price + snapshot.atr_15m * Decimal("1.5"),
                )
            if position.stop_price > candidate > position.mark_price:
                return candidate
        return None
