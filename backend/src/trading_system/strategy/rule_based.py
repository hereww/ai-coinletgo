from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from trading_system.config import Settings
from trading_system.domain.enums import PositionSide, SignalAction
from trading_system.domain.models import MarketSnapshot, TradeSignal
from trading_system.strategy.exit_policy import manual_atr_exit_prices


class RuleBasedStrategy:
    """Build bounded trend-continuation signals without a remote model."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_signals(
        self,
        candidates: list[MarketSnapshot],
        cycle_expires_at: datetime,
    ) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        for snapshot in candidates:
            if not self.eligible(snapshot):
                continue
            signal = self._build_signal(snapshot, cycle_expires_at)
            if signal is not None:
                signals.append(signal)
        return signals

    def eligible(self, snapshot: MarketSnapshot) -> bool:
        """Keep the local fallback independent from remote model discretion."""

        if snapshot.status != "TRADING":
            return False
        if snapshot.listing_days < self.settings.min_listing_days:
            return False
        if snapshot.spread_pct > Decimal(str(self.settings.max_spread_pct)):
            return False
        if abs(snapshot.funding_rate) > Decimal(str(self.settings.max_abs_funding_rate)):
            return False
        if abs(snapshot.basis_pct) > Decimal(str(self.settings.max_abs_basis_pct)):
            return False
        if snapshot.book_depth_usdt < Decimal(str(self.settings.min_book_depth_usdt)):
            return False
        if snapshot.volatility_percentile > Decimal(
            str(self.settings.volatility_hard_limit_percentile)
        ):
            return False
        if snapshot.market_regime != "TRENDING":
            return False
        if snapshot.trend_1h != snapshot.trend_4h or snapshot.trend_1h == 0:
            return False
        if snapshot.adx_1h < Decimal(str(self.settings.trend_adx_min)):
            return False
        if self.settings.entry_direction == "long_only" and snapshot.trend_1h != 1:
            return False
        if self.settings.entry_direction == "short_only" and snapshot.trend_1h != -1:
            return False

        trigger = self._trigger_matches(snapshot)
        strong_uptrend = (
            snapshot.trend_1h == 1
            and snapshot.adx_1h >= Decimal(str(self.settings.strong_trend_adx_min))
        )
        # When the remote model is disabled, the local strategy must still
        # have a usable entry path during a persistent trend. Requiring a
        # fresh 15m breakout/retest on every cycle made a valid 1h/4h trend
        # look like NO_TRADE all day. This path changes only opportunity
        # selection; the normal hard-risk gate still owns the final decision.
        local_continuation = (
            not self.settings.model_strategy_enabled
            and snapshot.market_regime == "TRENDING"
            and snapshot.trend_1h == snapshot.trend_4h
            and snapshot.adx_1h >= Decimal(str(self.settings.trend_adx_min))
        )
        return trigger or strong_uptrend or local_continuation

    def _trigger_matches(self, snapshot: MarketSnapshot) -> bool:
        direction = snapshot.trend_1h
        if self.settings.entry_trigger == "breakout_only":
            return snapshot.breakout_15m == direction
        if self.settings.entry_trigger == "pullback_only":
            return snapshot.pullback_15m == direction
        return snapshot.breakout_15m == direction or snapshot.pullback_15m == direction

    def _build_signal(
        self, snapshot: MarketSnapshot, cycle_expires_at: datetime
    ) -> TradeSignal | None:
        side = SignalAction.OPEN_LONG if snapshot.trend_1h == 1 else SignalAction.OPEN_SHORT
        entry = snapshot.mid_price
        entry_width = max(
            snapshot.best_ask - snapshot.best_bid,
            snapshot.atr_15m * Decimal("0.20"),
        )
        # ATR can be numerically valid while still being impossible to trade
        # (for example, a bad candle can make ATR larger than the instrument
        # price).  Reject that candidate before Pydantic or the risk engine
        # sees negative/absurd entry geometry and aborts the whole cycle.
        if entry <= 0 or entry_width <= 0 or entry_width >= entry * Decimal("0.50"):
            return None
        entry_min = entry - entry_width / Decimal("2")
        entry_max = entry + entry_width / Decimal("2")
        stop_atr = max(
            Decimal(str(self.settings.min_stop_atr)),
            Decimal(str(self.settings.manual_stop_atr))
            if self.settings.manual_exit_levels_enabled
            else Decimal("1.80"),
        )
        target_atr = (
            Decimal(str(self.settings.manual_take_profit_atr))
            if self.settings.manual_exit_levels_enabled
            else Decimal("5.00")
        )
        position_side = PositionSide.LONG if side == SignalAction.OPEN_LONG else PositionSide.SHORT
        stop_price, target_price = manual_atr_exit_prices(
            side=position_side,
            entry_min=entry_min,
            entry_max=entry_max,
            atr=snapshot.atr_15m,
            stop_atr=stop_atr,
            target_atr=target_atr,
        )
        target_price = self._raise_target_to_rr_floor(
            side=side,
            entry=entry,
            stop_price=stop_price,
            target_price=target_price,
            funding_rate=snapshot.funding_rate,
        )
        if (
            entry_min <= 0
            or entry_max <= 0
            or stop_price <= 0
            or target_price <= 0
            or entry_min > entry_max
            or (
                side == SignalAction.OPEN_LONG
                and not stop_price < entry_min < entry_max < target_price
            )
            or (
                side == SignalAction.OPEN_SHORT
                and not target_price < entry_min < entry_max < stop_price
            )
        ):
            return None
        confidence = self._confidence(snapshot)
        trigger = snapshot.breakout_15m == snapshot.trend_1h or (
            snapshot.pullback_15m == snapshot.trend_1h
        )
        strong_uptrend = (
            snapshot.trend_1h == 1
            and snapshot.adx_1h >= Decimal(str(self.settings.strong_trend_adx_min))
        )
        reason_codes = ["RULE_TREND_CONTINUATION"]
        if snapshot.trend_1h == snapshot.trend_4h:
            reason_codes.append("TREND_ALIGNED_1H_4H")
        if snapshot.breakout_15m == snapshot.trend_1h:
            reason_codes.append("BREAKOUT_15M")
        elif snapshot.pullback_15m == snapshot.trend_1h:
            reason_codes.append("PULLBACK_15M")
        elif strong_uptrend:
            reason_codes.append("STRONG_TREND_CONTINUATION")
        elif not self.settings.model_strategy_enabled:
            reason_codes.append("LOCAL_TREND_CONTINUATION")
        else:
            reason_codes.append("STRONG_TREND_CONTINUATION")
        if trigger:
            thesis = "1小时与4小时趋势一致，按趋势延续规则开仓，已有15分钟触发确认。"
        elif not self.settings.model_strategy_enabled:
            thesis = (
                "模型策略已关闭，1小时与4小时趋势一致且达到最低趋势强度，"
                "按本地趋势延续规则开仓；全部硬风控仍然生效。"
            )
        else:
            thesis = (
                "1小时与4小时趋势一致，按趋势延续规则开仓，ADX达到强趋势阈值，"
                "允许无15分钟触发开仓。"
            )
        signal_id = uuid5(
            NAMESPACE_URL,
            f"rule-v1:{snapshot.symbol}:{snapshot.timestamp.isoformat()}",
        )
        return TradeSignal(
            signal_id=signal_id,
            symbol=snapshot.symbol,
            action=side,
            confidence=confidence,
            entry_min=entry_min,
            entry_max=entry_max,
            invalidation_price=stop_price,
            target_price=target_price,
            horizon_minutes=240,
            thesis=thesis,
            reason_codes=reason_codes[:8],
            risk_flags=["MODEL_DISABLED", "LOCAL_RISK_ENGINE_REQUIRED"],
            created_at=datetime.now(UTC),
            expires_at=cycle_expires_at,
        )

    def _raise_target_to_rr_floor(
        self,
        *,
        side: SignalAction,
        entry: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        funding_rate: Decimal,
    ) -> Decimal:
        cost = entry * Decimal("0.0015") + entry * abs(funding_rate)
        distance = abs(entry - stop_price)
        required_reward = Decimal(str(self.settings.min_net_reward_risk)) * (
            distance + cost
        ) + cost
        target_distance = abs(target_price - entry)
        if target_distance < required_reward:
            target_distance = required_reward
        return (
            entry + target_distance
            if side == SignalAction.OPEN_LONG
            else entry - target_distance
        )

    def _confidence(self, snapshot: MarketSnapshot) -> Decimal:
        base = Decimal(str(self.settings.min_confidence))
        adx_bonus = min(snapshot.adx_1h / Decimal("200"), Decimal("0.12"))
        trigger_bonus = (
            Decimal("0.05")
            if snapshot.breakout_15m == snapshot.trend_1h
            or snapshot.pullback_15m == snapshot.trend_1h
            else Decimal("0")
        )
        return min(Decimal("0.99"), max(base, base + adx_bonus + trigger_bonus))
