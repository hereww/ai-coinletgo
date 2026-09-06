from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from trading_system.backtest.engine import BacktestConfig, BacktestResult, SimPosition
from trading_system.backtest.portfolio import PortfolioBacktestEngine
from trading_system.config import Settings
from trading_system.domain.enums import PortfolioPlanActionType, PositionSide, SystemMode
from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    MarketSnapshot,
    PortfolioDecision,
    PortfolioPlan,
    PortfolioPlanAction,
    PositionState,
    RiskLimits,
)
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.persistence.repository import Repository
from trading_system.risk.portfolio import PortfolioCompiler
from trading_system.strategy.exit_policy import first_take_profit
from trading_system.strategy.factor_policy import eligible_research_snapshot
from trading_system.strategy.indicators import atr


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
        self.portfolio_compiler = PortfolioCompiler()

    async def create(self, parameters: dict[str, object]) -> str:
        # Persist a complete config snapshot with the queued replay.  A replay
        # must keep its meaning even if the operator changes live settings
        # while the historical job is waiting in the background queue.
        prepared = self.prepare_parameters(parameters)
        if str(prepared.get("mode", "deterministic")) == "deterministic":
            factor_policy = await self._replay_factor_policy(prepared)
            if factor_policy is not None:
                prepared["factor_policy_snapshot"] = factor_policy
            prepared["factor_rank_weight"] = str(
                self.settings.factor_rank_weight if self.settings is not None else "0.20"
            )
            prepared["factor_minimum_risk_multiplier"] = str(
                self.settings.factor_min_risk_multiplier
                if self.settings is not None
                else "0.75"
            )
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
        timezone = ZoneInfo(
            self.settings.app_timezone if self.settings is not None else "Asia/Shanghai"
        )
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
        factor_policy = await self._replay_factor_policy(parameters)
        factor_rank_weight = Decimal(str(parameters.get("factor_rank_weight", "0.20")))
        factor_minimum_multiplier = Decimal(
            str(parameters.get("factor_minimum_risk_multiplier", "0.75"))
        )
        baseline = await asyncio.to_thread(
            PortfolioBacktestEngine().run_portfolio,
            markets,
            exchange_filters,
            config,
            evaluation_start=start,
            funding_rates=funding_rates,
            factor_policy=factor_policy,
            factor_enabled=False,
            factor_rank_weight=factor_rank_weight,
            factor_minimum_risk_multiplier=factor_minimum_multiplier,
        )
        replay = baseline
        factor_enabled_result: BacktestResult | None = None
        if factor_policy is not None:
            factor_enabled_result = await asyncio.to_thread(
                PortfolioBacktestEngine().run_portfolio,
                markets,
                exchange_filters,
                config,
                evaluation_start=start,
                funding_rates=funding_rates,
                factor_policy=factor_policy,
                factor_enabled=True,
                factor_rank_weight=factor_rank_weight,
                factor_minimum_risk_multiplier=factor_minimum_multiplier,
            )
            replay = factor_enabled_result
        split = start + (end - start) / 2
        out_of_sample = await asyncio.to_thread(
            PortfolioBacktestEngine().run_portfolio,
            markets,
            exchange_filters,
            config,
            evaluation_start=split,
            funding_rates=funding_rates,
            factor_policy=factor_policy,
            factor_enabled=factor_policy is not None,
            factor_rank_weight=factor_rank_weight,
            factor_minimum_risk_multiplier=factor_minimum_multiplier,
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
            "factor_comparison": self._factor_comparison(
                baseline,
                factor_enabled_result,
                factor_policy,
                factor_rank_weight,
                factor_minimum_multiplier,
            ),
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
                "take_profit_tranches": ["40%@1R", "40%@final target", "20% trailing"],
                "factor_research_run_id": (
                    str(factor_policy["research_run_id"])
                    if factor_policy is not None
                    else None
                ),
                "factor_timing": "historical_bar_close_point_in_time_per_rebalance_bucket",
            },
        }

    async def _replay_factor_policy(
        self, parameters: dict[str, object]
    ) -> dict[str, object] | None:
        raw_run_id = parameters.get("factor_research_run_id")
        if raw_run_id is None:
            return None
        frozen = parameters.get("factor_policy_snapshot")
        if isinstance(frozen, dict):
            return cast(dict[str, object], frozen)
        run_id = str(raw_run_id)
        research = await self.repository.get_factor_research_run(run_id)
        if research is None:
            raise ValueError("factor research run was not found")
        if research.get("status") != "COMPLETED":
            raise ValueError("factor research run is not completed")
        frozen = eligible_research_snapshot(research)
        if frozen is None:
            raise ValueError("factor research run has no PASSED factors")
        return frozen

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
        try:
            paper_outcome = await self._recorded_paper_outcome(
                decision,
                plan,
                limits,
            )
        except Exception as error:
            paper_outcome = {
                "status": "UNAVAILABLE",
                "reason": f"paper_outcome_unavailable:{type(error).__name__}",
            }
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
            "paper_outcome": paper_outcome,
        }

    async def _recorded_paper_outcome(
        self,
        decision: PortfolioDecision,
        plan: PortfolioPlan,
        limits: RiskLimits,
    ) -> dict[str, object]:
        actions = [
            action
            for action in plan.actions
            if action.action in {PortfolioPlanActionType.OPEN, PortfolioPlanActionType.ADD}
            and action.side is not None
            and action.quantity_delta > 0
            and action.entry_min is not None
            and action.entry_max is not None
            and action.stop_price is not None
            and action.target_price is not None
        ]
        if not actions:
            return {
                "status": "NO_OPEN_ACTIONS",
                "horizon_hours": 24,
                "actions": [],
            }
        now = datetime.now(UTC)
        evaluation_end = min(decision.created_at + timedelta(hours=24), now)
        if evaluation_end <= decision.created_at:
            return {
                "status": "PENDING",
                "horizon_hours": 24,
                "actions": [],
            }
        warmup_start = decision.created_at - timedelta(days=30)
        start_ms = int(warmup_start.timestamp() * 1000)
        decision_ms = int(decision.created_at.timestamp() * 1000)
        end_ms = int(evaluation_end.timestamp() * 1000) + 1
        symbols = sorted({action.symbol for action in actions})

        async def load_symbol(
            symbol: str,
        ) -> tuple[str, list[Candle], dict[datetime, Decimal]]:
            candles, funding = await asyncio.gather(
                self.exchange.get_historical_klines(symbol, "15m", start_ms, end_ms),
                self.exchange.get_historical_funding_rates(symbol, decision_ms, end_ms),
            )
            return (
                symbol,
                [
                    candle
                    for candle in candles
                    if candle.close_time <= evaluation_end
                ],
                funding,
            )

        loaded = await asyncio.gather(*(load_symbol(symbol) for symbol in symbols))
        market_data = {symbol: candles for symbol, candles, _ in loaded}
        funding_data = {symbol: funding for symbol, _, funding in loaded}
        config = BacktestConfig(
            fee_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0005"),
            estimated_funding_rate=Decimal("0.0001"),
            trailing_atr=Decimal("1.5"),
            min_stop_atr=limits.min_stop_atr,
            max_stop_atr=limits.max_stop_atr,
            manual_exit_levels_enabled=limits.manual_exit_levels_enabled,
            manual_stop_atr=limits.manual_stop_atr,
            manual_take_profit_atr=limits.manual_take_profit_atr,
        )
        outcomes = [
            self._simulate_paper_action(
                action,
                market_data.get(action.symbol, []),
                funding_data.get(action.symbol, {}),
                decision.created_at,
                config,
            )
            for action in actions
        ]
        settled = [item for item in outcomes if item.get("net_pnl_usdt") is not None]
        return {
            "status": (
                "COMPLETED"
                if evaluation_end >= decision.created_at + timedelta(hours=24)
                else "PARTIAL_WINDOW"
            ),
            "horizon_hours": 24,
            "evaluation_start": decision.created_at.isoformat(),
            "evaluation_end": evaluation_end.isoformat(),
            "actions": outcomes,
            "summary": {
                "actions_evaluated": len(outcomes),
                "actions_settled": len(settled),
                "net_pnl_usdt": str(
                    sum(
                        (Decimal(str(item["net_pnl_usdt"])) for item in settled),
                        Decimal("0"),
                    )
                ),
                "fees_usdt": str(
                    sum(
                        (Decimal(str(item["fees_usdt"])) for item in settled),
                        Decimal("0"),
                    )
                ),
                "slippage_usdt": str(
                    sum(
                        (Decimal(str(item["slippage_usdt"])) for item in settled),
                        Decimal("0"),
                    )
                ),
                "funding_usdt": str(
                    sum(
                        (Decimal(str(item["funding_usdt"])) for item in settled),
                        Decimal("0"),
                    )
                ),
            },
            "methodology": (
                "first_15m_entry_band_touch; conservative_stop_first_intrabar; "
                "40pct_tp1_40pct_final_20pct_trailing; fee_slippage_funding_included"
            ),
        }

    @staticmethod
    def _simulate_paper_action(
        action: PortfolioPlanAction,
        candles: list[Candle],
        funding_rates: dict[datetime, Decimal],
        decision_time: datetime,
        config: BacktestConfig,
    ) -> dict[str, object]:
        assert action.side is not None
        assert action.entry_min is not None
        assert action.entry_max is not None
        assert action.stop_price is not None
        assert action.target_price is not None
        side = action.side
        engine = PortfolioBacktestEngine()
        history: list[Candle] = []
        position: SimPosition | None = None
        entry_time: datetime | None = None
        exit_time: datetime | None = None
        for candle in sorted(candles, key=lambda item: item.open_time):
            if candle.open_time < decision_time:
                if candle.close_time <= decision_time:
                    history.append(candle)
                continue
            if position is None:
                touched = (
                    candle.low <= action.entry_max and candle.high >= action.entry_min
                )
                if not touched:
                    history.append(candle)
                    continue
                raw_entry = min(max(candle.open, action.entry_min), action.entry_max)
                entry = raw_entry * (
                    Decimal("1") + config.slippage_rate
                    if side == PositionSide.LONG
                    else Decimal("1") - config.slippage_rate
                )
                quantity = action.quantity_delta
                try:
                    tp1 = first_take_profit(
                        entry=entry,
                        stop_price=action.stop_price,
                        final_target=action.target_price,
                        side=side,
                    )
                except ValueError:
                    return {
                        "symbol": action.symbol,
                        "action": action.action.value,
                        "status": "INVALID_GEOMETRY_AFTER_SLIPPAGE",
                        "net_pnl_usdt": None,
                    }
                position = SimPosition(
                    side=side,
                    quantity=quantity,
                    remaining=quantity,
                    entry=entry,
                    stop=action.stop_price,
                    tp1=tp1,
                    tp2=action.target_price,
                    highest=entry,
                    lowest=entry,
                    fees=entry * quantity * config.fee_rate,
                    slippage=abs(entry - raw_entry) * quantity,
                    symbol=action.symbol,
                    initial_risk=action.target_risk_usdt,
                    opened_at=candle.open_time,
                )
                entry_time = candle.open_time
            current_atr = atr(history[-100:]) if history else Decimal("0")
            funding = engine._funding_cost(position, candle, config, funding_rates)
            position.funding += funding
            _, closed, _ = engine._manage(position, candle, current_atr, config)
            history.append(candle)
            if closed:
                exit_time = candle.close_time
                break
        if position is None:
            return {
                "symbol": action.symbol,
                "action": action.action.value,
                "side": side.value,
                "status": "NOT_FILLED",
                "net_pnl_usdt": None,
            }
        status = "CLOSED_BY_EXIT_POLICY"
        if position.remaining > 0:
            if not history:
                return {
                    "symbol": action.symbol,
                    "action": action.action.value,
                    "side": side.value,
                    "status": "NO_POST_DECISION_CANDLES",
                    "net_pnl_usdt": None,
                }
            engine._close_remaining(position, history[-1].close, config)
            exit_time = history[-1].close_time
            status = "HORIZON_MARK_TO_MARKET_CLOSE"
        net_pnl = position.realized - position.fees - position.funding
        return {
            "symbol": action.symbol,
            "action": action.action.value,
            "side": side.value,
            "status": status,
            "quantity": str(position.quantity),
            "entry_price": str(position.entry),
            "stop_price": str(position.stop),
            "tp1_price": str(position.tp1),
            "final_target_price": str(position.tp2),
            "tp1_hit": position.tp1_hit,
            "final_target_hit": position.tp2_hit,
            "entry_time": entry_time.isoformat() if entry_time is not None else None,
            "exit_time": exit_time.isoformat() if exit_time is not None else None,
            "gross_pnl_usdt": str(position.realized + position.slippage),
            "net_pnl_usdt": str(net_pnl),
            "fees_usdt": str(position.fees),
            "slippage_usdt": str(position.slippage),
            "funding_usdt": str(position.funding),
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
            "slippage": str(result.slippage),
            "funding": str(result.funding),
            "costs": str(result.fees + result.slippage + result.funding),
            "candidate_turnover": str(result.candidate_turnover),
            "oriented_mean_ic": (
                str(result.oriented_mean_ic)
                if result.oriented_mean_ic is not None
                else None
            ),
            "factor_risk_clippings": result.factor_risk_clippings,
            "factor_fallbacks": result.factor_fallbacks,
            "signal_rejections": result.signal_rejections,
            "circuit_breaker_triggered": result.circuit_breaker_triggered,
        }

    @staticmethod
    def _factor_comparison(
        baseline: BacktestResult,
        factor_enabled: BacktestResult | None,
        factor_policy: dict[str, object] | None,
        rank_weight: Decimal,
        minimum_multiplier: Decimal,
    ) -> dict[str, object]:
        def metrics(result: BacktestResult) -> dict[str, object]:
            return {
                "net_return": str(
                    (result.final_equity - result.initial_equity)
                    / result.initial_equity
                ),
                "max_drawdown": str(result.max_drawdown_pct),
                "trades": result.trades,
                "fees": str(result.fees),
                "slippage": str(result.slippage),
                "funding": str(result.funding),
                "total_costs": str(result.fees + result.slippage + result.funding),
                "candidate_turnover": str(result.candidate_turnover),
                "oriented_mean_ic": (
                    str(result.oriented_mean_ic)
                    if result.oriented_mean_ic is not None
                    else None
                ),
                "factor_risk_clippings": result.factor_risk_clippings,
                "factor_fallbacks": result.factor_fallbacks,
                "signal_rejections": result.signal_rejections,
            }

        return {
            "available": factor_enabled is not None,
            "research_run_id": (
                str(factor_policy["research_run_id"])
                if factor_policy is not None
                else None
            ),
            "rank_weight": str(rank_weight),
            "minimum_risk_multiplier": str(minimum_multiplier),
            "factor_off": metrics(baseline),
            "factor_on": metrics(factor_enabled) if factor_enabled is not None else None,
        }
