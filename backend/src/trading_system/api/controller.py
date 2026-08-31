from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from redis.asyncio import Redis
from sqlalchemy import text

from trading_system.ai.client import ResponsesModelClient
from trading_system.api.security import SecurityService
from trading_system.config import Settings
from trading_system.domain.enums import HealthState, PositionSide, SystemMode
from trading_system.domain.models import (
    AccountState,
    ExecutionIntent,
    HealthComponent,
    HealthReport,
    PositionState,
)
from trading_system.exchange.base import ExchangeError, ExchangeUnknownStatusError
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.execution.exit import ExitExecutionManager
from trading_system.execution.manager import ExecutionManager
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository
from trading_system.strategy.indicators import atr


class SystemController:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        redis: Redis,
        repository: Repository,
        exchange: BinanceUSDMarketClient,
        model: ResponsesModelClient,
    ) -> None:
        self.settings = settings
        self.database = database
        self.redis = redis
        self.repository = repository
        self.exchange = exchange
        self.model = model
        self.exits = ExitExecutionManager(exchange)
        self.entries = ExecutionManager(exchange, entry_guard=self._entries_allowed)
        self.started_at = datetime.now(UTC)
        self._health_cache: tuple[float, HealthReport] | None = None
        self._health_lock = asyncio.Lock()
        # The dashboard is a read-only view but it is polled by the browser.
        # Keep a short shared snapshot so every open tab does not multiply
        # signed Binance account/position requests (which can otherwise
        # trigger a proxy-IP REST ban). Trading and protection paths continue
        # to use their own fresh exchange reads.
        self._dashboard_exchange_cache: tuple[
            float, AccountState, list[PositionState]
        ] | None = None
        self._dashboard_exchange_lock = asyncio.Lock()

    def invalidate_health_cache(self) -> None:
        self._health_cache = None

    async def health(self, *, deep: bool = False) -> HealthReport:
        now = time.monotonic()
        if not deep and self._health_cache and now - self._health_cache[0] < 30:
            return self._health_cache[1]
        async with self._health_lock:
            now = time.monotonic()
            if not deep and self._health_cache and now - self._health_cache[0] < 30:
                return self._health_cache[1]
            components = [
                await self._database_health(),
                await self._redis_health(),
                await self._exchange_health(),
                await self._model_health(deep=deep),
                self._auth_health(),
            ]
            if self.settings.binance_environment == "live":
                components.append(await self._worker_health())
            report = HealthReport(
                ready=all(component.state == HealthState.HEALTHY for component in components),
                components=components,
            )
            if not deep:
                self._health_cache = (time.monotonic(), report)
            return report

    async def pause(self, reason: str = "operator_pause") -> SystemMode:
        cancellation_error: Exception | None = None
        if self.exchange.configured:
            try:
                await self.exchange.cancel_all_entry_orders()
            except Exception as error:
                cancellation_error = error
        mode = await self.repository.set_mode(SystemMode.PAUSED, halt_reason=reason)
        if cancellation_error is not None:
            raise ExchangeError(f"entry order cancellation failed: {cancellation_error}")
        return mode

    async def queue_cycle(self) -> str:
        if not self.exchange.configured:
            raise ValueError("Binance credentials are not configured")
        if not self.model.configured:
            raise ValueError("model relay is not configured")
        mode = await self.repository.get_mode(
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_LOCKED,
            self.settings.binance_environment,
        )
        # A deliberate testnet cycle request is also an explicit request to
        # resume a manually paused testnet. Keep this safety boundary in the
        # backend so direct API callers cannot hit the stale PAUSED-mode error
        # that the dashboard already handles before submitting the request.
        if self.settings.binance_environment == "testnet" and mode == SystemMode.PAUSED:
            mode = await self.resume_testnet()
        allowed = mode == (
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_ENABLED
        )
        if not allowed:
            raise ValueError(f"current system mode {mode.value} does not allow entries")
        operation_id = f"manual-cycle-{int(time.time() * 1000)}"
        try:
            queued = await self.redis.set(
                "trading-cycle:manual-request", operation_id, nx=True, ex=180
            )
        except Exception as error:
            raise ValueError("worker queue is unavailable") from error
        if not queued:
            raise ValueError("a manual analysis cycle is already queued")
        return operation_id

    async def integration_status(self) -> dict[str, Any]:
        proxy_scope = (
            "AI 中转（Binance 直连）"
            if self.settings.http_proxy_enabled
            and not self.settings.binance_proxy_enabled
            else "Binance + AI 中转"
            if self.settings.http_proxy_enabled
            else "未启用代理"
        )
        return {
            "proxy": {
                "enabled": self.settings.http_proxy_enabled,
                "configured": self.settings.http_proxy_configured,
                "scope": proxy_scope,
                "detail": self.settings.http_proxy_detail,
            },
            "binance": {
                "environment": self.settings.binance_environment,
                "configured": self.exchange.configured,
                "base_url": self.settings.binance_base_url,
                "health": (await self._exchange_health()).model_dump(mode="json"),
            },
            "model": {
                "configured": self.model.configured,
                "active_profile": self.settings.model_profile,
                "active_label": self.settings.active_model_label,
                "profiles": self.settings.model_profiles,
                "base_url": self.settings.active_model_base_url,
                "model_name": self.settings.active_model_name,
                "reasoning_effort": self.settings.model_reasoning_effort,
                "timeout_seconds": self.settings.model_timeout_seconds,
                "daily_request_limit": self.settings.model_daily_request_limit,
                "strategy_profile": self.settings.strategy_profile,
                "api_key_configured": bool(self.settings.active_model_api_key),
                "health": (await self._model_health()).model_dump(mode="json"),
            },
        }

    async def probe_integration(self, target: str) -> dict[str, Any]:
        if target == "binance_testnet":
            if self.settings.binance_environment != "testnet":
                raise ValueError("Binance runtime is not configured for testnet")
            component = await self._exchange_health()
        elif target == "model_relay":
            component = await self._model_health(deep=True)
        else:
            raise ValueError("unknown integration probe target")
        return component.model_dump(mode="json")

    async def resume_testnet(self) -> SystemMode:
        current = await self.repository.get_mode(
            SystemMode.TESTNET, self.settings.binance_environment
        )
        if current == SystemMode.RECONCILIATION_REQUIRED:
            raise ValueError("position reconciliation must be resolved first")
        if self.settings.binance_environment != "testnet":
            return await self.repository.set_mode(SystemMode.LIVE_LOCKED)
        return await self.repository.set_mode(SystemMode.TESTNET)

    async def reconcile_positions(self) -> SystemMode:
        if not self.exchange.configured:
            raise ValueError("Binance credentials are not configured")
        positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        if len({position.symbol for position in positions}) != len(positions):
            raise ValueError("simultaneous long and short position detected")
        if any(not position.protected for position in positions):
            raise ValueError("one or more exchange positions do not have a hard stop")
        await self.repository.sync_positions(positions)
        target = (
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_LOCKED
        )
        return await self.repository.set_mode(target, halt_reason=None)

    async def unlock_live(self) -> SystemMode:
        if self.settings.binance_environment != "live":
            raise ValueError("runtime is not configured for the live Binance environment")
        current = await self.repository.get_mode(
            SystemMode.LIVE_LOCKED, self.settings.binance_environment
        )
        if current == SystemMode.RECONCILIATION_REQUIRED:
            raise ValueError("position reconciliation must be resolved first")
        exchange_positions = await self.repository.hydrate_positions(
            await self.exchange.get_positions()
        )
        known = await self.repository.known_open_position_keys()
        actual = {(position.symbol, position.side.value) for position in exchange_positions}
        if known != actual or any(not position.protected for position in exchange_positions):
            await self.repository.set_mode(
                SystemMode.RECONCILIATION_REQUIRED,
                halt_reason="live unlock reconciliation failed",
            )
            raise ValueError("exchange positions or protection orders do not reconcile")
        if len({position.symbol for position in exchange_positions}) != len(exchange_positions):
            await self.repository.set_mode(
                SystemMode.RECONCILIATION_REQUIRED,
                halt_reason="simultaneous long and short position detected",
            )
            raise ValueError("simultaneous long and short position detected")
        components = [
            await self._database_health(),
            await self._redis_health(),
            await self._exchange_health(),
            await self._model_health(deep=True),
            self._auth_health(),
        ]
        if self.settings.binance_environment == "live":
            components.append(await self._worker_health())
        report = HealthReport(
            ready=all(component.state == HealthState.HEALTHY for component in components),
            components=components,
        )
        if not report.ready:
            raise ValueError("health gate is not green")
        return await self.repository.set_mode(SystemMode.LIVE_ENABLED)

    async def emergency_flatten(self) -> list[dict[str, Any]]:
        await self.repository.set_mode(SystemMode.PAUSED, halt_reason="emergency_flatten")
        if not self.exchange.configured:
            return []
        try:
            await self.exchange.cancel_all_entry_orders()
        except Exception:
            # Emergency market exits must continue even when an entry cancel endpoint is degraded.
            pass
        positions = await self.exchange.get_positions()
        orders = []
        errors: list[str] = []
        for position in positions:
            try:
                orders.extend(
                    await self.exchange.close_position_market_orders(
                        position, "emergency_flatten"
                    )
                )
            except Exception as error:
                errors.append(f"{position.symbol}:{position.side.value}:{error}")
        # Persist every child order before the reconciliation read.  If the
        # exchange position query is temporarily unavailable, the audit trail
        # must still contain the market-close attempts and their fills.
        await self.repository.save_orders(orders)
        remaining = await self.repository.hydrate_positions(await self.exchange.get_positions())
        await self.repository.sync_positions(remaining)
        remaining_keys = {(item.symbol, item.side) for item in remaining}
        cancel_protection = getattr(self.exchange, "cancel_position_protection", None)
        if cancel_protection is not None:
            for position in positions:
                if (position.symbol, position.side) in remaining_keys:
                    errors.append(
                        f"{position.symbol}:{position.side.value}:position remains protected"
                    )
                    continue
                try:
                    await cancel_protection(position)
                except Exception as error:
                    errors.append(
                        f"{position.symbol}:{position.side.value}:protection:{error}"
                    )
        if errors:
            raise ExchangeError("emergency flatten incomplete: " + "; ".join(errors))
        return [order.model_dump(mode="json") for order in orders]

    async def reduce_position(
        self,
        position_id: str,
        fraction: Decimal,
        operation_id: str,
    ) -> dict[str, Any]:
        if not self.exchange.configured:
            raise ValueError("Binance credentials are not configured")
        positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        position = next((item for item in positions if item.position_id == position_id), None)
        if position is None:
            raise ValueError("position is no longer open")
        close_side = "SELL" if position.side.value == "LONG" else "BUY"
        price = await self.exchange.best_entry_price(position.symbol, close_side)
        orders = await self.exits.execute(
            position, position.quantity * fraction, operation_id, price
        )
        await self.repository.save_orders(orders)
        remaining = await self.repository.hydrate_positions(await self.exchange.get_positions())
        await self.repository.sync_positions(remaining)
        return orders[-1].model_dump(mode="json")

    async def manual_entry(
        self,
        *,
        operation_id: str,
        symbol: str,
        side: str,
        leverage: int,
        stop_distance_pct: Decimal,
        tp1_r: Decimal,
        tp2_r: Decimal,
    ) -> dict[str, Any]:
        if self.settings.binance_environment != "testnet":
            raise ValueError("manual entry is available on Binance testnet only")
        if not self.exchange.configured:
            raise ValueError("Binance credentials are not configured")
        mode = await self._current_mode()
        if mode != SystemMode.TESTNET:
            raise ValueError(f"current system mode {mode.value} does not allow entries")
        if leverage > self.settings.max_leverage:
            raise ValueError("leverage exceeds the configured maximum")
        selected_symbols = {item.upper() for item in self.settings.entry_symbols}
        if selected_symbols and symbol not in selected_symbols:
            raise ValueError("symbol is not enabled in the entry whitelist")
        positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        if any(not item.protected for item in positions):
            await self.repository.set_mode(
                SystemMode.RECONCILIATION_REQUIRED,
                halt_reason="unprotected exchange position detected",
            )
            raise ValueError("an exchange position is missing a hard stop")
        if len(positions) >= self.settings.max_positions:
            raise ValueError("maximum position count reached")
        if any(item.symbol == symbol for item in positions):
            raise ValueError("this symbol already has an open position")
        desired_side = PositionSide(side)
        if sum(item.side == desired_side for item in positions) >= self.settings.max_same_direction:
            raise ValueError("maximum same-direction position count reached")
        if self.settings.entry_direction == "long_only" and desired_side == PositionSide.SHORT:
            raise ValueError("short entries are disabled by the configured strategy")
        if self.settings.entry_direction == "short_only" and desired_side == PositionSide.LONG:
            raise ValueError("long entries are disabled by the configured strategy")

        account = await self.exchange.get_account_state()
        await self.repository.save_income_ledger(self.exchange.last_income_ledger)
        account = await self.repository.apply_equity_checkpoints(account, record_history=False)
        if account.daily_equity_loss_pct >= Decimal(str(self.settings.daily_loss_pct)):
            raise ValueError("daily loss circuit breaker does not allow entries")
        if account.drawdown_pct >= Decimal(str(self.settings.max_drawdown_pct)):
            raise ValueError("drawdown circuit breaker does not allow entries")

        entry_side = "BUY" if desired_side == PositionSide.LONG else "SELL"
        entry = await self.exchange.best_entry_price(symbol, entry_side)
        filters = await self.exchange.get_filters(symbol)
        stop_distance = entry * stop_distance_pct / Decimal("100")
        if stop_distance <= 0:
            raise ValueError("stop distance is invalid")
        candles_15m = await self.exchange.get_klines(symbol, "15m", 120)
        if len(candles_15m) < 15:
            raise ValueError("not enough 15-minute market data to validate the stop")
        stop_atr = stop_distance / atr(candles_15m)
        if stop_atr < Decimal(str(self.settings.min_stop_atr)):
            raise ValueError("stop distance is below the configured ATR minimum")
        if stop_atr > Decimal(str(self.settings.max_stop_atr)):
            raise ValueError("stop distance exceeds the configured ATR maximum")
        if tp2_r < Decimal(str(self.settings.min_net_reward_risk)):
            raise ValueError("second target is below the configured reward/risk minimum")
        capital_base = min(account.equity, Decimal(str(self.settings.capital_limit_usdt)))
        risk_cap = capital_base * Decimal(str(self.settings.single_trade_risk_pct))
        current_risk = sum((item.initial_risk_usdt for item in positions), Decimal("0"))
        portfolio_cap = capital_base * Decimal(str(self.settings.portfolio_risk_pct))
        risk_amount = min(risk_cap, max(Decimal("0"), portfolio_cap - current_risk))
        if risk_amount <= 0:
            raise ValueError("portfolio risk capacity is exhausted")
        quantity = self._round_down(risk_amount / stop_distance, filters.step_size)
        available_margin = max(
            Decimal("0"),
            capital_base * Decimal(str(self.settings.max_margin_pct)) - account.total_margin_used,
        )
        quantity = min(
            quantity,
            self._round_down(available_margin * Decimal(leverage) / entry, filters.step_size),
        )
        if filters.max_quantity is not None:
            quantity = min(quantity, filters.max_quantity)
        if quantity < filters.min_quantity or quantity * entry < filters.min_notional:
            raise ValueError("calculated quantity is below the Binance minimum")

        entry = self._round_price(entry, filters.tick_size, ROUND_HALF_UP)
        if desired_side == PositionSide.LONG:
            stop = self._round_price(entry - stop_distance, filters.tick_size, ROUND_DOWN)
            tp1 = self._round_price(entry + stop_distance * tp1_r, filters.tick_size, ROUND_UP)
            tp2 = self._round_price(entry + stop_distance * tp2_r, filters.tick_size, ROUND_UP)
        else:
            stop = self._round_price(entry + stop_distance, filters.tick_size, ROUND_UP)
            tp1 = self._round_price(entry - stop_distance * tp1_r, filters.tick_size, ROUND_DOWN)
            tp2 = self._round_price(entry - stop_distance * tp2_r, filters.tick_size, ROUND_DOWN)
        if min(stop, tp1, tp2) <= 0:
            raise ValueError("calculated stop or target price is invalid")
        guard = max(filters.tick_size * Decimal("2"), entry * Decimal("0.003"))
        intent_id = uuid5(NAMESPACE_URL, f"manual-entry:{operation_id}")
        intent = ExecutionIntent(
            intent_id=intent_id,
            signal_id=intent_id,
            symbol=symbol,
            side=desired_side,
            quantity=quantity,
            limit_price=entry,
            entry_min=max(filters.tick_size, entry - guard),
            entry_max=entry + guard,
            stop_price=stop,
            tp1_price=tp1,
            tp2_price=tp2,
            leverage=leverage,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        try:
            entry_order, protection = await self.entries.execute(intent)
        except Exception as error:
            if self.entries.last_emergency_orders:
                await self.repository.save_orders(self.entries.last_emergency_orders)
            await self._halt_manual_entry(symbol, error)
            raise
        await self.repository.save_orders([entry_order, *protection])
        refreshed = await self.repository.hydrate_positions(await self.exchange.get_positions())
        await self.repository.sync_positions(refreshed)
        protected_position = next(
            (
                item
                for item in refreshed
                if item.symbol == symbol and item.side == desired_side
            ),
            None,
        )
        if entry_order.filled_quantity > 0 and (
            protected_position is None or not protected_position.protected
        ):
            await self.repository.set_mode(
                SystemMode.RECONCILIATION_REQUIRED,
                halt_reason="manual entry protection verification failed",
            )
            raise ExchangeError("manual entry filled but hard-stop verification failed")
        return {
            "operation_id": operation_id,
            "entry": entry_order.model_dump(mode="json"),
            "protection": [item.model_dump(mode="json") for item in protection],
            "position": (
                protected_position.model_dump(mode="json") if protected_position else None
            ),
        }

    async def advise_manual_entry(
        self,
        *,
        symbol: str,
        side: str,
        leverage: int,
        stop_distance_pct: Decimal,
        tp1_r: Decimal,
        tp2_r: Decimal,
        messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        if not self.model.configured:
            raise ValueError("model relay is not configured")
        if leverage > self.settings.max_leverage:
            raise ValueError("leverage exceeds the configured maximum")
        universe = await self.exchange.get_universe(0)
        item = next((row for row in universe if row.symbol == symbol), None)
        if item is None:
            raise ValueError("symbol is not available on Binance")
        positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        advice = await self.model.advise_manual_entry(
            symbol=symbol,
            side=side,
            leverage=leverage,
            stop_distance_pct=stop_distance_pct,
            tp1_r=tp1_r,
            tp2_r=tp2_r,
            messages=messages,
            market={
                "mark_price": str(item.mark_price),
                "index_price": str(item.index_price),
                "best_bid": str(item.best_bid),
                "best_ask": str(item.best_ask),
                "spread_pct": str(item.spread_pct),
                "funding_rate": str(item.funding_rate),
                "quote_volume_24h": str(item.quote_volume_24h),
            },
            positions=positions,
        )
        if advice.leverage is not None and advice.leverage > self.settings.max_leverage:
            advice = advice.model_copy(update={"leverage": self.settings.max_leverage})
        return advice.model_dump(mode="json")

    async def _current_mode(self) -> SystemMode:
        return await self.repository.get_mode(
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_LOCKED,
            self.settings.binance_environment,
        )

    async def _entries_allowed(self) -> bool:
        return await self._current_mode() in {SystemMode.TESTNET, SystemMode.LIVE_ENABLED}

    async def _halt_manual_entry(self, symbol: str, error: Exception) -> None:
        mode = (
            SystemMode.RECONCILIATION_REQUIRED
            if isinstance(error, ExchangeUnknownStatusError)
            else SystemMode.PAUSED
        )
        try:
            await self.exchange.cancel_all_entry_orders()
        except Exception:
            mode = SystemMode.RECONCILIATION_REQUIRED
        await self.repository.set_mode(
            mode, halt_reason=f"manual entry failure: {type(error).__name__}"
        )

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _round_price(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
        return (value / tick).to_integral_value(rounding=rounding) * tick

    async def dashboard(self) -> dict[str, Any]:
        mode_state = await self.repository.get_mode_state(
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_LOCKED,
            self.settings.binance_environment,
        )
        mode = SystemMode(str(mode_state["mode"]))
        report = await self.health()
        initial_risk = Decimal("0")
        position_count = 0
        if self.exchange.configured:
            try:
                account, hydrated = await self._dashboard_exchange_state()
                positions = [item.model_dump(mode="json") for item in hydrated]
                initial_risk = sum((item.initial_risk_usdt for item in hydrated), Decimal("0"))
                position_count = len(hydrated)
                capital_base = min(account.equity, Decimal(str(self.settings.capital_limit_usdt)))
                metrics = {
                    "equity": str(account.equity),
                    "daily_pnl": str(account.equity - account.day_start_equity),
                    "realized_pnl": str(account.realized_pnl_today),
                    "fees_today": str(account.fees_today),
                    "funding_today": str(account.funding_today),
                    "drawdown_pct": str(account.drawdown_pct),
                    "margin_pct": str(
                        account.total_margin_used / capital_base
                        if account.total_margin_used
                        else Decimal("0")
                    ),
                }
            except Exception:
                metrics, positions = self._empty_account()
        else:
            metrics, positions = self._empty_account()
        capital_base = min(
            Decimal(metrics["equity"]), Decimal(str(self.settings.capital_limit_usdt))
        )
        portfolio_used = initial_risk / capital_base if capital_base > 0 else Decimal("0")
        curve = await self.repository.list_equity_curve()
        if not curve:
            curve = [
                {
                    "time": datetime.now(UTC).isoformat(),
                    "equity": metrics["equity"],
                    "drawdown": metrics["drawdown_pct"],
                }
            ]
        signals = await self.repository.list_signals(limit=10)
        # Portfolio-v1 is the active strategy entry point. Keep the legacy
        # signal list for compatibility, but expose the latest portfolio
        # records separately so the dashboard cannot mistake an old signal
        # for the latest model decision.
        portfolio_decisions = await self.repository.list_portfolio_decisions(limit=6)
        cycle_status = await self._cycle_status()
        return {
            "mode": mode.value,
            "environment": self.settings.binance_environment,
            "entries_enabled": bool(mode_state.get("entries_enabled", False)),
            "halt_reason": mode_state.get("halt_reason"),
            "mode_updated_at": mode_state.get("updated_at"),
            "metrics": metrics,
            "equity_curve": curve,
            "risk_capacity": {
                "single_trade": {
                    "used": str(
                        max(
                            (
                                Decimal(str(item["initial_risk_usdt"])) / capital_base
                                for item in positions
                            ),
                            default=Decimal("0"),
                        )
                        if capital_base > 0
                        else Decimal("0")
                    ),
                    "limit": str(self.settings.single_trade_risk_pct),
                },
                "portfolio": {
                    "used": str(portfolio_used),
                    "limit": str(self.settings.portfolio_risk_pct),
                },
                "margin": {
                    "used": metrics["margin_pct"],
                    "limit": str(self.settings.max_margin_pct),
                },
                "max_leverage": self.settings.max_leverage,
                "positions": {"used": position_count, "limit": self.settings.max_positions},
            },
            "positions": positions,
            "signals": signals,
            "portfolio_decisions": portfolio_decisions,
            "cycle_status": cycle_status,
            "health": report.model_dump(mode="json"),
            "uptime_seconds": int((datetime.now(UTC) - self.started_at).total_seconds()),
        }

    async def _dashboard_exchange_state(self) -> tuple[AccountState, list[PositionState]]:
        """Read account state for the dashboard with a bounded short cache."""

        now = time.monotonic()
        cached = self._dashboard_exchange_cache
        if cached is not None and now - cached[0] < 30:
            return cached[1].model_copy(deep=True), [
                item.model_copy(deep=True) for item in cached[2]
            ]
        async with self._dashboard_exchange_lock:
            now = time.monotonic()
            cached = self._dashboard_exchange_cache
            if cached is not None and now - cached[0] < 30:
                return cached[1].model_copy(deep=True), [
                    item.model_copy(deep=True) for item in cached[2]
                ]
            account, raw_positions = await asyncio.gather(
                self.exchange.get_account_state(), self.exchange.get_positions()
            )
            await self.repository.save_income_ledger(self.exchange.last_income_ledger)
            account = await self.repository.apply_equity_checkpoints(
                account, record_history=False
            )
            hydrated = await self.repository.hydrate_positions(raw_positions)
            self._dashboard_exchange_cache = (
                time.monotonic(),
                account.model_copy(deep=True),
                [item.model_copy(deep=True) for item in hydrated],
            )
            return account, hydrated

    async def _cycle_status(self) -> dict[str, Any]:
        """Expose the latest worker cycle instead of making stale decisions look interrupted."""
        try:
            raw = await self.redis.get("trading-cycle:last-status")
            if raw:
                payload = json.loads(str(raw))
                if isinstance(payload, dict):
                    payload = await self._mark_interrupted_cycle(payload)
                    return payload
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        except Exception:
            pass
        return {
            "state": "UNKNOWN",
            "detail": "尚未收到 Worker 周期状态",
            "started_at": None,
            "finished_at": None,
            "snapshots": 0,
            "candidates": 0,
            "signals": 0,
            "approved": 0,
            "executed": 0,
        }

    async def _mark_interrupted_cycle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Label a genuinely abandoned RUNNING cycle without hiding slow model calls."""

        if payload.get("state") != "RUNNING" or payload.get("finished_at") is not None:
            return payload
        started_raw = payload.get("started_at")
        if not isinstance(started_raw, str):
            return payload
        try:
            started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return payload
        now = datetime.now(UTC)
        timeout_seconds = max(120.0, float(self.settings.model_timeout_seconds))
        # Allow the configured model timeout plus a bounded execution margin.
        # A healthy worker heartbeat keeps a long-running request in RUNNING;
        # only a stale heartbeat and an over-age cycle are marked interrupted.
        if (now - started_at).total_seconds() <= timeout_seconds + 90.0:
            return payload
        heartbeat_raw = await self.redis.get("worker:heartbeat")
        heartbeat_at: datetime | None = None
        if isinstance(heartbeat_raw, str):
            try:
                heartbeat_at = datetime.fromisoformat(heartbeat_raw.replace("Z", "+00:00"))
                if heartbeat_at.tzinfo is None:
                    heartbeat_at = heartbeat_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                heartbeat_at = None
        if heartbeat_at is not None and (now - heartbeat_at).total_seconds() <= 90.0:
            return payload
        interrupted = dict(payload)
        interrupted["state"] = "WORKER_INTERRUPTED"
        interrupted["failed"] = True
        interrupted["detail"] = (
            "Worker 在本轮模型调用或执行期间失联，未确认订单状态；"
            "已停止新增风险，请检查 Worker 后进行仓位对账。"
        )
        return interrupted

    async def latest_cycle_status(self) -> dict[str, Any]:
        """Return only the worker status for read-only console views.

        Portfolio-v1 stores model decisions separately from the worker cycle.
        Exposing this small Redis-backed view lets the portfolio page explain
        that no model call occurred (for example while reconciliation is
        pending) instead of making the last saved decision look interrupted.
        """

        return await self._cycle_status()

    async def _database_health(self) -> HealthComponent:
        started = time.perf_counter()
        try:
            async with self.database.sessions() as session:
                await session.execute(text("SELECT 1"))
            state, detail = HealthState.HEALTHY, "connected"
        except Exception:
            state, detail = HealthState.FAILED, "database unavailable"
        return HealthComponent(
            name="database",
            state=state,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=detail,
        )

    async def _redis_health(self) -> HealthComponent:
        started = time.perf_counter()
        try:
            await self.redis.ping()
            state, detail = HealthState.HEALTHY, "connected"
        except Exception:
            state = (
                HealthState.DEGRADED
                if self.settings.app_env == "development"
                else HealthState.FAILED
            )
            detail = "Redis unavailable; distributed lock and shared budget disabled"
        return HealthComponent(
            name="redis",
            state=state,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=detail,
        )

    async def _exchange_health(self) -> HealthComponent:
        started = time.perf_counter()
        healthy, detail = await self.exchange.health_check()
        if healthy:
            state = HealthState.HEALTHY
        elif not self.exchange.configured:
            state = HealthState.NOT_CONFIGURED
        else:
            state = HealthState.FAILED
        return HealthComponent(
            name="binance",
            state=state,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=detail,
        )

    async def _model_health(self, *, deep: bool = False) -> HealthComponent:
        healthy, detail = await self.model.health_check(deep=deep)
        state = (
            HealthState.HEALTHY
            if healthy
            else HealthState.FAILED
            if self.model.configured
            else HealthState.NOT_CONFIGURED
        )
        return HealthComponent(name="model_relay", state=state, detail=detail)

    async def _worker_health(self) -> HealthComponent:
        try:
            heartbeat, user_stream = await asyncio.gather(
                self.redis.get("worker:heartbeat"),
                self.redis.get("worker:user-stream"),
            )
            if not heartbeat:
                return HealthComponent(
                    name="worker", state=HealthState.FAILED, detail="worker heartbeat missing"
                )
            age = (datetime.now(UTC) - datetime.fromisoformat(str(heartbeat))).total_seconds()
            if age > 60:
                return HealthComponent(
                    name="worker", state=HealthState.FAILED, detail="worker heartbeat stale"
                )
            if user_stream != "connected":
                return HealthComponent(
                    name="worker",
                    state=HealthState.FAILED,
                    detail="Binance user data stream disconnected",
                )
            return HealthComponent(
                name="worker", state=HealthState.HEALTHY, detail="worker and user stream running"
            )
        except Exception:
            return HealthComponent(
                name="worker", state=HealthState.FAILED, detail="heartbeat unavailable"
            )

    def _auth_health(self) -> HealthComponent:
        configured = SecurityService(self.settings).production_configured()
        return HealthComponent(
            name="authentication",
            state=HealthState.HEALTHY if configured else HealthState.FAILED,
            detail="configured"
            if configured
            else "production auth secrets or secure cookies missing",
        )

    @staticmethod
    def _empty_account() -> tuple[dict[str, str], list[dict[str, Any]]]:
        return (
            {
                "equity": "0.00",
                "daily_pnl": "0.00",
                "realized_pnl": "0.00",
                "fees_today": "0.00",
                "funding_today": "0.00",
                "drawdown_pct": "0",
                "margin_pct": "0",
            },
            [],
        )
