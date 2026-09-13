from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import PositionSide
from trading_system.domain.models import Candle, UniverseSymbol
from trading_system.strategy.exit_policy import (
    TP1_FRACTION,
    TP2_FRACTION,
    breakeven_stop,
    first_take_profit,
    trailing_stop,
)
from trading_system.strategy.indicators import (
    atr,
    donchian_breakout,
    pullback_signal,
    trend_direction,
)
from trading_system.strategy.screener import MarketScreener
from trading_system.strategy.snapshot import build_snapshot


@dataclass
class SimPosition:
    side: PositionSide
    quantity: Decimal
    remaining: Decimal
    entry: Decimal
    stop: Decimal
    tp1: Decimal
    tp2: Decimal
    highest: Decimal
    lowest: Decimal
    realized: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    tp1_hit: bool = False
    tp2_hit: bool = False
    symbol: str = "BACKTEST"
    initial_risk: Decimal = Decimal("0")
    margin_used: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    holding_bars: int = 0
    opened_at: datetime | None = None


@dataclass
class BacktestConfig:
    initial_equity: Decimal = Decimal("1000")
    risk_pct: Decimal = Decimal("0.0025")
    stop_atr: Decimal = Decimal("1.5")
    trailing_atr: Decimal = Decimal("1.5")
    fee_rate: Decimal = Decimal("0.0005")
    slippage_rate: Decimal = Decimal("0.0005")
    estimated_funding_rate: Decimal = Decimal("0.0001")
    daily_loss_pct: Decimal = Decimal("0.01")
    max_drawdown_pct: Decimal = Decimal("0.05")
    portfolio_risk_pct: Decimal = Decimal("0.0075")
    max_leverage: int = 3
    max_margin_pct: Decimal = Decimal("0.20")
    max_positions: int = 3
    max_same_direction: int = 2
    correlation_limit: Decimal = Decimal("0.80")
    candidate_count: int = 5
    max_spread_pct: Decimal = Decimal("0.0015")
    max_abs_funding_rate: Decimal = Decimal("0.001")
    max_abs_basis_pct: Decimal = Decimal("0.01")
    min_book_depth_usdt: Decimal = Decimal("50000")
    min_listing_days: int = 90
    max_volatility_percentile: Decimal = Decimal("0.99")
    entry_direction: str = "both"
    entry_trigger: str = "breakout_or_pullback"
    min_confidence: Decimal = Decimal("0.75")
    min_net_reward_risk: Decimal = Decimal("2.5")
    min_stop_atr: Decimal = Decimal("0.80")
    max_stop_atr: Decimal = Decimal("2.50")
    manual_exit_levels_enabled: bool = False
    manual_stop_atr: Decimal = Decimal("1.80")
    manual_take_profit_atr: Decimal = Decimal("5.00")
    strong_trend_entry_override_enabled: bool = False
    strong_trend_adx_min: Decimal = Decimal("30")
    trend_adx_min: Decimal = Decimal("20")
    volatility_soft_limit_percentile: Decimal = Decimal("0.75")
    volatility_hard_limit_percentile: Decimal = Decimal("0.90")
    elevated_volatility_risk_multiplier: Decimal = Decimal("0.75")
    high_volatility_risk_multiplier: Decimal = Decimal("0.50")


@dataclass
class BacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    net_profit: Decimal
    max_drawdown_pct: Decimal
    profit_factor: Decimal
    expectancy: Decimal
    win_rate: Decimal
    trades: int
    fees: Decimal
    funding: Decimal
    average_holding_bars: Decimal
    signal_rejections: dict[str, int]
    circuit_breaker_triggered: bool
    equity_curve: list[dict[str, str]]
    symbol_results: dict[str, dict[str, object]] = field(default_factory=dict)
    candidate_turnover: Decimal = Decimal("0")
    oriented_mean_ic: Decimal | None = None
    factor_risk_clippings: int = 0
    factor_fallbacks: int = 0
    slippage: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "initial_equity": str(self.initial_equity),
            "final_equity": str(self.final_equity),
            "net_profit": str(self.net_profit),
            "net_return_pct": str((self.final_equity - self.initial_equity) / self.initial_equity),
            "max_drawdown_pct": str(self.max_drawdown_pct),
            "profit_factor": str(self.profit_factor),
            "expectancy": str(self.expectancy),
            "win_rate": str(self.win_rate),
            "trades": self.trades,
            "fees": str(self.fees),
            "funding": str(self.funding),
            "slippage": str(self.slippage),
            "average_holding_bars": str(self.average_holding_bars),
            "signal_rejections": self.signal_rejections,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "equity_curve": self.equity_curve,
            "candidate_turnover": str(self.candidate_turnover),
            "oriented_mean_ic": (
                str(self.oriented_mean_ic) if self.oriented_mean_ic is not None else None
            ),
            "factor_risk_clippings": self.factor_risk_clippings,
            "factor_fallbacks": self.factor_fallbacks,
        }
        if self.symbol_results:
            result["symbols"] = self.symbol_results
        return result


