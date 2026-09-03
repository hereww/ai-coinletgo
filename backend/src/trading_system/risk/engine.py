from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from trading_system.domain.enums import DecisionStatus, PositionSide, SignalAction, SystemMode
from trading_system.domain.models import (
    ExecutionIntent,
    MarketSnapshot,
    RiskContext,
    RiskDecision,
    TradeSignal,
)


class RiskEngine:
    def check_circuit_breakers(self, context: RiskContext) -> list[str]:
        reasons: list[str] = []
        if context.account.daily_equity_loss_pct >= context.limits.daily_loss_pct:
            reasons.append("daily_loss_limit_reached")
        if context.account.drawdown_pct >= context.limits.max_drawdown_pct:
            reasons.append("max_drawdown_reached")
        return reasons

    def evaluate(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        context: RiskContext,
    ) -> RiskDecision:
        reasons = self._preflight(signal, snapshot, context)
        if reasons:
            return self._reject_with_prices(signal, snapshot, reasons, context)

        entry = self._entry_price(signal, snapshot)
        stop = signal.invalidation_price or Decimal("0")
        target = signal.target_price or Decimal("0")
        stop_distance = abs(entry - stop)
        raw_reward = abs(target - entry)
        round_trip_cost = entry * (
            context.estimated_fee_rate * Decimal("2") + context.estimated_slippage_rate
        )
        estimated_funding_cost = (
            entry
            * abs(snapshot.funding_rate)
            * max(Decimal("1"), Decimal(signal.horizon_minutes) / Decimal("480"))
        )
        round_trip_cost += estimated_funding_cost
        net_reward = max(Decimal("0"), raw_reward - round_trip_cost)
        net_reward_risk = net_reward / (stop_distance + round_trip_cost)
        if net_reward_risk < context.limits.min_net_reward_risk:
            return self._reject(signal, ["net_reward_risk_below_minimum"])

        capital_base = min(context.account.equity, context.limits.capital_limit_usdt)
        risk_multiplier = snapshot.volatility_risk_multiplier
        risk_amount = (
            capital_base * context.limits.single_trade_risk_pct * risk_multiplier
        )
        current_initial_risk = sum(
            (position.initial_risk_usdt for position in context.positions), Decimal("0")
        )
        portfolio_risk_cap = capital_base * context.limits.portfolio_risk_pct
        available_risk = max(Decimal("0"), portfolio_risk_cap - current_initial_risk)
        risk_amount = min(risk_amount, available_risk)
        if risk_amount <= 0:
            return self._reject(signal, ["portfolio_risk_capacity_exhausted"])

        raw_quantity = risk_amount / stop_distance
        quantity = self._round_down(raw_quantity, context.filters.step_size)
        max_margin = capital_base * context.limits.max_margin_pct
        available_margin = max(Decimal("0"), max_margin - context.account.total_margin_used)
        max_notional = available_margin * Decimal(context.limits.max_leverage)
        max_quantity = self._round_down(max_notional / entry, context.filters.step_size)
        quantity = min(quantity, max_quantity)
        if context.filters.max_quantity is not None:
            quantity = min(quantity, context.filters.max_quantity)

        if quantity < context.filters.min_quantity:
            return self._reject(signal, ["quantity_below_exchange_minimum"])
        if quantity * entry < context.filters.min_notional:
            return self._reject(signal, ["notional_below_exchange_minimum"])

        entry = self._round_price(entry, context.filters.tick_size, ROUND_HALF_UP)
        if signal.action == SignalAction.OPEN_LONG:
            stop = self._round_price(stop, context.filters.tick_size, ROUND_DOWN)
            target = self._round_price(target, context.filters.tick_size, ROUND_UP)
        else:
            stop = self._round_price(stop, context.filters.tick_size, ROUND_UP)
            target = self._round_price(target, context.filters.tick_size, ROUND_DOWN)
        rounded_stop_distance = abs(entry - stop)
        if rounded_stop_distance <= 0:
            return self._reject(signal, ["rounded_stop_invalid"])
        quantity = min(
            quantity,
            self._round_down(risk_amount / rounded_stop_distance, context.filters.step_size),
        )
        if context.filters.max_quantity is not None:
            quantity = min(quantity, context.filters.max_quantity)
        if quantity < context.filters.min_quantity:
            return self._reject(signal, ["quantity_below_exchange_minimum"])
        if quantity * entry < context.filters.min_notional:
            return self._reject(signal, ["notional_below_exchange_minimum"])
        estimated_margin = quantity * entry / Decimal(context.limits.max_leverage)
        return RiskDecision(
            signal_id=signal.signal_id,
            status=DecisionStatus.APPROVED,
            reasons=["all_hard_limits_passed"],
            capital_base=capital_base,
            risk_amount_usdt=quantity * abs(entry - stop),
            quantity=quantity,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            leverage=context.limits.max_leverage,
            estimated_margin=estimated_margin,
            net_reward_risk=net_reward_risk,
            risk_multiplier=risk_multiplier,
        )

    def build_execution_intent(
        self, signal: TradeSignal, decision: RiskDecision
    ) -> ExecutionIntent:
        if decision.status != DecisionStatus.APPROVED:
            raise ValueError("cannot execute a rejected risk decision")
        risk_per_unit = abs(decision.entry_price - decision.stop_price)
        if signal.action == SignalAction.OPEN_LONG:
            side = PositionSide.LONG
            tp1 = decision.entry_price + risk_per_unit
            tp2 = decision.entry_price + risk_per_unit * Decimal("2")
        elif signal.action == SignalAction.OPEN_SHORT:
            side = PositionSide.SHORT
            tp1 = decision.entry_price - risk_per_unit
            tp2 = decision.entry_price - risk_per_unit * Decimal("2")
        else:
            raise ValueError("no-trade signals cannot be executed")
        return ExecutionIntent(
            intent_id=signal.signal_id,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=side,
            quantity=decision.quantity,
            limit_price=decision.entry_price,
            entry_min=signal.entry_min or decision.entry_price,
            entry_max=signal.entry_max or decision.entry_price,
            stop_price=decision.stop_price,
            tp1_price=tp1,
            tp2_price=tp2,
            leverage=decision.leverage,
            expires_at=signal.expires_at,
        )

    def _preflight(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        context: RiskContext,
    ) -> list[str]:
        reasons = self.check_circuit_breakers(context)
        if context.mode not in {SystemMode.TESTNET, SystemMode.LIVE_ENABLED}:
            reasons.append("system_mode_disallows_entries")
        if signal.action == SignalAction.NO_TRADE:
            reasons.append("model_returned_no_trade")
        if (
            context.limits.entry_direction == "long_only"
            and signal.action == SignalAction.OPEN_SHORT
        ) or (
            context.limits.entry_direction == "short_only"
            and signal.action == SignalAction.OPEN_LONG
        ):
            reasons.append("entry_direction_not_allowed")
        if signal.expires_at <= snapshot.timestamp:
            reasons.append("signal_expired")
        if signal.symbol != snapshot.symbol:
            reasons.append("signal_snapshot_symbol_mismatch")
        if signal.confidence < context.limits.min_confidence:
            reasons.append("confidence_below_minimum")
        if snapshot.market_regime != "TRENDING":
            reasons.append("market_regime_not_trending")
        if snapshot.adx_1h < context.limits.trend_adx_min:
            reasons.append("trend_strength_below_minimum")
        if not self._trend_matches_signal(signal, snapshot):
            reasons.append("trend_not_aligned")
        if not self._entry_trigger_matches(signal, snapshot, context.limits.entry_trigger):
            reasons.append("no_aligned_entry_trigger")
        if snapshot.volatility_risk_multiplier <= 0:
            reasons.append("invalid_volatility_risk_multiplier")
        if len(context.positions) >= context.limits.max_positions:
            reasons.append("position_count_limit_reached")

        desired_side = (
            PositionSide.LONG if signal.action == SignalAction.OPEN_LONG else PositionSide.SHORT
        )
        same_direction = sum(position.side == desired_side for position in context.positions)
        if same_direction >= context.limits.max_same_direction:
            reasons.append("same_direction_limit_reached")
        if any(position.symbol == signal.symbol for position in context.positions):
            reasons.append("existing_symbol_position")
        for position in context.positions:
            correlation = abs(context.correlations.get(position.symbol, Decimal("1")))
            if correlation > context.limits.correlation_limit:
                reasons.append(f"correlated_with_{position.symbol}")
                break

        if signal.invalidation_price is not None:
            entry = self._entry_price(signal, snapshot)
            if snapshot.atr_15m <= 0:
                reasons.append("atr_invalid")
                return reasons
            stop_atr = abs(entry - signal.invalidation_price) / snapshot.atr_15m
            if stop_atr < context.limits.min_stop_atr:
                reasons.append("stop_too_close")
            if stop_atr > context.limits.max_stop_atr:
                reasons.append("stop_too_far")
        return reasons

    @staticmethod
    def _trend_matches_signal(signal: TradeSignal, snapshot: MarketSnapshot) -> bool:
        direction = 1 if signal.action == SignalAction.OPEN_LONG else -1
        return snapshot.trend_1h == direction and snapshot.trend_4h == direction

    @staticmethod
    def _entry_trigger_matches(
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        entry_trigger: str,
    ) -> bool:
        direction = 1 if signal.action == SignalAction.OPEN_LONG else -1
        if entry_trigger == "breakout_only":
            return snapshot.breakout_15m == direction
        if entry_trigger == "pullback_only":
            return snapshot.pullback_15m == direction
        return snapshot.breakout_15m == direction or snapshot.pullback_15m == direction

    @staticmethod
    def _entry_price(signal: TradeSignal, snapshot: MarketSnapshot) -> Decimal:
        if signal.entry_min is None or signal.entry_max is None:
            return snapshot.mid_price
        midpoint = (signal.entry_min + signal.entry_max) / Decimal("2")
        return (
            min(max(snapshot.mid_price, signal.entry_min), signal.entry_max)
            if (signal.entry_min <= snapshot.mid_price <= signal.entry_max)
            else midpoint
        )

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _round_price(value: Decimal, tick: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
        return (value / tick).to_integral_value(rounding=rounding) * tick

    @staticmethod
    def _reject(signal: TradeSignal, reasons: list[str]) -> RiskDecision:
        return RiskDecision(
            signal_id=signal.signal_id, status=DecisionStatus.REJECTED, reasons=reasons
        )

    def _reject_with_prices(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        reasons: list[str],
        context: RiskContext,
    ) -> RiskDecision:
        """Retain normalized prices on rejected decisions for audit/debugging."""
        entry = self._entry_price(signal, snapshot)
        stop = signal.invalidation_price or Decimal("0")
        target = signal.target_price or Decimal("0")
        if stop > 0:
            entry = self._round_price(entry, context.filters.tick_size, ROUND_HALF_UP)
            if signal.action == SignalAction.OPEN_LONG:
                stop = self._round_price(stop, context.filters.tick_size, ROUND_DOWN)
                if target > 0:
                    target = self._round_price(target, context.filters.tick_size, ROUND_UP)
            elif signal.action == SignalAction.OPEN_SHORT:
                stop = self._round_price(stop, context.filters.tick_size, ROUND_UP)
                if target > 0:
                    target = self._round_price(target, context.filters.tick_size, ROUND_DOWN)
        return RiskDecision(
            signal_id=signal.signal_id,
            status=DecisionStatus.REJECTED,
            reasons=reasons,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
        )
