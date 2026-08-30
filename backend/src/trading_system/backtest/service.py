from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from trading_system.backtest.engine import BacktestConfig, BacktestResult
from trading_system.backtest.portfolio import PortfolioBacktestEngine
from trading_system.domain.enums import SystemMode
from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    MarketSnapshot,
    PortfolioDecision,
    PortfolioPlan,
    PositionState,
    RiskLimits,
)
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.persistence.repository import Repository
from trading_system.risk.portfolio import PortfolioCompiler


class ReplayService:
    """Run only reproducible local replays.

    A deterministic replay uses the local historical strategy. A recorded
    portfolio replay re-runs the Portfolio-v1 compiler from the immutable
    decision-time envelope. Neither path sends a prompt to a remote model, so a
    replay is never presented as an LLM performance test.
    """

    def __init__(
        self,
        repository: Repository,
        exchange: BinanceUSDMarketClient,
        notifier: TelegramNotifier,
    ) -> None:
        self.repository = repository
        self.exchange = exchange
        self.notifier = notifier
        self.engine = PortfolioBacktestEngine()
        self.portfolio_compiler = PortfolioCompiler()

    async def create(self, parameters: dict[str, object]) -> str:
        return await self.repository.create_replay(parameters)

    async def run(self, replay_id: str, parameters: dict[str, object]) -> None:
        await self.repository.set_replay_running(replay_id)
        try:
            mode = str(parameters.get("mode", "deterministic"))
            if mode == "deterministic":
                metrics = await self._run_deterministic(parameters)
            elif mode == "recorded_portfolio":
                metrics = await self._run_recorded_portfolio(parameters)
            else:
                raise ValueError("unsupported replay mode")
            await self.repository.complete_replay(replay_id, metrics)
            await self.notifier.send("历史回放完成", f"任务 {replay_id} 已完成。")
        except Exception as error:
            await self.repository.fail_replay(replay_id, str(error)[:500])
            await self.notifier.send("历史回放失败", f"任务 {replay_id} 执行失败。")

    async def _run_deterministic(
        self, parameters: dict[str, object]
    ) -> dict[str, object]:
        raw_symbols = cast(list[object], parameters["symbols"])
        symbols = [str(symbol).upper() for symbol in raw_symbols]
        start_date = date.fromisoformat(str(parameters["start_date"]))
        end_date = date.fromisoformat(str(parameters["end_date"]))
        timezone = ZoneInfo("Asia/Shanghai")
        start = datetime.combine(start_date, time.min, timezone).astimezone(UTC)
        end = datetime.combine(end_date + timedelta(days=1), time.min, timezone).astimezone(
            UTC
        )
        if end <= start or end - start > timedelta(days=366):
            raise ValueError("replay range must be between 1 and 366 days")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        warmup_start_ms = int((start - timedelta(days=30)).timestamp() * 1000)
        markets: dict[str, list[Candle]] = {}
        exchange_filters: dict[str, ExchangeFilters] = {}
        funding_rates: dict[str, dict[datetime, Decimal]] = {}
        for symbol in symbols:
            candles, symbol_filters, symbol_funding = await asyncio.gather(
                self.exchange.get_historical_klines(symbol, "15m", warmup_start_ms, end_ms),
                self.exchange.get_filters(symbol),
                self.exchange.get_historical_funding_rates(symbol, start_ms, end_ms),
            )
            markets[symbol] = [candle for candle in candles if candle.open_time < end]
            exchange_filters[symbol] = symbol_filters
            funding_rates[symbol] = symbol_funding
        replay = await asyncio.to_thread(
            self.engine.run_portfolio,
            markets,
            exchange_filters,
            BacktestConfig(initial_equity=Decimal("1000")),
            evaluation_start=start,
            funding_rates=funding_rates,
        )
        portfolio = replay.to_dict()
        portfolio.pop("symbols", None)
        return {
            "replay_mode": "deterministic",
            "reproducibility": "local_deterministic_strategy",
            "summary": self._summary(replay),
            "portfolio": portfolio,
            "symbols": replay.symbol_results,
        }

    async def _run_recorded_portfolio(
        self, parameters: dict[str, object]
    ) -> dict[str, object]:
        decision_id = str(parameters["portfolio_decision_id"])
        envelope = await self.repository.get_portfolio_replay_input(decision_id)
        if envelope is None:
            raise ValueError(
                "recorded Portfolio-v1 context is unavailable for this decision; "
                "only decisions saved after Portfolio-v1 replay capture can be replayed"
            )
        decision_payload = cast(dict[str, object], envelope["decision"])
        context = cast(dict[str, object], envelope["context"])
        stored_plan_payload = cast(dict[str, object], envelope["compiled_plan"])
        try:
            decision = PortfolioDecision.model_validate(decision_payload)
            snapshots = {
                symbol: MarketSnapshot.model_validate(payload)
                for symbol, payload in cast(dict[str, object], context["snapshots"]).items()
            }
            account = AccountState.model_validate(context["account"])
            positions = [
                PositionState.model_validate(item)
                for item in cast(list[object], context["positions"])
            ]
            filters = {
                symbol: ExchangeFilters.model_validate(payload)
                for symbol, payload in cast(dict[str, object], context["filters"]).items()
            }
            limits = RiskLimits.model_validate(context["limits"])
            correlations = {
                (str(item["left"]), str(item["right"])): Decimal(str(item["value"]))
                for item in cast(list[dict[str, object]], context["correlations"])
            }
            raw_last_rebalance_at = context.get("last_rebalance_at")
            last_rebalance_at = (
                datetime.fromisoformat(str(raw_last_rebalance_at))
                if raw_last_rebalance_at is not None
                else None
            )
            compile_now = datetime.fromisoformat(str(context["compile_now"]))
            stored_plan = PortfolioPlan.model_validate(stored_plan_payload)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("recorded Portfolio-v1 context is malformed") from error

        plan = self.portfolio_compiler.compile(
            decision,
            snapshots=snapshots,
            account=account,
            positions=positions,
            filters=filters,
            limits=limits,
            mode=SystemMode(str(context["mode"])),
            correlations=correlations,
            last_rebalance_at=last_rebalance_at,
            cooldown_minutes=int(str(context["cooldown_minutes"])),
            now=compile_now,
        )
        matched = self._canonical_plan(plan) == self._canonical_plan(stored_plan)
        return {
            "replay_mode": "recorded_portfolio",
            "reproducibility": "persisted_decision_snapshot_compiler_exchange_rules",
            "summary": {
                "source_portfolio_decision_id": decision_id,
                "decision_time": decision.created_at.isoformat(),
                "compiler_plan_match": matched,
                "planned_actions": len(plan.actions),
                "risk_cap_usdt": str(plan.risk_cap_usdt),
                "approved_risk_usdt": str(plan.approved_risk_usdt),
            },
            "recorded_portfolio": {
                "decision": decision.model_dump(mode="json"),
                "replayed_plan": plan.model_dump(mode="json"),
                "stored_plan": stored_plan.model_dump(mode="json"),
                "candidate_symbols": context.get("candidate_symbols", []),
            },
        }

    @staticmethod
    def _canonical_plan(plan: PortfolioPlan) -> dict[str, object]:
        """Drop runtime-generated IDs/timestamps before deterministic comparison."""

        payload = plan.model_dump(mode="json")
        payload.pop("plan_id", None)
        payload.pop("compiled_at", None)
        actions = cast(list[dict[str, object]], payload["actions"])
        for action in actions:
            action.pop("action_id", None)
        return payload

    @staticmethod
    def _summary(result: BacktestResult) -> dict[str, object]:
        net_return = (result.final_equity - result.initial_equity) / result.initial_equity
        return {
            "portfolio_net_return_pct": str(net_return),
            "average_net_return_pct": str(net_return),
            "max_drawdown_pct": str(result.max_drawdown_pct),
            "worst_max_drawdown_pct": str(result.max_drawdown_pct),
            "profit_factor": str(result.profit_factor),
            "expectancy": str(result.expectancy),
            "win_rate": str(result.win_rate),
            "total_trades": result.trades,
            "symbols_tested": len(result.symbol_results),
            "fees": str(result.fees),
            "funding": str(result.funding),
            "signal_rejections": result.signal_rejections,
            "circuit_breaker_triggered": result.circuit_breaker_triggered,
        }
