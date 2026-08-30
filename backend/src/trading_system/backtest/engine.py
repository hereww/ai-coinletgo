from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading_system.domain.enums import PositionSide
from trading_system.domain.models import Candle, UniverseSymbol
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
            "average_holding_bars": str(self.average_holding_bars),
            "signal_rejections": self.signal_rejections,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "equity_curve": self.equity_curve,
        }
        if self.symbol_results:
            result["symbols"] = self.symbol_results
        return result


class BacktestEngine:
    def run(self, candles: list[Candle], config: BacktestConfig | None = None) -> BacktestResult:
        config = config or BacktestConfig()
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
        )

    @staticmethod
    def _signal(candles: list[Candle]) -> PositionSide | None:
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
        )
        eligible, _ = MarketScreener().eligible(snapshot)
        if not eligible:
            return None
        trend_1h = trend_direction(candles_1h)
        trend_4h = trend_direction(candles_4h)
        if trend_1h == 0 or trend_1h != trend_4h:
            return None
        breakout = donchian_breakout(candles)
        pullback = pullback_signal(candles, trend_1h)
        if breakout != trend_1h and pullback != trend_1h:
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
        risk_distance = current_atr * config.stop_atr
        risk_amount = equity * config.risk_pct
        quantity = risk_amount / risk_distance
        direction = Decimal("1") if side == PositionSide.LONG else Decimal("-1")
        fee = quantity * entry * config.fee_rate
        return SimPosition(
            side=side,
            quantity=quantity,
            remaining=quantity,
            entry=entry,
            stop=entry - direction * risk_distance,
            tp1=entry + direction * risk_distance,
            tp2=entry + direction * risk_distance * Decimal("2"),
            highest=entry,
            lowest=entry,
            fees=fee,
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
                quantity = position.quantity * Decimal("0.4")
                pnl, fee = self._realize(position, position.tp1, quantity, config)
                realized_now += pnl
                fees_now += fee
                position.tp1_hit = True
                fee_buffer = position.entry * config.fee_rate * Decimal("2")
                position.stop = position.entry + (
                    fee_buffer if position.side == PositionSide.LONG else -fee_buffer
                )

        if position.tp1_hit and not position.tp2_hit:
            tp2_hit = (
                candle.high >= position.tp2
                if position.side == PositionSide.LONG
                else candle.low <= position.tp2
            )
            if tp2_hit:
                quantity = min(position.quantity * Decimal("0.4"), position.remaining)
                pnl, fee = self._realize(position, position.tp2, quantity, config)
                realized_now += pnl
                fees_now += fee
                position.tp2_hit = True

        if position.tp2_hit and position.remaining > 0 and current_atr > 0:
            if position.side == PositionSide.LONG:
                position.stop = max(
                    position.stop, position.highest - current_atr * config.trailing_atr
                )
            else:
                position.stop = min(
                    position.stop, position.lowest + current_atr * config.trailing_atr
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
