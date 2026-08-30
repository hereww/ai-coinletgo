from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from redis.asyncio import Redis

from trading_system.ai.client import ModelUnavailableError, ResponsesModelClient
from trading_system.config import Settings
from trading_system.domain.enums import (
    DecisionStatus,
    PortfolioPlanActionType,
    PositionSide,
    ReviewAction,
    SignalAction,
    SystemMode,
)
from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    MarketSnapshot,
    PositionReview,
    PositionState,
    RiskContext,
    RiskLimits,
    UniverseSymbol,
)
from trading_system.exchange.base import ExchangeError, ExchangeUnknownStatusError
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.execution.exit import ExitExecutionManager
from trading_system.execution.manager import ExecutionManager
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.persistence.repository import Repository
from trading_system.risk.engine import RiskEngine
from trading_system.risk.portfolio import PortfolioCompiler
from trading_system.strategy.indicators import pearson_correlation
from trading_system.strategy.screener import MarketScreener
from trading_system.strategy.snapshot import build_snapshot

logger = logging.getLogger("trading-worker.cycle")


@dataclass
class CycleResult:
    snapshots: int = 0
    candidates: int = 0
    signals: int = 0
    approved: int = 0
    executed: int = 0
    failed: bool = False
    # A valid model decision may intentionally produce no risk-changing
    # action (for example, HOLD or an empty risk budget).  Keep that distinct
    # from a decision whose non-flat allocations were rejected by hard risk.
    no_action: bool = False
    risk_rejected: bool = False
    exchange_unavailable: bool = False
    detail: str = ""


