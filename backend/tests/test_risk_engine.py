from decimal import Decimal

import pytest

from tests.factories import context, position, signal, snapshot
from trading_system.domain.enums import DecisionStatus, PositionSide, SystemMode
from trading_system.domain.models import AccountState, ExchangeFilters
from trading_system.risk.engine import RiskEngine


def test_sizes_from_stop_distance_and_rounds_down() -> None:
    trade_signal = signal()
    engine = RiskEngine()
    decision = engine.evaluate(
        trade_signal,
        snapshot(),
        context(
            filters=ExchangeFilters(
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.3"),
                min_quantity=Decimal("0.1"),
                min_notional=Decimal("5"),
            )
        ),
    )
    assert decision.status == DecisionStatus.APPROVED
    assert decision.quantity == Decimal("2.4")
    assert decision.risk_amount_usdt == Decimal("2.40")
    assert decision.leverage == 3
    assert engine.build_execution_intent(trade_signal, decision).intent_id == trade_signal.signal_id


def test_configured_leverage_can_reach_30_without_increasing_risk_amount() -> None:
    engine = RiskEngine()
    baseline = engine.evaluate(signal(), snapshot(), context())
    limits = context().limits.model_copy(update={"max_leverage": 30})
    leveraged = engine.evaluate(signal(), snapshot(), context(limits=limits))
    assert leveraged.status == DecisionStatus.APPROVED
    assert leveraged.leverage == 30
    assert leveraged.risk_amount_usdt == baseline.risk_amount_usdt
    assert leveraged.estimated_margin < baseline.estimated_margin


def test_strong_uptrend_override_allows_long_without_15m_trigger() -> None:
    limits = context().limits.model_copy(
        update={"strong_trend_entry_override_enabled": True, "strong_trend_adx_min": 30}
    )
    decision = RiskEngine().evaluate(
        signal(),
        snapshot(breakout_15m=0, pullback_15m=0),
        context(limits=limits),
    )
    assert decision.status == DecisionStatus.APPROVED
    assert "strong_trend_entry_override" in decision.reasons


def test_model_primary_accepts_low_confidence_ranging_signal() -> None:
    limits = context().limits.model_copy(
        update={"model_primary_portfolio_enabled": True, "min_net_reward_risk": 3}
    )
    result = RiskEngine().evaluate(
        signal(confidence=Decimal("0.1"), target_price=Decimal("100.2")),
        snapshot(
            market_regime="RANGING",
            trend_1h=-1,
            trend_4h=-1,
            adx_1h=Decimal("5"),
            breakout_15m=0,
            pullback_15m=0,
        ),
        context(limits=limits),
    )
    assert result.status == DecisionStatus.APPROVED
    assert "model_primary_opportunity_accepted" in result.reasons


def test_model_primary_keeps_hard_stop_and_margin_guards() -> None:
    limits = context().limits.model_copy(update={"model_primary_portfolio_enabled": True})
    result = RiskEngine().evaluate(
        signal(invalidation_price=Decimal("99.5")),
        snapshot(),
        context(limits=limits),
    )
    assert result.status == DecisionStatus.REJECTED
    assert "stop_too_close" in result.reasons


def test_strong_uptrend_override_bypasses_opportunity_filters() -> None:
    limits = context().limits.model_copy(
        update={
            "strong_trend_entry_override_enabled": True,
            "strong_trend_adx_min": Decimal("30"),
            "max_same_direction": 1,
            "correlation_limit": Decimal("0.1"),
        }
    )
    existing = position(symbol="ETHUSDT", side=PositionSide.LONG)
    decision = RiskEngine().evaluate(
        signal(confidence=Decimal("0.1"), target_price=Decimal("100.2")),
        snapshot(breakout_15m=1, pullback_15m=0),
        context(
            limits=limits,
            positions=[existing],
            correlations={"ETHUSDT": Decimal("0.99")},
        ),
    )

    assert decision.status == DecisionStatus.APPROVED
    assert "strong_trend_entry_override" in decision.reasons


def test_strong_uptrend_override_keeps_circuit_breakers() -> None:
    limits = context().limits.model_copy(
        update={"strong_trend_entry_override_enabled": True}
    )
    account = AccountState(
        equity=Decimal("940"),
        available_balance=Decimal("900"),
        day_start_equity=Decimal("950"),
        high_water_mark=Decimal("1000"),
    )
    decision = RiskEngine().evaluate(
        signal(confidence=Decimal("0.1"), target_price=Decimal("100.2")),
        snapshot(breakout_15m=0, pullback_15m=0),
        context(limits=limits, account=account),
    )

    assert decision.status == DecisionStatus.REJECTED
    assert "daily_loss_limit_reached" in decision.reasons
    assert "max_drawdown_reached" in decision.reasons


def test_strong_uptrend_override_does_not_bypass_short_trigger() -> None:
    limits = context().limits.model_copy(
        update={"strong_trend_entry_override_enabled": True, "strong_trend_adx_min": 30}
    )
    decision = RiskEngine().evaluate(
        signal(
            action="OPEN_SHORT",
            invalidation_price=Decimal("101"),
            target_price=Decimal("97"),
        ),
        snapshot(
            trend_1h=-1,
            trend_4h=-1,
            breakout_15m=0,
            pullback_15m=0,
        ),
        context(limits=limits),
    )
    assert decision.status == DecisionStatus.REJECTED
    assert "no_aligned_entry_trigger" in decision.reasons


