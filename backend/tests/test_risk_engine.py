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