class TradingCycle:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        repository: Repository,
        exchange: BinanceUSDMarketClient,
        model: ResponsesModelClient,
        notifier: TelegramNotifier,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.repository = repository
        self.exchange = exchange
        self.model = model
        self.notifier = notifier
        self.screener = MarketScreener(
            max_spread_pct=Decimal(str(settings.max_spread_pct)),
            max_abs_funding_rate=Decimal(str(settings.max_abs_funding_rate)),
            max_abs_basis_pct=Decimal(str(settings.max_abs_basis_pct)),
            min_book_depth_usdt=Decimal(str(settings.min_book_depth_usdt)),
            min_listing_days=settings.min_listing_days,
            entry_trigger=settings.entry_trigger,
        )
        self.risk = RiskEngine()
        self.portfolio = PortfolioCompiler()
        self.execution = ExecutionManager(exchange, entry_guard=self._entries_allowed)
        self.exits = ExitExecutionManager(exchange)

    async def run(self) -> CycleResult:
        result = CycleResult()
        started_at = datetime.now(UTC)
        await self._set_cycle_status(
            state="RUNNING",
            detail="交易周期正在执行行情检查与组合决策",
            started_at=started_at,
            finished_at=None,
            result=result,
        )
        logger.info(
            "cycle start timestamp=%s interval_minutes=%d configured_symbols=%s",
            started_at.isoformat(),
            self.settings.scan_interval_minutes,
            self.settings.entry_symbols or ["AUTO_UNIVERSE"],
        )
        if not self.exchange.configured:
            result.detail = "Binance credentials not configured; cycle stayed idle"
            return await self._finish_cycle(result, started_at)
        if (
            self.settings.portfolio_strategy_enabled
            and self.settings.binance_environment != "testnet"
        ):
            result.detail = "Portfolio-v1 策略仅允许在 Binance 测试网执行"
            return await self._finish_cycle(result, started_at)
        # A worker crash can leave the Redis lock alive until its TTL expires.
        # Keep a separate short-lived activity marker so a fresh worker can
        # remove only locks that have no corresponding active cycle, instead
        # of waiting up to fourteen minutes and reporting every cycle as
        # interrupted/busy.
        await self._recover_stale_cycle_lock()
        lock = self.redis.lock("trading-cycle", timeout=840, blocking_timeout=0)
        active_token = uuid4().hex
        try:
            acquired = await lock.acquire()
        except Exception:
            result.detail = "Redis lock unavailable; cycle failed closed"
            return await self._finish_cycle(result, started_at)
        if not acquired:
            result.detail = "another worker owns the cycle lock"
            return await self._finish_cycle(result, started_at)
        try:
            try:
                await self.redis.set("trading-cycle:active", active_token, ex=840)
            except Exception:
                logger.debug("cycle activity marker unavailable", exc_info=True)
            return await self._finish_cycle(
                await self._run_locked(result),
                started_at,
            )
        except ExchangeError as error:
            # A transport/rate-limit failure is distinct from a model
            # interruption.  Keep the cycle auditable and let the worker
            # continue scheduling the next safe cycle.
            result.exchange_unavailable = True
            result.detail = f"交易所请求失败，本轮未调用模型：{str(error)[:300]}"
            await self._finish_cycle(result, started_at)
            return result
        except Exception as error:
            result.failed = True
            await self._set_cycle_status(
                state="FAILED",
                detail=f"交易周期异常：{type(error).__name__}",
                started_at=started_at,
                finished_at=datetime.now(UTC),
                result=result,
            )
            raise
        finally:
            try:
                if acquired and await lock.owned():
                    await lock.release()
            except Exception:
                pass
            try:
                if await self.redis.get("trading-cycle:active") == active_token:
                    await self.redis.delete("trading-cycle:active")
            except Exception:
                logger.debug("cycle activity marker cleanup failed", exc_info=True)

    async def _finish_cycle(self, result: CycleResult, started_at: datetime) -> CycleResult:
        state = "COMPLETED"
        # A partially successful cycle is still a failed cycle when any
        # planned action was halted.  Do this before the executed-count check
        # so the UI cannot report a successful execution while later risk was
        # rejected or an order failed.
        execution_failed = (
            result.failed
            or "执行失败" in result.detail
            or "execution failure" in result.detail.lower()
        )
        if execution_failed:
            state = "FAILED"
        elif result.exchange_unavailable:
            state = "EXCHANGE_UNAVAILABLE"
        elif result.detail == "another worker owns the cycle lock":
            state = "WORKER_BUSY"
        elif "reconciliation" in result.detail.lower() or "对账" in result.detail:
            state = "BLOCKED_RECONCILIATION"
        elif result.executed > 0:
            state = "EXECUTED"
        elif result.detail.startswith("本轮没有通过确定性筛选"):
            state = "NO_CANDIDATES"
        elif result.no_action:
            state = "COMPLETED"
        elif result.risk_rejected:
            state = "RISK_REJECTED"
        elif "节流门禁" in result.detail or "跳过重复模型请求" in result.detail:
            state = "MODEL_THROTTLED"
        elif "超时" in result.detail or "timed out" in result.detail.lower():
            state = "MODEL_TIMEOUT"
        elif "模型" in result.detail or "model" in result.detail.lower():
            state = "MODEL_UNAVAILABLE"
        await self._set_cycle_status(
            state=state,
            detail=result.detail or "cycle completed",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            result=result,
        )
        return result

    async def _recover_stale_cycle_lock(self) -> None:
        """Clear a lock left by a dead/old worker, never an active cycle."""

        try:
            if not await self.redis.exists("trading-cycle"):
                return
            if await self.redis.exists("trading-cycle:active"):
                return
            raw = await self.redis.get("trading-cycle:last-status")
            state = ""
            if raw:
                try:
                    payload = json.loads(str(raw))
                    if isinstance(payload, dict):
                        state = str(payload.get("state", ""))
                except (TypeError, ValueError, json.JSONDecodeError):
                    state = ""
            # A RUNNING status without the activity marker may belong to an
            # older build. Keep it until the normal lock TTL expires rather
            # than risking two concurrent cycles.
            if state == "RUNNING":
                return
            deleted = await self.redis.delete("trading-cycle")
            if deleted:
                logger.warning("cleared stale trading cycle lock state=%s", state or "unknown")
        except Exception:
            logger.debug("stale trading cycle lock recovery unavailable", exc_info=True)

    async def _set_cycle_status(
        self,
        *,
        state: str,
        detail: str,
        started_at: datetime,
        finished_at: datetime | None,
        result: CycleResult,
    ) -> None:
        payload = {
            "state": state,
            "detail": detail[:400],
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat() if finished_at else None,
            "snapshots": result.snapshots,
            "candidates": result.candidates,
            "signals": result.signals,
            "approved": result.approved,
            "executed": result.executed,
            "failed": result.failed,
            "no_action": result.no_action,
            "risk_rejected": result.risk_rejected,
            "exchange_unavailable": result.exchange_unavailable,
        }
        try:
            await self.redis.set(
                "trading-cycle:last-status",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ex=86_400,
            )
        except Exception:
            logger.debug("cycle status state unavailable", exc_info=True)

    async def _run_locked(self, result: CycleResult) -> CycleResult:
        await self.repository.apply_runtime_config(self.settings)
        self.screener.entry_trigger = self.settings.entry_trigger
        logger.info(
            "cycle runtime config interval_minutes=%d entry_trigger=%s candidate_count=%d",
            self.settings.scan_interval_minutes,
            self.settings.entry_trigger,
            self.settings.candidate_count,
        )
        mode = await self.repository.get_mode(
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_LOCKED,
            self.settings.binance_environment,
        )
        # Reconciliation is an explicit operator acknowledgement boundary.
        # Do not spend Binance or model requests while it is pending, and make
        # the dashboard state unambiguously about reconciliation rather than
        # presenting a misleading model interruption.
        if mode == SystemMode.RECONCILIATION_REQUIRED:
            result.detail = "仓位对账待确认，本轮未调用模型"
            return result
        exchange_healthy, exchange_detail = await self.exchange.health_check()
        if not exchange_healthy:
            result.exchange_unavailable = True
            result.detail = f"交易所健康检查未通过，本轮未调用模型：{exchange_detail}"
            return result
        account, positions = await asyncio.gather(
            self.exchange.get_account_state(), self.exchange.get_positions()
        )
        positions = await self.repository.hydrate_positions(positions)
        await self.repository.save_income_ledger(self.exchange.last_income_ledger)
        account = await self.repository.apply_equity_checkpoints(account)

        unsafe_positions = [position for position in positions if not position.protected]
        if unsafe_positions:
            await self.repository.set_mode(
                SystemMode.RECONCILIATION_REQUIRED,
                halt_reason="unprotected exchange position detected",
            )
            await self.notifier.send(
                "无保护仓位待人工处置",
                f"检测到 {len(unsafe_positions)} 个无硬止损仓位，已冻结新仓，请完成仓位对账或清仓。",
            )
            result.detail = "unprotected positions require reconciliation"
            return result

        known = await self.repository.known_open_position_keys()
        actual = {(position.symbol, position.side.value) for position in positions}
        duplicate_symbol = len({position.symbol for position in positions}) != len(positions)
        unknown_exchange_positions = actual - known
        if unknown_exchange_positions or duplicate_symbol:
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
                "币安仓位与数据库不一致，系统已冻结新开仓。",
            )
            result.detail = "position reconciliation required"
            return result
        await self.repository.sync_positions(positions)

        limits = self._risk_limits()
        breaker_context = RiskContext(
            mode=mode,
            account=account,
            positions=positions,
            filters=self._breaker_filters(),
            limits=limits,
        )
        breaker_reasons = self.risk.check_circuit_breakers(breaker_context)
        if breaker_reasons:
            await self._trip_breaker(breaker_reasons)
            mode = SystemMode.RISK_HALTED
            result.detail = "risk circuit breaker halted new entries"
            # Portfolio-v1 may still need a model-directed CLOSE, REDUCE, or
            # TIGHTEN_STOP for existing protected positions.  Continue the
            # market/portfolio path in RISK_HALTED mode; PortfolioCompiler
            # blocks every OPEN/ADD action in that mode.  Legacy signal-v1 has
            # no equivalent portfolio target, so it remains fail-closed.
            if not (self.settings.portfolio_strategy_enabled and positions):
                return result

        selected_symbols = {symbol.upper() for symbol in self.settings.entry_symbols}
        logger.info(
            "cycle selected symbols=%s",
            sorted(selected_symbols) if selected_symbols else ["AUTO_UNIVERSE"],
        )
        universe = await self.exchange.get_universe(
            0 if selected_symbols else self.settings.universe_size
        )
        if selected_symbols:
            universe = [item for item in universe if item.symbol in selected_symbols]
            if not universe:
                result.detail = "selected entry symbols are not available on Binance"
                return result
        logger.info(
            "cycle universe count=%d symbols=%s",
            len(universe),
            [item.symbol for item in universe],
        )
        snapshots = await self._build_snapshots(universe)
        result.snapshots = len(snapshots)
        await self.repository.save_market_snapshots(snapshots)
        for snapshot in snapshots:
            eligible, reasons = self.screener.eligible(snapshot)
            logger.info(
                "cycle screener symbol=%s eligible=%s reasons=%s",
                snapshot.symbol,
                eligible,
                reasons or ["eligible"],
            )
        candidates = self.screener.rank(snapshots, self.settings.candidate_count)
        if self.settings.portfolio_strategy_enabled:
            # Portfolio-v1 owns cross-sectional ranking.  Feed it the complete
            # market-safe watchlist instead of requiring every symbol to have a
            # simultaneous trend and 15m trigger.  Those features remain in the
            # model input and malformed/unsafe targets are still rejected by the
            # deterministic compiler.  Signal-v1 keeps its strict screener.
            candidates = self.screener.rank_portfolio(snapshots)
        result.candidates = len(candidates)
        logger.info(
            "cycle candidates count=%d symbols=%s",
            len(candidates),
            [item.symbol for item in candidates],
        )
        if not candidates and not (self.settings.portfolio_strategy_enabled and positions):
            result.detail = "本轮没有通过确定性筛选的候选合约，跳过模型请求并保留下一轮15分钟周期。"
            logger.info("cycle skipped model request because deterministic candidates are empty")
            return result
        if not self.model.configured:
            result.detail = "model relay not configured; no new decisions"
            return result
        if not await self._model_cadence_available():
            result.detail = "模型15分钟节流门禁仍在生效，本轮完成行情检查但跳过重复模型请求。"
            logger.info("cycle skipped model request because local cadence window is active")
            return result

        expires_at = self._next_cycle_boundary(self.settings.scan_interval_minutes)
        try:
            if self.settings.portfolio_strategy_enabled:
                return await self._run_portfolio_cycle(
                    result,
                    candidates,
                    positions,
                    snapshots,
                    account,
                    mode,
                    limits,
                    expires_at,
                    input_hash=self._input_hash(candidates, positions),
                )
            analysis = await self.model.analyze(candidates, positions, expires_at)
        except ModelUnavailableError as error:
            if self._is_model_cadence_error(error):
                await self._mark_model_cadence()
            await self.notifier.send("模型中转异常", "模型不可用，本轮禁止新开仓。")
            result.detail = str(error)
            return result
        await self._mark_model_cadence()
        logger.info(
            "cycle model result signals=%d position_reviews=%d regime=%s summary=%s",
            len(analysis.signals),
            len(analysis.position_reviews),
            analysis.market_regime,
            analysis.summary,
        )

        reviews_ok = await self._process_reviews(analysis.position_reviews, positions)
        if not reviews_ok:
            result.failed = True
            result.detail = "position review execution failed; new entries halted"
            return result
        mode = await self._current_mode()
        account = await self._checkpoint_account(record_history=False)
        positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
        breaker_context = RiskContext(
            mode=mode,
            account=account,
            positions=positions,
            filters=self._breaker_filters(),
            limits=limits,
        )
        breaker_reasons = self.risk.check_circuit_breakers(breaker_context)
        if breaker_reasons:
            await self._trip_breaker(breaker_reasons)
            result.detail = "risk circuit breaker halted after position review"
            return result
        snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
        result.signals = len(analysis.signals)
        input_hash = self._input_hash(candidates, positions)

        for signal in analysis.signals:
            mode = await self._current_mode()
            if mode not in {SystemMode.TESTNET, SystemMode.LIVE_ENABLED}:
                result.detail = f"entry halted by system mode {mode.value}"
                break
            account = await self._checkpoint_account(record_history=False)
            positions = await self.repository.hydrate_positions(await self.exchange.get_positions())
            breaker_context = RiskContext(
                mode=mode,
                account=account,
                positions=positions,
                filters=self._breaker_filters(),
                limits=limits,
            )
            breaker_reasons = self.risk.check_circuit_breakers(breaker_context)
            if breaker_reasons:
                await self._trip_breaker(breaker_reasons)
                result.detail = "risk circuit breaker halted before entry"
                break
            signal_snapshot = snapshot_map.get(signal.symbol)
            if signal_snapshot is None:
                logger.warning(
                    "cycle signal rejected symbol=%s status=REJECTED_UNKNOWN_SYMBOL",
                    signal.symbol,
                )
                await self.repository.save_signal(
                    signal,
                    status="REJECTED_UNKNOWN_SYMBOL",
                    prompt_version=self.settings.model_prompt_version,
                    model_name=self.settings.active_model_name,
                    input_hash=input_hash,
                )
                continue
            filters = await self.exchange.get_filters(signal.symbol)
            correlations = self._correlations(signal_snapshot, positions, snapshot_map)
            context = RiskContext(
                mode=mode,
                account=account,
                positions=positions,
                filters=filters,
                limits=limits,
                correlations=correlations,
            )
            decision = self.risk.evaluate(signal, signal_snapshot, context)
            await self.repository.save_signal(
                signal,
                status=decision.status.value,
                prompt_version=self.settings.model_prompt_version,
                model_name=self.settings.active_model_name,
                input_hash=input_hash,
                market_snapshot=signal_snapshot,
            )
            await self.repository.save_risk_decision(decision)
            logger.info(
                "cycle hard risk symbol=%s action=%s status=%s reasons=%s "
                "quantity=%s leverage=%d net_reward_risk=%s",
                signal.symbol,
                signal.action.value,
                decision.status.value,
                decision.reasons,
                decision.quantity,
                decision.leverage,
                decision.net_reward_risk,
            )
            if decision.status != DecisionStatus.APPROVED:
                if signal.action != SignalAction.NO_TRADE:
                    result.risk_rejected = True
                continue
            result.approved += 1
            intent = self.risk.build_execution_intent(signal, decision)
            try:
                entry, protection = await self.execution.execute(intent)
            except Exception as error:
                result.failed = True
                if self.execution.last_emergency_orders:
                    await self.repository.save_orders(self.execution.last_emergency_orders)
                await self._halt_execution(signal.symbol, error)
                break
            await self.repository.save_orders([entry, *protection])
            if entry.filled_quantity <= 0:
                continue
            refreshed = await self.repository.hydrate_positions(await self.exchange.get_positions())
            await self.repository.sync_positions(refreshed)
            positions = refreshed
            account = await self._checkpoint_account(record_history=False)
            post_execution_context = RiskContext(
                mode=mode,
                account=account,
                positions=positions,
                filters=self._breaker_filters(),
                limits=limits,
            )
            breaker_reasons = self.risk.check_circuit_breakers(post_execution_context)
            if breaker_reasons:
                await self._trip_breaker(breaker_reasons)
                result.detail = "risk circuit breaker halted after execution"
                break
            result.executed += 1
            logger.info(
                "cycle execution symbol=%s action=%s quantity=%s protection_orders=%d",
                signal.symbol,
                signal.action.value,
                decision.quantity,
                len(protection),
            )
            await self.notifier.send(
                "订单已执行",
                f"{signal.symbol} {signal.action.value}，数量 {decision.quantity}，保护单已提交。",
            )
        if not result.detail:
            result.detail = "cycle completed"
        return result

    async def _trip_breaker(self, reasons: list[str]) -> None:
        await self.repository.set_mode(SystemMode.RISK_HALTED, halt_reason=",".join(reasons))
        try:
            await self.exchange.cancel_all_entry_orders()
        finally:
            await self.notifier.send("风控熔断", "、".join(reasons))

    async def _run_portfolio_cycle(
        self,
        result: CycleResult,
        candidates: list[MarketSnapshot],
        positions: list[PositionState],
        snapshots: list[MarketSnapshot],
        account: AccountState,
        mode: SystemMode,
        limits: RiskLimits,
        expires_at: datetime,
        *,
        input_hash: str,
    ) -> CycleResult:
        snapshot_map = {item.symbol: item for item in snapshots}
        filters: dict[str, ExchangeFilters] = {}
        for symbol in {item.symbol for item in candidates} | {item.symbol for item in positions}:
            try:
                filters[symbol] = await self.exchange.get_filters(symbol)
            except ExchangeError:
                logger.exception("portfolio filters unavailable symbol=%s", symbol)
                result.detail = f"组合编译缺少 {symbol} 交易所精度参数"
                return result
        portfolio_context = {
            "equity": str(account.equity),
            "available_balance": str(account.available_balance),
            "total_margin_used": str(account.total_margin_used),
            "used_initial_risk_usdt": str(
                sum((item.initial_risk_usdt for item in positions), Decimal("0"))
            ),
            "max_positions": limits.max_positions,
            "max_same_direction": limits.max_same_direction,
            "portfolio_risk_pct": str(limits.portfolio_risk_pct),
            # Existing positions need the same market context as candidates.
            # Without ATR/trend/volatility data the model tends to close a
            # healthy protected position simply because its thesis cannot be
            # revalidated from the position record alone.
            "position_market_context": {
                position.symbol: snapshot_map[position.symbol].model_dump(
                    mode="json", exclude={"recent_returns_1h"}
                )
                for position in positions
                if position.symbol in snapshot_map
            },
        }
        correlations = self._portfolio_correlations(candidates, positions, snapshot_map)
        portfolio_context["correlations"] = [
            {"left": left, "right": right, "value": str(value)}
            for (left, right), value in correlations.items()
        ]
        try:
            decision = await self.model.analyze_portfolio(
                candidates,
                positions,
                expires_at,
                portfolio_context=portfolio_context,
            )
        except ModelUnavailableError as error:
            if self._is_model_cadence_error(error):
                await self._mark_model_cadence()
            await self.notifier.send("模型中转异常", "组合模型不可用，本轮禁止新增或调仓风险。")
            result.detail = str(error)
            return result
        await self._mark_model_cadence()
        last_rebalance_at: datetime | None = None
        try:
            raw_last_rebalance = await self.redis.get("portfolio:last-rebalance-at")
            if raw_last_rebalance:
                last_rebalance_at = datetime.fromisoformat(str(raw_last_rebalance))
        except (TypeError, ValueError):
            last_rebalance_at = None
        compile_now = datetime.now(UTC)
        plan = self.portfolio.compile(
            decision,
            snapshots=snapshot_map,
            account=account,
            positions=positions,
            filters=filters,
            limits=limits,
            mode=mode,
            correlations=correlations,
            last_rebalance_at=last_rebalance_at,
            cooldown_minutes=self.settings.portfolio_rebalance_cooldown_minutes,
            now=compile_now,
        )
        await self.repository.save_portfolio_decision(
            decision,
            status=plan.status.value,
            prompt_version=self.settings.portfolio_prompt_version,
            model_name=self.settings.active_model_name,
            input_hash=input_hash,
            replay_context=self._portfolio_replay_context(
                candidates=candidates,
                snapshots=snapshot_map,
                account=account,
                positions=positions,
                filters=filters,
                limits=limits,
                mode=mode,
                correlations=correlations,
                last_rebalance_at=last_rebalance_at,
                cooldown_minutes=self.settings.portfolio_rebalance_cooldown_minutes,
                compile_now=compile_now,
            ),
            compiled_plan=plan,
        )
        await self.repository.save_portfolio_plan(plan)
        result.signals = len(decision.allocations)
        actionable_actions = [
            item
            for item in plan.actions
            if item.action
            not in {PortfolioPlanActionType.HOLD, PortfolioPlanActionType.REJECTED}
        ]
        rejected_actions = [
            item for item in plan.actions if item.action == PortfolioPlanActionType.REJECTED
        ]
        result.no_action = (
            not actionable_actions
            and not rejected_actions
            and plan.status.value == "NO_ACTION"
        )
        result.risk_rejected = plan.status.value == "REJECTED" and (
            bool(rejected_actions) or bool(plan.reasons)
        )
        result.approved = sum(
            item.action
            in {
                PortfolioPlanActionType.OPEN,
                PortfolioPlanActionType.ADD,
                PortfolioPlanActionType.REDUCE,
                PortfolioPlanActionType.CLOSE,
                PortfolioPlanActionType.TIGHTEN_STOP,
            }
            for item in plan.actions
        )
        current_by_symbol = {item.symbol: item for item in positions}
        for action in plan.actions:
            if action.action in {PortfolioPlanActionType.HOLD, PortfolioPlanActionType.REJECTED}:
                continue
            try:
                orders = await self._execute_portfolio_action(action, current_by_symbol, decision)
                await self.repository.save_orders(orders)
                filled = self._portfolio_action_filled(action, orders)
                await self.repository.save_portfolio_execution(
                    plan,
                    action,
                    status="EXECUTED" if filled else "NO_FILL",
                    orders=orders,
                )
                if not filled:
                    logger.info(
                        "portfolio action completed without fill symbol=%s action=%s",
                        action.symbol,
                        action.action.value,
                    )
                    continue
                result.executed += 1
                if action.action in {
                    PortfolioPlanActionType.OPEN,
                    PortfolioPlanActionType.ADD,
                }:
                    await self.redis.set(
                        "portfolio:last-rebalance-at",
                        datetime.now(UTC).isoformat(),
                        ex=172_800,
                    )
                refreshed = await self.repository.hydrate_positions(
                    await self.exchange.get_positions()
                )
                await self.repository.sync_positions(refreshed)
                current_by_symbol = {item.symbol: item for item in refreshed}
                account = await self._checkpoint_account(record_history=False)
                breaker_reasons = self.risk.check_circuit_breakers(
                    RiskContext(
                        mode=mode,
                        account=account,
                        positions=refreshed,
                        filters=self._breaker_filters(),
                        limits=limits,
                    )
                )
                if breaker_reasons:
                    await self._trip_breaker(breaker_reasons)
                    result.detail = "组合执行后触发风控熔断"
                    break
            except Exception as error:
                # A limit entry can become stale while the model decision is
                # still valid.  Treat that one action as an ordinary no-fill
                # and let the next cycle re-evaluate it; do not freeze the
                # whole testnet for a price that simply left its guard band.
                if self._is_soft_entry_guard_error(error):
                    await self.repository.save_portfolio_execution(
                        plan,
                        action,
                        status="NO_FILL",
                        orders=[],
                        detail="approved entry price guard no longer matched",
                    )
                    logger.info(
                        "portfolio action skipped because entry guard moved "
                        "symbol=%s action=%s",
                        action.symbol,
                        action.action.value,
                    )
                    continue
                result.failed = True
                await self.repository.save_portfolio_execution(
                    plan, action, status="FAILED", orders=[], detail=str(error)[:400]
                )
                await self._halt_execution(action.symbol, error)
                result.detail = f"组合动作执行失败: {action.symbol}"
                break
        if not result.detail:
            if result.no_action:
                result.detail = (
                    f"组合决策完成：{decision.market_regime}，无调仓动作，"
                    f"风险预算 {plan.risk_cap_usdt} USDT"
                )
            elif result.executed == 0 and actionable_actions:
                result.detail = (
                    f"组合决策完成：{decision.market_regime}，计划 {len(plan.actions)} 项，"
                    "本轮未产生成交"
                )
            else:
                result.detail = (
                    f"组合决策完成：{decision.market_regime}，"
                    f"计划 {len(plan.actions)} 项，批准风险 {plan.approved_risk_usdt} USDT"
                )
        return result

    @staticmethod
    def _is_soft_entry_guard_error(error: Exception) -> bool:
        """Return whether an entry missed its approved price band without a write."""

        return isinstance(error, ExchangeError) and not isinstance(
            error, ExchangeUnknownStatusError
        ) and str(error).strip() == "current price moved outside approved entry guard"

    @staticmethod
    def _portfolio_action_filled(action: Any, orders: list[Any]) -> bool:
        """Return whether a plan action changed risk or successfully updated protection.

        Entry and exit actions are only executions when at least one child LIMIT/MARKET
        order reports a fill.  Protection updates intentionally have no fills of their
        own; the exchange invariant checked by ``upsert_protection`` is their success
        signal.  This keeps an unfilled entry from being audited as a position change.
        """

        if action.action == PortfolioPlanActionType.TIGHTEN_STOP:
            return bool(orders)
        return any(
            getattr(order, "order_type", "") in {"LIMIT", "MARKET"}
            and getattr(order, "filled_quantity", Decimal("0")) > 0
            for order in orders
        )

    @staticmethod
    def _portfolio_replay_context(
        *,
        candidates: list[MarketSnapshot],
        snapshots: dict[str, MarketSnapshot],
        account: AccountState,
        positions: list[PositionState],
        filters: dict[str, ExchangeFilters],
        limits: RiskLimits,
        mode: SystemMode,
        correlations: dict[tuple[str, str], Decimal],
        last_rebalance_at: datetime | None,
        cooldown_minutes: int,
        compile_now: datetime,
    ) -> dict[str, object]:
        """Freeze every compiler input required for a recorded Portfolio-v1 replay."""

        return {
            "candidate_symbols": [item.symbol for item in candidates],
            "snapshots": {
                symbol: snapshot.model_dump(mode="json")
                for symbol, snapshot in snapshots.items()
            },
            "account": account.model_dump(mode="json"),
            "positions": [item.model_dump(mode="json") for item in positions],
            "filters": {
                symbol: exchange_filters.model_dump(mode="json")
                for symbol, exchange_filters in filters.items()
            },
            "limits": limits.model_dump(mode="json"),
            "mode": mode.value,
            "correlations": [
                {"left": left, "right": right, "value": str(value)}
                for (left, right), value in sorted(correlations.items())
            ],
            "last_rebalance_at": (
                last_rebalance_at.isoformat() if last_rebalance_at is not None else None
            ),
            "cooldown_minutes": cooldown_minutes,
            "compile_now": compile_now.isoformat(),
        }

    async def _execute_portfolio_action(
        self,
        action: Any,
        current_by_symbol: dict[str, PositionState],
        decision: Any,
    ) -> list[Any]:
        position = current_by_symbol.get(action.symbol)
        if action.action == PortfolioPlanActionType.TIGHTEN_STOP:
            if position is None or action.stop_price is None:
                raise ExchangeError("portfolio tighten-stop position is missing")
            protection_intent = self._protection_intent(
                action,
                position,
                decision_id=decision.decision_id,
            )
            return self._tag_portfolio_orders(
                await self.exchange.upsert_protection(
                    protection_intent,
                    position.quantity,
                    position.entry_price,
                ),
                action,
                decision,
            )
        if action.action in {PortfolioPlanActionType.CLOSE, PortfolioPlanActionType.REDUCE}:
            if position is None:
                raise ExchangeError("portfolio exit position is missing")
            close_side = "SELL" if position.side == PositionSide.LONG else "BUY"
            price = await self.exchange.best_entry_price(position.symbol, close_side)
            quantity = (
                position.quantity
                if action.action == PortfolioPlanActionType.CLOSE
                else action.quantity_delta
            )
            orders = await self.exits.execute(
                position,
                quantity,
                f"portfolio-{decision.decision_id}-{action.action_id}",
                price,
            )
            protection_orders = await self._refresh_after_exit(
                position,
                action,
                decision_id=decision.decision_id,
            )
            orders.extend(protection_orders)
            return self._tag_portfolio_orders(orders, action, decision)
        if action.action in {PortfolioPlanActionType.OPEN, PortfolioPlanActionType.ADD}:
            if action.side is None or action.stop_price is None or action.target_price is None:
                raise ExchangeError("portfolio entry action is incomplete")
            quantity = (
                action.quantity_delta
                if action.action == PortfolioPlanActionType.ADD
                else action.target_quantity
            )
            entry = action.entry_min or action.entry_max or action.target_price
            if action.action == PortfolioPlanActionType.ADD and action.entry_min is None:
                entry = await self.exchange.best_entry_price(
                    action.symbol,
                    "BUY" if action.side == PositionSide.LONG else "SELL",
                )
            entry_min = action.entry_min or entry * Decimal("0.997")
            entry_max = action.entry_max or entry * Decimal("1.003")
            from trading_system.domain.models import ExecutionIntent

            intent_id = uuid5(
                NAMESPACE_URL,
                f"portfolio:{decision.decision_id}:{action.action_id}",
            )
            intent = ExecutionIntent(
                intent_id=intent_id,
                signal_id=intent_id,
                symbol=action.symbol,
                side=action.side,
                quantity=quantity,
                limit_price=entry,
                entry_min=min(entry_min, entry_max),
                entry_max=max(entry_min, entry_max),
                stop_price=action.stop_price,
                tp1_price=(entry + abs(entry - action.stop_price))
                if action.side == PositionSide.LONG
                else (entry - abs(entry - action.stop_price)),
                tp2_price=action.target_price,
                leverage=self.settings.max_leverage,
                expires_at=decision.expires_at,
            )
            order, protection = await self.execution.execute(intent)
            orders = [order, *protection]
            if action.action == PortfolioPlanActionType.ADD and order.filled_quantity > 0:
                refreshed = await self.exchange.get_positions()
                total = next(
                    (
                        item
                        for item in refreshed
                        if item.symbol == action.symbol and item.side == action.side
                    ),
                    None,
                )
                if total is not None:
                    orders.extend(
                        await self.exchange.upsert_protection(
                            intent, total.quantity, total.entry_price
                        )
                    )
            return self._tag_portfolio_orders(orders, action, decision)
        raise ExchangeError(f"unsupported portfolio action: {action.action}")

    async def _refresh_after_exit(
        self,
        position: PositionState,
        action: Any,
        *,
        decision_id: Any,
    ) -> list[Any]:
        """Reconcile all remaining protection after a close or reduction.

        ExitExecutionManager cancels stale take-profits, but a limit/market
        close can be partially filled.  Re-reading the exchange position and
        upserting protection with the remaining quantity prevents oversized
        TP orders and guarantees the hard stop still covers the remainder.
        """

        refreshed = await self.exchange.get_positions()
        remaining = next(
            (
                item
                for item in refreshed
                if item.symbol == position.symbol
                and item.side == position.side
                and item.quantity > 0
            ),
            None,
        )
        if remaining is None:
            await self.exchange.cancel_position_protection(position)
            return []
        protection_intent = self._protection_intent(
            action,
            remaining,
            decision_id=decision_id,
        )
        return await self.exchange.upsert_protection(
            protection_intent,
            remaining.quantity,
            remaining.entry_price,
        )

    @staticmethod
    def _tag_portfolio_orders(
        orders: list[Any], action: Any, decision: Any
    ) -> list[Any]:
        """Attach immutable portfolio lineage to every child exchange order."""

        return [
            order.model_copy(
                update={
                    "portfolio_decision_id": decision.decision_id,
                    "portfolio_allocation_id": action.allocation_id,
                    "action_sequence": action.action_sequence,
                }
            )
            for order in orders
        ]

    def _protection_intent(
        self, action: Any, position: PositionState, *, decision_id: Any
    ) -> Any:
        from trading_system.domain.models import ExecutionIntent

        entry = position.entry_price
        risk = abs(entry - (action.stop_price or position.stop_price))
        if risk <= 0:
            risk = max(entry * Decimal("0.001"), Decimal("0.00000001"))
        target = action.target_price or position.tp2_price or (
            entry + risk * Decimal("2")
            if position.side == PositionSide.LONG
            else entry - risk * Decimal("2")
        )
        tp1 = position.tp1_price or (
            entry + risk if position.side == PositionSide.LONG else entry - risk
        )
        intent_id = uuid5(
            NAMESPACE_URL,
            f"portfolio-protection:{decision_id}:{action.action_id}",
        )
        return ExecutionIntent(
            intent_id=intent_id,
            signal_id=intent_id,
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            limit_price=entry,
            entry_min=entry,
            entry_max=entry,
            stop_price=action.stop_price or position.stop_price,
            tp1_price=tp1,
            tp2_price=target,
            leverage=self.settings.max_leverage,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    @staticmethod
    def _portfolio_correlations(
        candidates: list[MarketSnapshot],
        positions: list[PositionState],
        snapshots: dict[str, MarketSnapshot],
    ) -> dict[tuple[str, str], Decimal]:
        result: dict[tuple[str, str], Decimal] = {}
        for candidate in candidates:
            for peer_candidate in candidates:
                if candidate.symbol == peer_candidate.symbol:
                    continue
                result[(candidate.symbol, peer_candidate.symbol)] = pearson_correlation(
                    candidate.recent_returns_1h,
                    peer_candidate.recent_returns_1h,
                )
            for position in positions:
                peer = snapshots.get(position.symbol)
                result[(candidate.symbol, position.symbol)] = (
                    pearson_correlation(candidate.recent_returns_1h, peer.recent_returns_1h)
                    if peer is not None
                    else Decimal("1")
                )
        return result

    @staticmethod
    def _breaker_filters() -> ExchangeFilters:
        return ExchangeFilters(
            tick_size=Decimal("1"),
            step_size=Decimal("1"),
            min_quantity=Decimal("1"),
            min_notional=Decimal("1"),
        )

    async def _halt_execution(self, symbol: str, error: Exception) -> None:
        unknown_status = isinstance(error, ExchangeUnknownStatusError)
        mode = (
            SystemMode.RECONCILIATION_REQUIRED
            if unknown_status
            else SystemMode.PAUSED
        )
        reason = f"execution failure: {type(error).__name__}"
        try:
            await self.exchange.cancel_all_entry_orders()
        except Exception as cancel_error:
            mode = SystemMode.RECONCILIATION_REQUIRED
            reason += f"; entry cancellation failed: {type(cancel_error).__name__}"
        await self.repository.set_mode(mode, halt_reason=reason)
        await self.notifier.send(
            "订单执行异常",
            (
                f"{symbol} 执行状态未知，系统已进入仓位对账冻结。"
                if unknown_status
                else f"{symbol} 执行失败，系统已冻结新开仓。"
            ),
        )

    async def _build_snapshots(self, universe: list[UniverseSymbol]) -> list[MarketSnapshot]:
        semaphore = asyncio.Semaphore(6)

        async def build(
            item: UniverseSymbol,
        ) -> tuple[MarketSnapshot | None, list[dict[str, object]]]:
            async with semaphore:
                symbol = item.symbol
                failures: list[dict[str, object]] = []
                stages = (
                    "candles_15m",
                    "candles_1h",
                    "candles_4h",
                    "open_interest",
                    "book_depth",
                )
                requests = (
                    self.exchange.get_klines(symbol, "15m", 120),
                    self.exchange.get_klines(symbol, "1h", 720),
                    self.exchange.get_klines(symbol, "4h", 120),
                    self.exchange.get_open_interest(symbol),
                    self.exchange.get_book_depth(symbol),
                )
                values = cast(
                    tuple[Any, ...],
                    await asyncio.gather(*requests, return_exceptions=True),
                )
                for stage, value in zip(stages, values, strict=True):
                    if isinstance(value, asyncio.CancelledError):
                        raise value
                    if isinstance(value, BaseException):
                        failures.append(
                            self._snapshot_failure(
                                symbol,
                                stage=stage,
                                reason_code="MARKET_DATA_REQUEST_FAILED",
                                reason_zh=(
                                    f"{self._snapshot_stage_zh(stage)}行情接口获取失败"
                                ),
                                error=value,
                            )
                        )
                if failures:
                    self._log_snapshot_failures(symbol, failures)
                    return None, failures

                candles_15m = cast(list[Candle], values[0])
                candles_1h = cast(list[Candle], values[1])
                candles_4h = cast(list[Candle], values[2])
                open_interest = cast(Decimal, values[3])
                book_depth = cast(Decimal, values[4])
                now = datetime.now(UTC)
                candle_sets = (
                    ("candles_15m", candles_15m, timedelta(minutes=20)),
                    ("candles_1h", candles_1h, timedelta(minutes=75)),
                    ("candles_4h", candles_4h, timedelta(hours=5)),
                )
                for stage, candles, max_age in candle_sets:
                    if not candles:
                        failures.append(
                            self._snapshot_failure(
                                symbol,
                                stage=stage,
                                reason_code="EMPTY_CANDLES",
                                reason_zh=f"{self._snapshot_stage_zh(stage)}为空",
                                extra={"candle_count": 0},
                            )
                        )
                    elif now - candles[-1].close_time > max_age:
                        failures.append(
                            self._snapshot_failure(
                                symbol,
                                stage=stage,
                                reason_code="STALE_CANDLES",
                                reason_zh=f"{self._snapshot_stage_zh(stage)}已过期",
                                extra={
                                    "candle_count": len(candles),
                                    "latest_close_time": candles[-1].close_time.isoformat(),
                                    "max_age_seconds": int(max_age.total_seconds()),
                                },
                            )
                        )
                if failures:
                    self._log_snapshot_failures(symbol, failures)
                    return None, failures

                key = f"open-interest:{symbol}"
                try:
                    previous_raw = await self.redis.get(key)
                    await self.redis.set(key, str(open_interest), ex=172_800)
                    previous = Decimal(previous_raw) if previous_raw else None
                except Exception as error:
                    failure = self._snapshot_failure(
                        symbol,
                        stage="open_interest_cache",
                        reason_code="OPEN_INTEREST_CACHE_FAILED",
                        reason_zh="持仓量缓存读写失败",
                        error=error,
                    )
                    self._log_snapshot_failures(symbol, [failure])
                    return None, [failure]
                try:
                    snapshot = build_snapshot(
                        item,
                        candles_15m,
                        candles_1h,
                        candles_4h,
                        open_interest,
                        previous,
                        book_depth,
                    )
                except Exception as error:
                    failure = self._snapshot_failure(
                        symbol,
                        stage="build_snapshot",
                        reason_code="SNAPSHOT_BUILD_FAILED",
                        reason_zh="行情快照计算失败",
                        error=error,
                    )
                    self._log_snapshot_failures(symbol, [failure])
                    return None, [failure]
                logger.info(
                    "cycle snapshot success symbol=%s timestamp=%s",
                    symbol,
                    snapshot.timestamp.isoformat(),
                )
                return snapshot, []

        rows = await asyncio.gather(*(build(item) for item in universe))
        snapshots: list[MarketSnapshot] = []
        for snapshot, failures in rows:
            if snapshot is not None:
                snapshots.append(snapshot)
            for failure in failures:
                try:
                    await self.repository.audit(
                        actor="worker",
                        action="market_data_snapshot_failed",
                        resource=str(failure["symbol"]),
                        outcome="failed",
                        detail=failure,
                    )
                except Exception:
                    logger.exception(
                        "market data failure audit write failed symbol=%s stage=%s",
                        failure.get("symbol"),
                        failure.get("stage"),
                    )
        return snapshots

    @staticmethod
    def _snapshot_failure(
        symbol: str,
        *,
        stage: str,
        reason_code: str,
        reason_zh: str,
        error: BaseException | None = None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        detail: dict[str, object] = {
            "symbol": symbol,
            "stage": stage,
            "reason_code": reason_code,
            "reason_zh": reason_zh,
            "observed_at": datetime.now(UTC).isoformat(),
        }
        if error is not None:
            detail.update(
                {
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:500],
                }
            )
        if extra:
            detail.update(extra)
        return detail

    @staticmethod
    def _log_snapshot_failures(symbol: str, failures: list[dict[str, object]]) -> None:
        logger.warning(
            "cycle snapshot failed symbol=%s failures=%s",
            symbol,
            [
                {
                    "stage": failure.get("stage"),
                    "reason_code": failure.get("reason_code"),
                    "reason_zh": failure.get("reason_zh"),
                }
                for failure in failures
            ],
        )

    @staticmethod
    def _snapshot_stage_zh(stage: str) -> str:
        return {
            "candles_15m": "15分钟K线",
            "candles_1h": "1小时K线",
            "candles_4h": "4小时K线",
            "open_interest": "持仓量",
            "book_depth": "盘口深度",
            "open_interest_cache": "持仓量缓存",
            "build_snapshot": "行情快照计算",
        }.get(stage, "行情数据")

    async def _checkpoint_account(self, *, record_history: bool) -> AccountState:
        account = await self.exchange.get_account_state()
        await self.repository.save_income_ledger(self.exchange.last_income_ledger)
        return await self.repository.apply_equity_checkpoints(
            account, record_history=record_history
        )

    async def _model_cadence_available(self) -> bool:
        """Prevent duplicate manual cycles from tripping the relay cadence gate."""
        key = "trading-cycle:model-last-slot"
        try:
            raw = await self.redis.get(key)
            if raw is None:
                return True
            return int(raw) != self._model_cadence_slot()
        except Exception:
            logger.exception("model cadence state unavailable; allowing one bounded request")
            return True

    async def _mark_model_cadence(self) -> None:
        key = "trading-cycle:model-last-slot"
        window = max(15, int(self.settings.scan_interval_minutes)) * 60
        try:
            await self.redis.set(key, str(self._model_cadence_slot()), ex=window * 2)
        except Exception:
            logger.exception("model cadence state write failed")

    def _model_cadence_slot(self) -> int:
        window = max(15, int(self.settings.scan_interval_minutes)) * 60
        # Keep the five-second post-boundary grace period in the same slot as
        # the scheduled cycle, so a manual request cannot consume that cycle.
        return int((datetime.now(UTC).timestamp() - 5) // window)

    @staticmethod
    def _is_model_cadence_error(error: ModelUnavailableError) -> bool:
        message = str(error).lower()
        return any(
            token in message
            for token in ("cadence", "rate limit", "too frequent", "15 minute", "15-minute")
        )

    async def _current_mode(self) -> SystemMode:
        return await self.repository.get_mode(
            SystemMode.TESTNET
            if self.settings.binance_environment == "testnet"
            else SystemMode.LIVE_LOCKED,
            self.settings.binance_environment,
        )

    async def _entries_allowed(self) -> bool:
        return await self._current_mode() in {SystemMode.TESTNET, SystemMode.LIVE_ENABLED}

    async def _process_reviews(
        self, reviews: list[PositionReview], positions: list[PositionState]
    ) -> bool:
        position_map = {position.position_id: position for position in positions}
        processed: set[str] = set()
        priority = {
            ReviewAction.CLOSE: 0,
            ReviewAction.PARTIAL_CLOSE: 1,
            ReviewAction.TIGHTEN_STOP: 2,
            ReviewAction.HOLD: 3,
        }
        ordered_reviews = sorted(reviews, key=lambda review: priority[review.action])
        for review in ordered_reviews:
            position = position_map.get(review.position_id)
            if (
                position is None
                or review.position_id in processed
                or review.symbol != position.symbol
                or review.side != position.side
            ):
                continue
            processed.add(review.position_id)
            logger.info(
                "cycle position review symbol=%s position_id=%s action=%s confidence=%s "
                "tightened_stop=%s close_fraction=%s",
                review.symbol,
                review.position_id,
                review.action.value,
                review.confidence,
                review.tightened_stop,
                review.close_fraction,
            )
            try:
                changed = False
                if review.action in {ReviewAction.CLOSE, ReviewAction.PARTIAL_CLOSE}:
                    close_side = "SELL" if position.side == PositionSide.LONG else "BUY"
                    price = await self.exchange.best_entry_price(position.symbol, close_side)
                    quantity = position.quantity
                    if review.action == ReviewAction.PARTIAL_CLOSE:
                        quantity = await self._rounded_review_quantity(
                            position, position.quantity * (review.close_fraction or Decimal("0"))
                        )
                    orders = await self.exits.execute(
                        position,
                        quantity,
                        f"model-review-{review.action.value.lower()}-{review.position_id}-{position.opened_at.isoformat()}",
                        price,
                    )
                    await self.repository.save_orders(orders)
                    changed = True
                elif review.action == ReviewAction.TIGHTEN_STOP and review.tightened_stop:
                    new_stop = review.tightened_stop
                    tightens = (
                        position.side == PositionSide.LONG
                        and position.stop_price < new_stop < position.mark_price
                    ) or (
                        position.side == PositionSide.SHORT
                        and position.stop_price > new_stop > position.mark_price
                    )
                    if tightens:
                        order = await self.exchange.tighten_stop(position, new_stop)
                        await self.repository.save_orders([order])
                        changed = True
                if changed:
                    refreshed = await self.repository.hydrate_positions(
                        await self.exchange.get_positions()
                    )
                    await self.repository.sync_positions(refreshed)
            except Exception as error:
                if (
                    review.action == ReviewAction.TIGHTEN_STOP
                    and position.protected
                    and "rollback could not restore hard stop" not in str(error)
                ):
                    code = error.code if isinstance(error, ExchangeError) else None
                    http_status = (
                        error.http_status if isinstance(error, ExchangeError) else None
                    )
                    logger.warning(
                        "cycle position review stop adjustment skipped symbol=%s "
                        "position_id=%s proposed_stop=%s binance_code=%s "
                        "http_status=%s error=%s existing_hard_stop_preserved=true",
                        position.symbol,
                        position.position_id,
                        review.tightened_stop,
                        code,
                        http_status,
                        error,
                    )
                    continue
                await self._halt_execution(position.symbol, error)
                return False
        return True

    async def _rounded_review_quantity(
        self, position: PositionState, requested: Decimal
    ) -> Decimal:
        filters = await self.exchange.get_filters(position.symbol)
        step = filters.market_step_size or filters.step_size
        minimum = filters.market_min_quantity or filters.min_quantity
        quantity = min(requested, position.quantity)
        rounded = (quantity // step) * step
        if rounded < minimum or rounded <= 0:
            raise ExchangeError(
                f"model partial close quantity {requested} rounds below exchange minimum"
            )
        return rounded

    def _risk_limits(self) -> RiskLimits:
        return RiskLimits(
            capital_limit_usdt=Decimal(str(self.settings.capital_limit_usdt)),
            single_trade_risk_pct=Decimal(str(self.settings.single_trade_risk_pct)),
            portfolio_risk_pct=Decimal(str(self.settings.portfolio_risk_pct)),
            daily_loss_pct=Decimal(str(self.settings.daily_loss_pct)),
            max_drawdown_pct=Decimal(str(self.settings.max_drawdown_pct)),
            max_leverage=self.settings.max_leverage,
            max_margin_pct=Decimal(str(self.settings.max_margin_pct)),
            max_positions=self.settings.max_positions,
            max_same_direction=self.settings.max_same_direction,
            correlation_limit=Decimal(str(self.settings.correlation_limit)),
            min_stop_atr=Decimal(str(self.settings.min_stop_atr)),
            max_stop_atr=Decimal(str(self.settings.max_stop_atr)),
            min_confidence=Decimal(str(self.settings.min_confidence)),
            min_net_reward_risk=Decimal(str(self.settings.min_net_reward_risk)),
            entry_direction=self.settings.entry_direction,
            portfolio_rebalance_deadband_fraction=Decimal(
                str(self.settings.portfolio_rebalance_deadband_fraction)
            ),
        )

    @staticmethod
    def _correlations(
        candidate: MarketSnapshot,
        positions: list[PositionState],
        snapshots: dict[str, MarketSnapshot],
    ) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for position in positions:
            peer = snapshots.get(position.symbol)
            if peer is None:
                result[position.symbol] = Decimal("1")
            else:
                result[position.symbol] = pearson_correlation(
                    candidate.recent_returns_1h, peer.recent_returns_1h
                )
        return result

    @staticmethod
    def _input_hash(candidates: list[MarketSnapshot], positions: list[PositionState]) -> str:
        body = {
            "candidates": [
                item.model_dump(mode="json", exclude={"recent_returns_1h"}) for item in candidates
            ],
            "positions": [item.model_dump(mode="json") for item in positions],
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _next_cycle_boundary(interval_minutes: int = 15) -> datetime:
        now = datetime.now(UTC)
        interval_seconds = interval_minutes * 60
        next_timestamp = (int(now.timestamp()) // interval_seconds + 1) * interval_seconds
        return datetime.fromtimestamp(next_timestamp, tz=UTC)
