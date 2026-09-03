from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from trading_system.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    SimPosition,
    aggregate_candles,
)
from trading_system.domain.enums import DecisionStatus, PositionSide, SignalAction, SystemMode
from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    MarketSnapshot,
    PositionState,
    RiskContext,
    RiskDecision,
    RiskLimits,
    TradeSignal,
    UniverseSymbol,
)
from trading_system.risk.engine import RiskEngine
from trading_system.strategy.indicators import atr, pearson_correlation
from trading_system.strategy.screener import MarketScreener
from trading_system.strategy.snapshot import build_snapshot


@dataclass
class PendingSignal:
    signal: TradeSignal
    snapshot: MarketSnapshot


@dataclass
class SymbolLedger:
    trade_pnls: list[Decimal] = field(default_factory=list)
    holding_bars: list[int] = field(default_factory=list)
    fees: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class PortfolioBacktestEngine(BacktestEngine):
    """Replay multiple symbols against one account and the production risk engine."""

    minimum_history_bars = 30 * 24 * 4

    def __init__(self) -> None:
        self.risk = RiskEngine()
        self.screener = MarketScreener()

    def run_portfolio(
        self,
        markets: dict[str, list[Candle]],
        filters: dict[str, ExchangeFilters],
        config: BacktestConfig | None = None,
        *,
        evaluation_start: datetime | None = None,
        funding_rates: dict[str, dict[datetime, Decimal]] | None = None,
    ) -> BacktestResult:
        config = config or BacktestConfig()
        self._active_config = config
        self._validate_inputs(markets, filters)
        funding_rates = funding_rates or {}
        candle_maps = {
            symbol: {candle.open_time: candle for candle in candles}
            for symbol, candles in markets.items()
        }
        timeline = sorted({timestamp for rows in candle_maps.values() for timestamp in rows})
        if not timeline:
            raise ValueError("portfolio replay has no candles")
        evaluation_start = evaluation_start or timeline[0]

        histories: dict[str, list[Candle]] = {symbol: [] for symbol in markets}
        last_prices: dict[str, Decimal] = {}
        positions: dict[str, SimPosition] = {}
        pending: dict[str, PendingSignal] = {}
        ledgers = {symbol: SymbolLedger() for symbol in markets}
        equity = config.initial_equity
        peak = equity
        max_drawdown = Decimal("0")
        day_start_equity = equity
        trading_day = evaluation_start.date()
        last_marked_equity = equity
        total_fees = Decimal("0")
        total_funding = Decimal("0")
        curve: list[dict[str, str]] = []
        circuit_breaker_triggered = False
        evaluation_index = 0
        latest_snapshots: dict[str, MarketSnapshot] = {}

        for timestamp in timeline:
            current = {
                symbol: rows[timestamp]
                for symbol, rows in candle_maps.items()
                if timestamp in rows
            }
            if timestamp < evaluation_start:
                self._append_history(current, histories, last_prices)
                continue

            if timestamp.date() != trading_day:
                trading_day = timestamp.date()
                day_start_equity = last_marked_equity

            open_prices = dict(last_prices)
            open_prices.update({symbol: candle.open for symbol, candle in current.items()})
            if not circuit_breaker_triggered:
                entry_fees = self._process_pending(
                    pending,
                    current,
                    positions,
                    ledgers,
                    latest_snapshots,
                    open_prices,
                    equity,
                    peak,
                    day_start_equity,
                    filters,
                    config,
                )
                equity -= entry_fees
                total_fees += entry_fees
            else:
                pending.clear()

            for symbol, position in list(positions.items()):
                candle = current.get(symbol)
                if candle is None:
                    continue
                position.holding_bars += 1
                current_atr = atr(histories[symbol][-100:])
                funding = self._funding_cost(
                    position,
                    candle,
                    config,
                    funding_rates.get(symbol),
                )
                if funding:
                    position.funding += funding
                    ledgers[symbol].funding += funding
                    total_funding += funding
                    equity -= funding
                pnl, closed, exit_fees = self._manage(position, candle, current_atr, config)
                equity += pnl - exit_fees
                ledgers[symbol].fees += exit_fees
                total_fees += exit_fees
                if closed:
                    self._record_closed_position(position, ledgers[symbol])
                    del positions[symbol]

            self._append_history(current, histories, last_prices)
            marked_equity = self._marked_equity(equity, positions, last_prices)
            last_marked_equity = marked_equity
            peak = max(peak, marked_equity)
            drawdown = (peak - marked_equity) / peak if peak else Decimal("0")
            max_drawdown = max(max_drawdown, drawdown)
            if evaluation_index % 16 == 0 or timestamp == timeline[-1]:
                curve.append(
                    {
                        "time": max(item.close_time for item in current.values()).isoformat(),
                        "equity": str(marked_equity),
                        "drawdown": str(drawdown),
                    }
                )

            breaker_reasons = self.risk.check_circuit_breakers(
                self._risk_context(
                    marked_equity,
                    peak,
                    day_start_equity,
                    positions,
                    last_prices,
                    next(iter(filters.values())),
                    config,
                )
            )
            if breaker_reasons:
                circuit_breaker_triggered = True
                pending.clear()
                for reason in breaker_reasons:
                    self._reject_all(ledgers, reason)
            else:
                latest_snapshots = self._generate_pending(
                    histories,
                    positions,
                    pending,
                    ledgers,
                    config,
                    funding_rates,
                )
            evaluation_index += 1

        for symbol, position in list(positions.items()):
            final_price = last_prices[symbol]
            fees_before = position.fees
            equity += self._close_remaining(position, final_price, config)
            close_fee = position.fees - fees_before
            ledgers[symbol].fees += close_fee
            total_fees += close_fee
            self._record_closed_position(position, ledgers[symbol])

        final_drawdown = (peak - equity) / peak if peak else Decimal("0")
        max_drawdown = max(max_drawdown, final_drawdown)
        if curve:
            curve[-1]["equity"] = str(equity)
            curve[-1]["drawdown"] = str(final_drawdown)

        trade_pnls = [pnl for ledger in ledgers.values() for pnl in ledger.trade_pnls]
        holding_bars = [bars for ledger in ledgers.values() for bars in ledger.holding_bars]
        signal_rejections = self._aggregate_rejections(ledgers)
        return BacktestResult(
            initial_equity=config.initial_equity,
            final_equity=equity,
            net_profit=equity - config.initial_equity,
            max_drawdown_pct=max_drawdown,
            profit_factor=self._profit_factor(trade_pnls),
            expectancy=self._expectancy(trade_pnls),
            win_rate=self._win_rate(trade_pnls),
            trades=len(trade_pnls),
            fees=total_fees,
            funding=total_funding,
            average_holding_bars=(
                Decimal(sum(holding_bars)) / Decimal(len(holding_bars))
                if holding_bars
                else Decimal("0")
            ),
            signal_rejections=signal_rejections,
            circuit_breaker_triggered=circuit_breaker_triggered,
            equity_curve=curve,
            symbol_results=self._symbol_results(ledgers, filters, config.initial_equity),
        )

    def _process_pending(
        self,
        pending: dict[str, PendingSignal],
        current: dict[str, Candle],
        positions: dict[str, SimPosition],
        ledgers: dict[str, SymbolLedger],
        snapshots: dict[str, MarketSnapshot],
        prices: dict[str, Decimal],
        equity: Decimal,
        peak: Decimal,
        day_start_equity: Decimal,
        filters: dict[str, ExchangeFilters],
        config: BacktestConfig,
    ) -> Decimal:
        fees = Decimal("0")
        ordered = sorted(pending.values(), key=lambda item: item.snapshot.score, reverse=True)
        pending.clear()
        for item in ordered:
            symbol = item.signal.symbol
            candle = current.get(symbol)
            if candle is None or candle.open_time > item.signal.expires_at:
                ledgers[symbol].rejections["signal_expired"] += 1
                continue
            entry = item.snapshot.mid_price
            touched = (
                candle.low <= entry
                if item.signal.action == SignalAction.OPEN_LONG
                else candle.high >= entry
            )
            if not touched:
                ledgers[symbol].rejections["entry_not_filled"] += 1
                continue
            marked_equity = self._marked_equity(equity - fees, positions, prices)
            correlations = self._correlations(item.snapshot, positions, snapshots)
            context = self._risk_context(
                marked_equity,
                peak,
                day_start_equity,
                positions,
                prices,
                filters[symbol],
                config,
                correlations,
            )
            decision = self.risk.evaluate(item.signal, item.snapshot, context)
            if decision.status != DecisionStatus.APPROVED:
                for reason in decision.reasons:
                    ledgers[symbol].rejections[reason] += 1
                continue
            position = self._open_decision(symbol, item.signal, decision, config)
            positions[symbol] = position
            ledgers[symbol].fees += position.fees
            fees += position.fees
        return fees

    def _generate_pending(
        self,
        histories: dict[str, list[Candle]],
        positions: dict[str, SimPosition],
        pending: dict[str, PendingSignal],
        ledgers: dict[str, SymbolLedger],
        config: BacktestConfig,
        funding_rates: dict[str, dict[datetime, Decimal]] | None = None,
    ) -> dict[str, MarketSnapshot]:
        funding_rates = funding_rates or {}
        snapshots: list[MarketSnapshot] = []
        for symbol, history in histories.items():
            if len(history) < self.minimum_history_bars:
                continue
            snapshot = self._portfolio_snapshot(symbol, history, config)
            historical_rate = self._funding_rate_at(
                funding_rates.get(symbol),
                history[-1].close_time,
                config.estimated_funding_rate,
            )
            # Keep subclass/test seams compatible with the original
            # three-argument snapshot hook while still injecting the
            # historical funding rate into the resulting immutable snapshot.
            if historical_rate != snapshot.funding_rate:
                snapshot = snapshot.model_copy(update={"funding_rate": historical_rate})
            snapshots.append(snapshot)
        snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
        self._rank_config = config
        candidates = self._rank_candidates(snapshots, ledgers, config.candidate_count)
        for snapshot in candidates:
            symbol = snapshot.symbol
            if symbol in positions or symbol in pending:
                ledgers[symbol].rejections["existing_symbol_position"] += 1
                continue
            side = self._signal(histories[symbol])
            if side is None:
                ledgers[symbol].rejections["no_aligned_signal"] += 1
                continue
            signal = self._trade_signal(snapshot, side, config)
            if signal is None:
                ledgers[symbol].rejections["invalid_price_geometry"] += 1
                continue
            pending[symbol] = PendingSignal(signal=signal, snapshot=snapshot)
        return snapshot_map

    def _rank_candidates(
        self,
        snapshots: list[MarketSnapshot],
        ledgers: dict[str, SymbolLedger],
        limit: int,
        *,
        config: BacktestConfig | None = None,
    ) -> list[MarketSnapshot]:
        eligible: list[MarketSnapshot] = []
        resolved_config = config
        if resolved_config is None:
            candidate_config = getattr(self, "_rank_config", None)
            resolved_config = (
                candidate_config
                if isinstance(candidate_config, BacktestConfig)
                else BacktestConfig()
            )
        screener = MarketScreener(
            max_spread_pct=resolved_config.max_spread_pct,
            max_abs_funding_rate=resolved_config.max_abs_funding_rate,
            max_abs_basis_pct=resolved_config.max_abs_basis_pct,
            min_book_depth_usdt=resolved_config.min_book_depth_usdt,
            min_listing_days=resolved_config.min_listing_days,
            max_volatility_percentile=resolved_config.max_volatility_percentile,
            entry_trigger=cast(
                Literal["breakout_or_pullback", "breakout_only", "pullback_only"],
                resolved_config.entry_trigger,
            ),
            trend_adx_min=resolved_config.trend_adx_min,
            volatility_soft_limit_percentile=resolved_config.volatility_soft_limit_percentile,
            volatility_hard_limit_percentile=resolved_config.volatility_hard_limit_percentile,
        )
        for snapshot in snapshots:
            accepted, reasons = screener.eligible(snapshot)
            if not accepted:
                for reason in reasons:
                    ledgers[snapshot.symbol].rejections[reason] += 1
                continue
            snapshot.score = screener.score(snapshot)
            eligible.append(snapshot)
        return sorted(eligible, key=lambda item: item.score, reverse=True)[:limit]

    @staticmethod
    def _portfolio_snapshot(
        symbol: str,
        history: list[Candle],
        config: BacktestConfig,
        *,
        funding_rate: Decimal | None = None,
    ) -> MarketSnapshot:
        price = history[-1].close
        universe = UniverseSymbol(
            symbol=symbol,
            status="TRADING",
            listing_days=365,
            quote_volume_24h=Decimal("1000000"),
            best_bid=price * Decimal("0.9999"),
            best_ask=price * Decimal("1.0001"),
            mark_price=price,
            index_price=price,
            funding_rate=(
                config.estimated_funding_rate
                if funding_rate is None
                else funding_rate
            ),
        )
        return build_snapshot(
            universe,
            history[-480:],
            aggregate_candles(history[-2880:], 4),
            aggregate_candles(history[-1920:], 16),
            Decimal("1000000"),
            book_depth_usdt=Decimal("1000000"),
            trend_adx_min=config.trend_adx_min,
            volatility_soft_limit_percentile=config.volatility_soft_limit_percentile,
            volatility_hard_limit_percentile=config.volatility_hard_limit_percentile,
            elevated_volatility_risk_multiplier=config.elevated_volatility_risk_multiplier,
            high_volatility_risk_multiplier=config.high_volatility_risk_multiplier,
        )

    @staticmethod
    def _funding_rate_at(
        rates: dict[datetime, Decimal] | None,
        timestamp: datetime,
        fallback: Decimal,
    ) -> Decimal:
        """Use the latest known historical funding rate at snapshot time."""
        if not rates:
            return fallback
        eligible = [event_time for event_time in rates if event_time <= timestamp]
        if not eligible:
            return fallback
        return rates[max(eligible)]

    @staticmethod
    def _trade_signal(
        snapshot: MarketSnapshot,
        side: PositionSide,
        config: BacktestConfig,
    ) -> TradeSignal | None:
        entry = snapshot.mid_price
        risk_distance = snapshot.atr_15m * config.stop_atr
        direction = Decimal("1") if side == PositionSide.LONG else Decimal("-1")
        invalidation = entry - direction * risk_distance
        target = entry + direction * risk_distance * Decimal("3")
        if invalidation <= 0 or target <= 0:
            return None
        return TradeSignal(
            symbol=snapshot.symbol,
            action=(
                SignalAction.OPEN_LONG
                if side == PositionSide.LONG
                else SignalAction.OPEN_SHORT
            ),
            confidence=Decimal("1"),
            entry_min=entry,
            entry_max=entry,
            invalidation_price=invalidation,
            target_price=target,
            thesis="deterministic replay signal",
            reason_codes=["DETERMINISTIC_SCREEN"],
            created_at=snapshot.timestamp,
            expires_at=snapshot.timestamp + timedelta(minutes=15),
        )

    @staticmethod
    def _open_decision(
        symbol: str,
        signal: TradeSignal,
        decision: RiskDecision,
        config: BacktestConfig,
    ) -> SimPosition:
        side = (
            PositionSide.LONG
            if signal.action == SignalAction.OPEN_LONG
            else PositionSide.SHORT
        )
        direction = Decimal("1") if side == PositionSide.LONG else Decimal("-1")
        risk_distance = abs(decision.entry_price - decision.stop_price)
        entry_fee = decision.quantity * decision.entry_price * config.fee_rate
        return SimPosition(
            side=side,
            quantity=decision.quantity,
            remaining=decision.quantity,
            entry=decision.entry_price,
            stop=decision.stop_price,
            tp1=decision.entry_price + direction * risk_distance,
            tp2=decision.entry_price + direction * risk_distance * Decimal("2"),
            highest=decision.entry_price,
            lowest=decision.entry_price,
            fees=entry_fee,
            symbol=symbol,
            initial_risk=decision.risk_amount_usdt,
            margin_used=decision.estimated_margin,
            opened_at=signal.created_at,
        )

    def _risk_context(
        self,
        equity: Decimal,
        peak: Decimal,
        day_start_equity: Decimal,
        positions: dict[str, SimPosition],
        prices: dict[str, Decimal],
        exchange_filters: ExchangeFilters,
        config: BacktestConfig,
        correlations: dict[str, Decimal] | None = None,
    ) -> RiskContext:
        margin = sum(
            (
                position.margin_used * position.remaining / position.quantity
                for position in positions.values()
            ),
            Decimal("0"),
        )
        safe_equity = max(equity, Decimal("0.00000001"))
        return RiskContext(
            mode=SystemMode.TESTNET,
            account=AccountState(
                equity=safe_equity,
                available_balance=max(Decimal("0"), safe_equity - margin),
                day_start_equity=max(day_start_equity, Decimal("0.00000001")),
                high_water_mark=max(peak, safe_equity),
                total_margin_used=margin,
            ),
            positions=self._position_states(positions, prices),
            filters=exchange_filters,
            limits=self._risk_limits(config),
            correlations=correlations or {},
            estimated_fee_rate=config.fee_rate,
            estimated_slippage_rate=config.slippage_rate,
        )

    @staticmethod
    def _risk_limits(config: BacktestConfig) -> RiskLimits:
        return RiskLimits(
            capital_limit_usdt=config.initial_equity,
            single_trade_risk_pct=config.risk_pct,
            portfolio_risk_pct=config.portfolio_risk_pct,
            daily_loss_pct=config.daily_loss_pct,
            max_drawdown_pct=config.max_drawdown_pct,
            max_leverage=config.max_leverage,
            max_margin_pct=config.max_margin_pct,
            max_positions=config.max_positions,
            max_same_direction=config.max_same_direction,
            correlation_limit=config.correlation_limit,
            min_stop_atr=config.min_stop_atr,
            max_stop_atr=config.max_stop_atr,
            min_confidence=config.min_confidence,
            min_net_reward_risk=config.min_net_reward_risk,
            entry_direction=cast(
                Literal["both", "long_only", "short_only"], config.entry_direction
            ),
            entry_trigger=cast(
                Literal["breakout_or_pullback", "breakout_only", "pullback_only"],
                config.entry_trigger,
            ),
            trend_adx_min=config.trend_adx_min,
            volatility_soft_limit_percentile=config.volatility_soft_limit_percentile,
            volatility_hard_limit_percentile=config.volatility_hard_limit_percentile,
            elevated_volatility_risk_multiplier=config.elevated_volatility_risk_multiplier,
            high_volatility_risk_multiplier=config.high_volatility_risk_multiplier,
        )

    @staticmethod
    def _position_states(
        positions: dict[str, SimPosition], prices: dict[str, Decimal]
    ) -> list[PositionState]:
        states: list[PositionState] = []
        for position in positions.values():
            mark = prices.get(position.symbol, position.entry)
            direction = (
                Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
            )
            states.append(
                PositionState(
                    position_id=f"replay-{position.symbol}-{position.side.value}",
                    symbol=position.symbol,
                    side=position.side,
                    quantity=position.remaining,
                    initial_quantity=position.quantity,
                    entry_price=position.entry,
                    mark_price=mark,
                    stop_price=position.stop,
                    original_stop_price=position.stop,
                    initial_risk_usdt=position.initial_risk,
                    unrealized_pnl=(mark - position.entry) * position.remaining * direction,
                    margin_used=(
                        position.margin_used * position.remaining / position.quantity
                    ),
                    opened_at=position.opened_at or datetime.now(UTC),
                )
            )
        return states

    @staticmethod
    def _correlations(
        candidate: MarketSnapshot,
        positions: dict[str, SimPosition],
        snapshots: dict[str, MarketSnapshot],
    ) -> dict[str, Decimal]:
        correlations: dict[str, Decimal] = {}
        for symbol in positions:
            peer = snapshots.get(symbol)
            correlations[symbol] = (
                pearson_correlation(candidate.recent_returns_1h, peer.recent_returns_1h)
                if peer is not None
                else Decimal("1")
            )
        return correlations

    @staticmethod
    def _marked_equity(
        cash_equity: Decimal,
        positions: dict[str, SimPosition],
        prices: dict[str, Decimal],
    ) -> Decimal:
        unrealized = Decimal("0")
        for symbol, position in positions.items():
            price = prices.get(symbol, position.entry)
            direction = (
                Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
            )
            unrealized += (price - position.entry) * position.remaining * direction
        return cash_equity + unrealized

    @staticmethod
    def _append_history(
        current: dict[str, Candle],
        histories: dict[str, list[Candle]],
        last_prices: dict[str, Decimal],
    ) -> None:
        for symbol, candle in current.items():
            histories[symbol].append(candle)
            last_prices[symbol] = candle.close

    @staticmethod
    def _funding_cost(
        position: SimPosition,
        candle: Candle,
        config: BacktestConfig,
        rates: dict[datetime, Decimal] | None,
    ) -> Decimal:
        settlement = candle.close_time + timedelta(milliseconds=1)
        if settlement.minute != 0 or settlement.second != 0 or settlement.hour % 8 != 0:
            return Decimal("0")
        rate = rates.get(settlement, config.estimated_funding_rate) if rates else (
            config.estimated_funding_rate
        )
        direction = Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
        return position.entry * position.remaining * rate * direction

    @staticmethod
    def _record_closed_position(position: SimPosition, ledger: SymbolLedger) -> None:
        ledger.trade_pnls.append(position.realized - position.fees - position.funding)
        ledger.holding_bars.append(position.holding_bars)

    @staticmethod
    def _validate_inputs(
        markets: dict[str, list[Candle]], filters: dict[str, ExchangeFilters]
    ) -> None:
        if not markets:
            raise ValueError("at least one symbol is required")
        missing_filters = set(markets) - set(filters)
        if missing_filters:
            raise ValueError(f"missing exchange filters for {sorted(missing_filters)}")
        for symbol, candles in markets.items():
            if len(candles) < PortfolioBacktestEngine.minimum_history_bars:
                raise ValueError(f"{symbol} requires at least 30 days of 15-minute history")

    @staticmethod
    def _reject_all(ledgers: dict[str, SymbolLedger], reason: str) -> None:
        for ledger in ledgers.values():
            ledger.rejections[reason] += 1

    @staticmethod
    def _aggregate_rejections(ledgers: dict[str, SymbolLedger]) -> dict[str, int]:
        result: dict[str, int] = {}
        for ledger in ledgers.values():
            for reason, count in ledger.rejections.items():
                result[reason] = result.get(reason, 0) + count
        return result

    @staticmethod
    def _profit_factor(trade_pnls: list[Decimal]) -> Decimal:
        gross_profit = sum((pnl for pnl in trade_pnls if pnl > 0), Decimal("0"))
        gross_loss = -sum((pnl for pnl in trade_pnls if pnl < 0), Decimal("0"))
        return gross_profit / gross_loss if gross_loss else Decimal("0")

    @staticmethod
    def _expectancy(trade_pnls: list[Decimal]) -> Decimal:
        return (
            sum(trade_pnls, Decimal("0")) / Decimal(len(trade_pnls))
            if trade_pnls
            else Decimal("0")
        )

    @staticmethod
    def _win_rate(trade_pnls: list[Decimal]) -> Decimal:
        return (
            Decimal(sum(pnl > 0 for pnl in trade_pnls)) / Decimal(len(trade_pnls))
            if trade_pnls
            else Decimal("0")
        )

    def _symbol_results(
        self,
        ledgers: dict[str, SymbolLedger],
        filters: dict[str, ExchangeFilters],
        initial_equity: Decimal,
    ) -> dict[str, dict[str, object]]:
        results: dict[str, dict[str, object]] = {}
        for symbol, ledger in ledgers.items():
            net_profit = sum(ledger.trade_pnls, Decimal("0"))
            results[symbol] = {
                "net_profit": str(net_profit),
                "net_return_pct": str(net_profit / initial_equity),
                "profit_factor": str(self._profit_factor(ledger.trade_pnls)),
                "expectancy": str(self._expectancy(ledger.trade_pnls)),
                "win_rate": str(self._win_rate(ledger.trade_pnls)),
                "trades": len(ledger.trade_pnls),
                "fees": str(ledger.fees),
                "funding": str(ledger.funding),
                "average_holding_bars": str(
                    Decimal(sum(ledger.holding_bars)) / Decimal(len(ledger.holding_bars))
                    if ledger.holding_bars
                    else Decimal("0")
                ),
                "signal_rejections": dict(ledger.rejections),
                "exchange_filters": filters[symbol].model_dump(mode="json"),
            }
        return results