def test_high_volatility_reduces_risk_budget_without_changing_leverage() -> None:
    engine = RiskEngine()
    baseline = engine.evaluate(signal(), snapshot(), context())
    reduced = engine.evaluate(
        signal(),
        snapshot(volatility_risk_multiplier=Decimal("0.5")),
        context(),
    )
    assert reduced.status == DecisionStatus.APPROVED
    assert reduced.risk_multiplier == Decimal("0.5")
    assert reduced.risk_amount_usdt < baseline.risk_amount_usdt
    assert reduced.leverage == baseline.leverage


@pytest.mark.parametrize("regime", ["RANGING", "VOLATILE", "UNCERTAIN"])
def test_new_entries_require_trending_market_regime(regime: str) -> None:
    decision = RiskEngine().evaluate(signal(), snapshot(market_regime=regime), context())
    assert decision.status == DecisionStatus.REJECTED
    assert "market_regime_not_trending" in decision.reasons


def test_stop_rounding_is_conservative_for_long_and_short() -> None:
    engine = RiskEngine()
    long_signal = signal(invalidation_price=Decimal("99.04"), target_price=Decimal("104"))
    short_signal = signal(
        action="OPEN_SHORT", entry_min=Decimal("99.5"), entry_max=Decimal("100.5"),
        invalidation_price=Decimal("100.96"), target_price=Decimal("96"),
    )
    filters = ExchangeFilters(
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.1"),
        min_quantity=Decimal("0.1"),
        min_notional=Decimal("5"),
    )
    long_decision = engine.evaluate(long_signal, snapshot(), context(filters=filters))
    short_decision = engine.evaluate(short_signal, snapshot(), context(filters=filters))
    assert long_decision.stop_price == Decimal("99.0")
    assert short_decision.stop_price == Decimal("101.0")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"confidence": Decimal("0.74")}, "confidence_below_minimum"),
        ({"invalidation_price": Decimal("99.5")}, "stop_too_close"),
        ({"invalidation_price": Decimal("97")}, "stop_too_far"),
        ({"target_price": Decimal("102")}, "net_reward_risk_below_minimum"),
    ],
)
def test_rejects_signal_boundary_violations(overrides: dict[str, object], reason: str) -> None:
    decision = RiskEngine().evaluate(signal(**overrides), snapshot(), context())
    assert decision.status == DecisionStatus.REJECTED
    assert reason in decision.reasons


def test_rejects_position_direction_correlation_and_mode_limits() -> None:
    existing = position(symbol="ETHUSDT", side=PositionSide.LONG)
    cases = [
        (context(mode=SystemMode.PAUSED), "system_mode_disallows_entries"),
        (
            context(positions=[existing], correlations={"ETHUSDT": Decimal("0.81")}),
            "correlated_with_ETHUSDT",
        ),
        (
            context(positions=[existing, position(position_id="p2", symbol="SOLUSDT")]),
            "same_direction_limit_reached",
        ),
        (
            context(
                positions=[
                    existing,
                    position(position_id="p2", symbol="SOLUSDT", side=PositionSide.SHORT),
                    position(position_id="p3", symbol="BNBUSDT", side=PositionSide.SHORT),
                ]
            ),
            "position_count_limit_reached",
        ),
    ]
    for risk_context, reason in cases:
        decision = RiskEngine().evaluate(signal(), snapshot(), risk_context)
        assert reason in decision.reasons


def test_rejects_open_direction_outside_configured_policy() -> None:
    limits = context().limits.model_copy(update={"entry_direction": "long_only"})
    decision = RiskEngine().evaluate(
        signal(
            action="OPEN_SHORT",
            invalidation_price=Decimal("101"),
            target_price=Decimal("97"),
        ),
        snapshot(),
        context(limits=limits),
    )
    assert decision.status == DecisionStatus.REJECTED
    assert "entry_direction_not_allowed" in decision.reasons


def test_margin_capacity_and_exchange_minimum_are_enforced() -> None:
    account = AccountState(
        equity=Decimal("1000"),
        available_balance=Decimal("800"),
        day_start_equity=Decimal("1000"),
        high_water_mark=Decimal("1000"),
        total_margin_used=Decimal("200"),
    )
    decision = RiskEngine().evaluate(signal(), snapshot(), context(account=account))
    assert decision.status == DecisionStatus.REJECTED
    assert "quantity_below_exchange_minimum" in decision.reasons


def test_daily_loss_and_drawdown_trigger_circuit_breakers() -> None:
    account = AccountState(
        equity=Decimal("940"),
        available_balance=Decimal("900"),
        day_start_equity=Decimal("950"),
        high_water_mark=Decimal("1000"),
    )
    reasons = RiskEngine().check_circuit_breakers(context(account=account))
    assert reasons == ["daily_loss_limit_reached", "max_drawdown_reached"]