class BacktestEngine:
    def run(self, candles: list[Candle], config: BacktestConfig | None = None) -> BacktestResult:
        config = config or BacktestConfig()
        self._active_config = config
        if len(candles) < 240:
            raise ValueError("at least 240 15-minute candles are required")
        equity = config.initial_equity
        peak = equity
        max_drawdown = Decimal("0")
        position: SimPosition | None = None
        pending_side: PositionSide | None = None
        trade_pnls: list[Decimal] = []
        holding_bars: list[int] = []
        current_holding = 0
        total_fees = Decimal("0")
        total_funding = Decimal("0")
        total_slippage = Decimal("0")
        curve: list[dict[str, str]] = []
        signal_rejections = {"no_aligned_signal": 0}
        circuit_breaker_triggered = False
        day_start_equity = equity
        trading_day = candles[0].open_time.date()

        for index, candle in enumerate(candles):
            if candle.open_time.date() != trading_day:
                trading_day = candle.open_time.date()
                day_start_equity = equity
            history = candles[:index]
            current_atr = atr(history[-100:]) if history else Decimal("0")
            if (
                pending_side is not None
                and position is None
                and current_atr > 0
                and not circuit_breaker_triggered
            ):
                position = self._open(pending_side, candle.open, current_atr, equity, config)
                total_fees += position.fees
                equity -= position.fees
                pending_side = None
                current_holding = 0

            if position is not None:
                current_holding += 1
                funding = self._apply_funding(position, index, config)
                equity -= funding
                total_funding += funding
                pnl, closed, exit_fees = self._manage(position, candle, current_atr, config)
                if pnl:
                    equity += pnl
                if exit_fees:
                    equity -= exit_fees
                    total_fees += exit_fees
                if closed:
                    trade_pnls.append(position.realized - position.fees)
                    holding_bars.append(current_holding)
                    total_slippage += position.slippage
                    position = None

            marked_equity = equity + self._unrealized(position, candle.close)
            peak = max(peak, marked_equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - marked_equity) / peak)
            if index % 16 == 0 or index == len(candles) - 1:
                curve.append(
                    {
                        "time": candle.close_time.isoformat(),
                        "equity": str(marked_equity),
                        "drawdown": str((peak - marked_equity) / peak if peak else 0),
                    }
                )

            if position is None and index >= 220 and not circuit_breaker_triggered:
                pending_side = self._signal(candles[: index + 1])
                if pending_side is None:
                    signal_rejections["no_aligned_signal"] += 1
            if (
                (day_start_equity - marked_equity) / day_start_equity >= config.daily_loss_pct
                or (peak - marked_equity) / peak >= config.max_drawdown_pct
            ):
                circuit_breaker_triggered = True
                pending_side = None

        if position is not None:
            fees_before_close = position.fees
            final_pnl = self._close_remaining(position, candles[-1].close, config)
            equity += final_pnl
            total_fees += position.fees - fees_before_close
            trade_pnls.append(position.realized - position.fees)
            holding_bars.append(current_holding)
            total_slippage += position.slippage

        gross_profit = sum((pnl for pnl in trade_pnls if pnl > 0), Decimal("0"))
        gross_loss = -sum((pnl for pnl in trade_pnls if pnl < 0), Decimal("0"))
        trades = len(trade_pnls)
        wins = sum(pnl > 0 for pnl in trade_pnls)
        return BacktestResult(
            initial_equity=config.initial_equity,
            final_equity=equity,
            net_profit=equity - config.initial_equity,
            max_drawdown_pct=max_drawdown,
            profit_factor=(gross_profit / gross_loss) if gross_loss else Decimal("0"),
            expectancy=(sum(trade_pnls, Decimal("0")) / Decimal(trades))
            if trades
            else Decimal("0"),
            win_rate=(Decimal(wins) / Decimal(trades)) if trades else Decimal("0"),
            trades=trades,
            fees=total_fees,
            funding=total_funding,
            average_holding_bars=(Decimal(sum(holding_bars)) / Decimal(trades))
            if trades
            else Decimal("0"),
            signal_rejections=signal_rejections,
            circuit_breaker_triggered=circuit_breaker_triggered,
            equity_curve=curve,
            slippage=total_slippage,
        )

    def _signal(
        self, candles: list[Candle], config: BacktestConfig | None = None
    ) -> PositionSide | None:
        if config is None:
            config = getattr(self, "_active_config", None)
        if config is None:
            config = BacktestConfig()
        candles_15m = candles[-480:]
        candles_1h = aggregate_candles(candles, 4)
        candles_4h = aggregate_candles(candles, 16)
        universe = UniverseSymbol(
            symbol="BACKTEST",
            status="TRADING",
            listing_days=365,
            quote_volume_24h=Decimal("1000000"),
            best_bid=candles[-1].close * Decimal("0.9999"),
            best_ask=candles[-1].close * Decimal("1.0001"),
            mark_price=candles[-1].close,
            index_price=candles[-1].close,
            funding_rate=Decimal("0"),
        )
        snapshot = build_snapshot(
            universe,
            candles_15m,
            candles_1h,
            candles_4h,
            Decimal("1000000"),
            book_depth_usdt=Decimal("1000000"),
            trend_adx_min=config.trend_adx_min,
            volatility_soft_limit_percentile=config.volatility_soft_limit_percentile,
            volatility_hard_limit_percentile=config.volatility_hard_limit_percentile,
            elevated_volatility_risk_multiplier=config.elevated_volatility_risk_multiplier,
            high_volatility_risk_multiplier=config.high_volatility_risk_multiplier,
        )
        eligible, _ = MarketScreener(
            max_spread_pct=config.max_spread_pct,
            max_abs_funding_rate=config.max_abs_funding_rate,
            max_abs_basis_pct=config.max_abs_basis_pct,
            min_book_depth_usdt=config.min_book_depth_usdt,
            min_listing_days=config.min_listing_days,
            max_volatility_percentile=config.max_volatility_percentile,
            entry_trigger=config.entry_trigger,
            trend_adx_min=config.trend_adx_min,
            strong_trend_entry_override_enabled=config.strong_trend_entry_override_enabled,
            strong_trend_adx_min=config.strong_trend_adx_min,
            volatility_soft_limit_percentile=config.volatility_soft_limit_percentile,
            volatility_hard_limit_percentile=config.volatility_hard_limit_percentile,
        ).eligible(snapshot)
        if not eligible:
            return None
        trend_1h = trend_direction(candles_1h)
        trend_4h = trend_direction(candles_4h)
        if trend_1h == 0 or trend_1h != trend_4h:
            return None
        if config.entry_direction == "long_only" and trend_1h != 1:
            return None
        if config.entry_direction == "short_only" and trend_1h != -1:
            return None
        breakout = donchian_breakout(candles)
        pullback = pullback_signal(candles, trend_1h)
        if (
            breakout != trend_1h
            and pullback != trend_1h
            and not (
                config.strong_trend_entry_override_enabled
                and trend_1h == 1
                and trend_4h == 1
                and snapshot.market_regime == "TRENDING"
                and snapshot.adx_1h >= config.strong_trend_adx_min
            )
        ):
            return None
        return PositionSide.LONG if trend_1h == 1 else PositionSide.SHORT

    @staticmethod
    def _open(
        side: PositionSide,
        raw_entry: Decimal,
        current_atr: Decimal,
        equity: Decimal,
        config: BacktestConfig,
    ) -> SimPosition:
        entry = raw_entry * (
            Decimal("1") + config.slippage_rate
            if side == PositionSide.LONG
            else Decimal("1") - config.slippage_rate
        )
        stop_atr = (
            config.manual_stop_atr
            if config.manual_exit_levels_enabled
            else config.stop_atr
        )
        risk_distance = current_atr * stop_atr
        risk_amount = equity * config.risk_pct
        quantity = risk_amount / risk_distance
        direction = Decimal("1") if side == PositionSide.LONG else Decimal("-1")
        fee = quantity * entry * config.fee_rate
        entry_slippage = abs(entry - raw_entry) * quantity
        stop = entry - direction * risk_distance
        final_distance = (
            current_atr * config.manual_take_profit_atr
            if config.manual_exit_levels_enabled
            else risk_distance * Decimal("2")
        )
        tp2 = entry + direction * final_distance
        return SimPosition(
            side=side,
            quantity=quantity,
            remaining=quantity,
            entry=entry,
            stop=stop,
            tp1=first_take_profit(
                entry=entry,
                stop_price=stop,
                final_target=tp2,
                side=side,
            ),
            tp2=tp2,
            highest=entry,
            lowest=entry,
            fees=fee,
            slippage=entry_slippage,
        )

    def _manage(
        self,
        position: SimPosition,
        candle: Candle,
        current_atr: Decimal,
        config: BacktestConfig,
    ) -> tuple[Decimal, bool, Decimal]:
        position.highest = max(position.highest, candle.high)
        position.lowest = min(position.lowest, candle.low)
        stop_hit = (
            candle.low <= position.stop
            if position.side == PositionSide.LONG
            else candle.high >= position.stop
        )
        if stop_hit:
            pnl, fee = self._realize(position, position.stop, position.remaining, config)
            return pnl, True, fee

        realized_now = Decimal("0")
        fees_now = Decimal("0")
        if not position.tp1_hit:
            tp1_hit = (
                candle.high >= position.tp1
                if position.side == PositionSide.LONG
                else candle.low <= position.tp1
            )
            if tp1_hit:
                quantity = position.quantity * TP1_FRACTION
                pnl, fee = self._realize(position, position.tp1, quantity, config)
                realized_now += pnl
                fees_now += fee
                position.tp1_hit = True
                position.stop = breakeven_stop(
                    position.entry,
                    position.side,
                    config.fee_rate * Decimal("2"),
                )

        if position.tp1_hit and not position.tp2_hit:
            tp2_hit = (
                candle.high >= position.tp2
                if position.side == PositionSide.LONG
                else candle.low <= position.tp2
            )
            if tp2_hit:
                quantity = min(position.quantity * TP2_FRACTION, position.remaining)
                pnl, fee = self._realize(position, position.tp2, quantity, config)
                realized_now += pnl
                fees_now += fee
                position.tp2_hit = True

        if position.tp2_hit and position.remaining > 0 and current_atr > 0:
            position.stop = trailing_stop(
                current_stop=position.stop,
                side=position.side,
                highest=position.highest,
                lowest=position.lowest,
                atr=current_atr,
                atr_multiple=config.trailing_atr,
            )
        return realized_now, position.remaining <= 0, fees_now

    @staticmethod
    def _realize(
        position: SimPosition, raw_exit: Decimal, quantity: Decimal, config: BacktestConfig
    ) -> tuple[Decimal, Decimal]:
        quantity = min(quantity, position.remaining)
        exit_price = raw_exit * (
            Decimal("1") - config.slippage_rate
            if position.side == PositionSide.LONG
            else Decimal("1") + config.slippage_rate
        )
        direction = Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
        pnl = (exit_price - position.entry) * quantity * direction
        fee = exit_price * quantity * config.fee_rate
        position.slippage += abs(exit_price - raw_exit) * quantity
        position.realized += pnl
        position.fees += fee
        position.remaining -= quantity
        return pnl, fee

    def _close_remaining(
        self, position: SimPosition, price: Decimal, config: BacktestConfig
    ) -> Decimal:
        pnl, fee = self._realize(position, price, position.remaining, config)
        return pnl - fee

    @staticmethod
    def _unrealized(position: SimPosition | None, price: Decimal) -> Decimal:
        if position is None:
            return Decimal("0")
        direction = Decimal("1") if position.side == PositionSide.LONG else Decimal("-1")
        return (price - position.entry) * position.remaining * direction

    @staticmethod
    def _apply_funding(position: SimPosition, index: int, config: BacktestConfig) -> Decimal:
        if index % 32 != 0:
            return Decimal("0")
        notional = position.entry * position.remaining
        return notional * config.estimated_funding_rate


def aggregate_candles(candles: list[Candle], factor: int) -> list[Candle]:
    result: list[Candle] = []
    complete = len(candles) - len(candles) % factor
    for start in range(0, complete, factor):
        group = candles[start : start + factor]
        result.append(
            Candle(
                open_time=group[0].open_time,
                close_time=group[-1].close_time,
                open=group[0].open,
                high=max(item.high for item in group),
                low=min(item.low for item in group),
                close=group[-1].close,
                volume=sum((item.volume for item in group), Decimal("0")),
            )
        )
    return result
