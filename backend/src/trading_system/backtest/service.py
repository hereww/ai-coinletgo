from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from trading_system.backtest.engine import BacktestConfig, BacktestResult
from trading_system.backtest.portfolio import PortfolioBacktestEngine
from trading_system.config import Settings
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
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.exchange = exchange
        self.notifier = notifier
        self.settings = settings
        self.engine = PortfolioBacktestEngine()
        self.portfolio_compiler = PortfolioCompiler()

    async def create(self, parameters: dict[str, object]) -> str:
        # Persist a complete config snapshot with the queued replay.  A replay
        # must keep its meaning even if the operator changes live settings
        # while the historical job is waiting in the background queue.
        prepared = self.prepare_parameters(parameters)
        parameters.clear()
        parameters.update(prepared)
        return await self.repository.create_replay(parameters)

    def prepare_parameters(self, parameters: dict[str, object]) -> dict[str, object]:
        prepared = dict(parameters)
        if str(prepared.get("mode", "deterministic")) == "deterministic":
            config = self._backtest_config(prepared)
            prepared["backtest_config"] = self._config_payload(config)
        return prepared

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
        config = self._backtest_config(parameters)
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
            config,
            evaluation_start=start,
            funding_rates=funding_rates,
        )
        split = start + (end - start) / 2
        out_of_sample = await asyncio.to_thread(
            self.engine.run_portfolio,
            markets,
            exchange_filters,
            config,
            evaluation_start=split,
            funding_rates=funding_rates,
        )
        portfolio = replay.to_dict()
        portfolio.pop("symbols", None)
        out_of_sample_portfolio = out_of_sample.to_dict()
        out_of_sample_portfolio.pop("symbols", None)
        return {
            "replay_mode": "deterministic",
            "reproducibility": "local_deterministic_strategy",
            "summary": self._summary(replay),
            "portfolio": portfolio,
            "symbols": replay.symbol_results,
            "validation": {
                "method": "chronological_holdout",
                "split_at": split.isoformat(),
                "in_sample_period": {
                    "start": start.isoformat(),
                    "end": split.isoformat(),
                },
                "out_of_sample_period": {
                    "start": split.isoformat(),
                    "end": end.isoformat(),
                },
                "out_of_sample": {
                    "summary": self._summary(out_of_sample),
                    "portfolio": out_of_sample_portfolio,
                    "symbols": out_of_sample.symbol_results,
                },
            },
            "strategy_parameters": {
                "config_source": "runtime_settings_and_replay_request_snapshot",
                "backtest_config": self._config_payload(config),
                "take_profit_tranches": ["40%@1R", "40%@2R", "20% trailing"],
            },
        }

    def _backtest_config(self, parameters: dict[str, object]) -> BacktestConfig:
        settings = self.settings
        if settings is None:
            defaults = BacktestConfig()
            base: dict[str, object] = asdict(defaults)
        else:
            minimum_stop = Decimal(str(settings.min_stop_atr))
            maximum_stop = Decimal(str(settings.max_stop_atr))
            base = {
                "initial_equity": Decimal(str(settings.capital_limit_usdt)),
                "risk_pct": Decimal(str(settings.single_trade_risk_pct)),
                "stop_atr": min(max(Decimal("1.5"), minimum_stop), maximum_stop),
                "trailing_atr": Decimal("1.5"),
                "fee_rate": Decimal("0.0005"),
                "slippage_rate": Decimal("0.0005"),
                "estimated_funding_rate": Decimal("0.0001"),
                "daily_loss_pct": Decimal(str(settings.daily_loss_pct)),
                "max_drawdown_pct": Decimal(str(settings.max_drawdown_pct)),
                "portfolio_risk_pct": Decimal(str(settings.portfolio_risk_pct)),
                "max_leverage": settings.max_leverage,
                "max_margin_pct": Decimal(str(settings.max_margin_pct)),
                "max_positions": settings.max_positions,
                "max_same_direction": settings.max_same_direction,
            "correlation_limit": Decimal(str(settings.correlation_limit)),
            "candidate_count": settings.candidate_count,
                "max_spread_pct": Decimal(str(settings.max_spread_pct)),
                "max_abs_funding_rate": Decimal(str(settings.max_abs_funding_rate)),
                "max_abs_basis_pct": Decimal(str(settings.max_abs_basis_pct)),
                "min_book_depth_usdt": Decimal(str(settings.min_book_depth_usdt)),
                "min_listing_days": settings.min_listing_days,
                "max_volatility_percentile": Decimal("0.99"),
                "entry_direction": settings.entry_direction,
                "entry_trigger": settings.entry_trigger,
                "min_confidence": Decimal(str(settings.min_confidence)),
                "min_net_reward_risk": Decimal(str(settings.min_net_reward_risk)),
                "min_stop_atr": minimum_stop,
                "max_stop_atr": maximum_stop,
                "manual_exit_levels_enabled": settings.manual_exit_levels_enabled,
                "manual_stop_atr": Decimal(str(settings.manual_stop_atr)),
                "manual_take_profit_atr": Decimal(str(settings.manual_take_profit_atr)),
                "strong_trend_entry_override_enabled": settings.strong_trend_entry_override_enabled,
                "strong_trend_adx_min": Decimal(str(settings.strong_trend_adx_min)),
                "trend_adx_min": Decimal(str(settings.trend_adx_min)),
                "volatility_soft_limit_percentile": Decimal(
                    str(settings.volatility_soft_limit_percentile)
                ),
                "volatility_hard_limit_percentile": Decimal(
                    str(settings.volatility_hard_limit_percentile)
                ),
                "elevated_volatility_risk_multiplier": Decimal(
                    str(settings.elevated_volatility_risk_multiplier)
                ),
                "high_volatility_risk_multiplier": Decimal(
                    str(settings.high_volatility_risk_multiplier)
                ),
            }
        raw_overrides = parameters.get("backtest_config")
        overrides = raw_overrides if isinstance(raw_overrides, dict) else {}
        decimal_fields = {
            "initial_equity",
            "risk_pct",
            "stop_atr",
            "trailing_atr",
            "fee_rate",
            "slippage_rate",
            "estimated_funding_rate",
            "daily_loss_pct",
            "max_drawdown_pct",
            "portfolio_risk_pct",
            "max_margin_pct",
            "correlation_limit",
            "max_spread_pct",
            "max_abs_funding_rate",
            "max_abs_basis_pct",
            "min_book_depth_usdt",
            "min_confidence",
            "min_net_reward_risk",
            "min_stop_atr",
            "max_stop_atr",
            "manual_stop_atr",
            "manual_take_profit_atr",
            "strong_trend_adx_min",
            "trend_adx_min",
            "volatility_soft_limit_percentile",
            "volatility_hard_limit_percentile",
            "elevated_volatility_risk_multiplier",
            "high_volatility_risk_multiplier",
        }
        integer_fields = {
            "max_leverage",
            "max_positions",
            "max_same_direction",
            "candidate_count",
            "min_listing_days",
        }
        for key, value in overrides.items():
            if value is None or key not in base:
                continue
            if key in decimal_fields:
                base[key] = Decimal(str(value))
            elif key in integer_fields:
                base[key] = int(str(value))
            else:
                base[key] = str(value)
        if "stop_atr" not in overrides:
            min_stop = cast(Decimal, base["min_stop_atr"])
            max_stop = cast(Decimal, base["max_stop_atr"])
            base["stop_atr"] = min(max(cast(Decimal, base["stop_atr"]), min_stop), max_stop)
        config = BacktestConfig(
            initial_equity=cast(Decimal, base["initial_equity"]),
            risk_pct=cast(Decimal, base["risk_pct"]),
            stop_atr=cast(Decimal, base["stop_atr"]),
            trailing_atr=cast(Decimal, base["trailing_atr"]),
            fee_rate=cast(Decimal, base["fee_rate"]),
            slippage_rate=cast(Decimal, base["slippage_rate"]),
            estimated_funding_rate=cast(Decimal, base["estimated_funding_rate"]),
            daily_loss_pct=cast(Decimal, base["daily_loss_pct"]),
            max_drawdown_pct=cast(Decimal, base["max_drawdown_pct"]),
            portfolio_risk_pct=cast(Decimal, base["portfolio_risk_pct"]),
            max_leverage=cast(int, base["max_leverage"]),
            max_margin_pct=cast(Decimal, base["max_margin_pct"]),
            max_positions=cast(int, base["max_positions"]),
            max_same_direction=cast(int, base["max_same_direction"]),
            correlation_limit=cast(Decimal, base["correlation_limit"]),
            candidate_count=cast(int, base["candidate_count"]),
            entry_direction=cast(str, base["entry_direction"]),
            entry_trigger=cast(str, base["entry_trigger"]),
            min_confidence=cast(Decimal, base["min_confidence"]),
            min_net_reward_risk=cast(Decimal, base["min_net_reward_risk"]),
            min_stop_atr=cast(Decimal, base["min_stop_atr"]),
            max_stop_atr=cast(Decimal, base["max_stop_atr"]),
            manual_exit_levels_enabled=bool(base["manual_exit_levels_enabled"]),
            manual_stop_atr=cast(Decimal, base["manual_stop_atr"]),
            manual_take_profit_atr=cast(Decimal, base["manual_take_profit_atr"]),
            strong_trend_entry_override_enabled=bool(base["strong_trend_entry_override_enabled"]),
            strong_trend_adx_min=cast(Decimal, base["strong_trend_adx_min"]),
            trend_adx_min=cast(Decimal, base["trend_adx_min"]),
            volatility_soft_limit_percentile=cast(
                Decimal, base["volatility_soft_limit_percentile"]
            ),
            volatility_hard_limit_percentile=cast(
                Decimal, base["volatility_hard_limit_percentile"]
            ),
            elevated_volatility_risk_multiplier=cast(
                Decimal, base["elevated_volatility_risk_multiplier"]
            ),
            high_volatility_risk_multiplier=cast(
                Decimal, base["high_volatility_risk_multiplier"]
            ),
        )
        if config.min_stop_atr > config.max_stop_atr:
            raise ValueError("min_stop_atr cannot exceed max_stop_atr")
        if config.stop_atr < config.min_stop_atr:
            raise ValueError("stop_atr cannot be below min_stop_atr")
        if config.stop_atr > config.max_stop_atr:
            raise ValueError("stop_atr cannot exceed max_stop_atr")
        if config.manual_exit_levels_enabled and config.manual_stop_atr > config.max_stop_atr:
            raise ValueError("manual_stop_atr cannot exceed max_stop_atr")
        if config.volatility_soft_limit_percentile > config.volatility_hard_limit_percentile:
            raise ValueError(
                "volatility_soft_limit_percentile cannot exceed volatility_hard_limit_percentile"
            )
        return config

    @staticmethod
    def _config_payload(config: BacktestConfig) -> dict[str, object]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(config).items()
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
