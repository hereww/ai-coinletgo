from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.api.schemas import (
    ConfigUpdateRequest,
    IntegrationProbeRequest,
    ManualEntryAdviceRequest,
    ManualEntryRequest,
    ModelProfileSelectRequest,
    ModelRelayUpdateRequest,
    PasswordActionRequest,
    ReducePositionRequest,
    ReplayRequest,
)
from trading_system.config import Settings
from trading_system.domain.enums import PositionSide, ReviewAction
from trading_system.domain.models import ManualEntryAdvice, PositionReview, RiskLimits


def test_replay_request_accepts_inclusive_year_and_rejects_invalid_ranges() -> None:
    request = ReplayRequest(
        symbols=["BTCUSDT"],
        start_date="2025-01-01",
        end_date="2025-12-31",
    )
    assert request.end_date == "2025-12-31"

    with pytest.raises(ValidationError, match="end_date cannot precede start_date"):
        ReplayRequest(
            symbols=["BTCUSDT"],
            start_date="2025-02-01",
            end_date="2025-01-01",
        )
    with pytest.raises(ValidationError):
        ReplayRequest(
            symbols=["BTCUSDT"],
            start_date="2025-01-01T00:00:00",
            end_date="2025-02-01",
        )
    recorded = ReplayRequest(
        mode="recorded_portfolio",
        portfolio_decision_id="0d5f81b1-4a64-4ac9-8d72-e1c4a2f70162",
    )
    assert recorded.mode == "recorded_portfolio"
    with pytest.raises(ValidationError, match="portfolio_decision_id"):
        ReplayRequest(mode="recorded_portfolio")
    with pytest.raises(ValidationError, match="no longer supported"):
        ReplayRequest(
            symbols=["BTCUSDT"],
            start_date="2025-01-01",
            end_date="2025-02-01",
            include_ai_sample=True,
        )


def test_model_relay_config_accepts_http_urls_and_enforces_budget_ceiling() -> None:
    request = ModelRelayUpdateRequest(
        base_url="https://relay.example.com/",
        model_name="gpt-5.6",
        reasoning_effort="medium",
        daily_request_limit=110,
    )
    assert request.base_url == "https://relay.example.com"

    with pytest.raises(ValidationError, match="http:// or https://"):
        ModelRelayUpdateRequest(base_url="relay.example.com", model_name="gpt-5.6")
    with pytest.raises(ValidationError):
        ModelRelayUpdateRequest(
            base_url="https://relay.example.com",
            model_name="gpt-5.6",
            daily_request_limit=111,
        )

    assert ModelProfileSelectRequest(profile_id="vllm").profile_id == "vllm"
    with pytest.raises(ValidationError):
        ModelProfileSelectRequest(profile_id="unknown")


def test_position_review_allows_only_bounded_partial_close_fractions() -> None:
    review = PositionReview(
        position_id="binance-BTCUSDT-LONG",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        action=ReviewAction.PARTIAL_CLOSE,
        confidence="0.9",
        close_fraction="0.25",
        rationale="momentum is extended",
    )
    assert review.close_fraction == Decimal("0.25")

    with pytest.raises(ValidationError, match="0.25 or 0.5"):
        PositionReview(
            position_id="binance-BTCUSDT-LONG",
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            action=ReviewAction.PARTIAL_CLOSE,
            confidence="0.9",
            close_fraction="0.3",
            rationale="invalid fraction",
        )


def test_opening_strategy_config_is_bounded_and_validates_stop_range() -> None:
    request = ConfigUpdateRequest(
        entry_direction="long_only",
        entry_trigger="pullback_only",
        candidate_count=3,
        min_confidence="0.8",
        min_net_reward_risk="2.5",
        min_stop_atr="1.0",
        max_stop_atr="2.0",
    )
    assert request.entry_direction == "long_only"
    assert request.entry_trigger == "pullback_only"

    with pytest.raises(ValidationError, match="min_stop_atr cannot exceed max_stop_atr"):
        ConfigUpdateRequest(min_stop_atr="2.2", max_stop_atr="1.2")

    symbols = ConfigUpdateRequest(entry_symbols=["btcusdt", "ETHUSDT", "BTCUSDT"]).entry_symbols
    assert symbols == ["BTCUSDT", "ETHUSDT"]
    with pytest.raises(ValidationError, match="valid Binance symbols"):
        ConfigUpdateRequest(entry_symbols=["BTC-USDT"])


def test_risk_config_accepts_custom_ranges_and_domain_limits_validate_stop_order() -> None:
    request = ConfigUpdateRequest(
        capital_limit_usdt="250000",
        single_trade_risk_pct="0.2",
        portfolio_risk_pct="0.8",
        daily_loss_pct="0.25",
        max_drawdown_pct="0.6",
        max_margin_pct="0.9",
        max_positions=24,
        max_same_direction=12,
        correlation_limit="0.15",
        min_confidence="0.1",
        min_net_reward_risk="0.25",
        min_stop_atr="0.2",
        max_stop_atr="4.5",
        candidate_count=25,
    )
    assert request.max_positions == 24
    assert request.max_same_direction == 12
    assert request.min_stop_atr == Decimal("0.2")
    assert ConfigUpdateRequest(min_stop_atr="0.2", max_stop_atr="4.5")

    with pytest.raises(ValidationError, match="min_stop_atr cannot exceed max_stop_atr"):
        RiskLimits(min_stop_atr="4.5", max_stop_atr="0.2")

    with pytest.raises(ValidationError):
        RiskLimits(single_trade_risk_pct="0")


def test_settings_accept_custom_candidate_count_and_validate_stop_order() -> None:
    settings = Settings(candidate_count=25, min_stop_atr=0.2, max_stop_atr=4.5)
    assert settings.candidate_count == 25
    assert settings.min_stop_atr == 0.2

    with pytest.raises(ValueError, match="min_stop_atr cannot exceed max_stop_atr"):
        Settings(min_stop_atr=4.5, max_stop_atr=0.2)


def test_manual_advice_uses_the_same_target_ordering_as_manual_entry() -> None:
    request = ManualEntryAdviceRequest(
        symbol="ethusdt",
        side="LONG",
        leverage=3,
        stop_distance_pct="1.2",
        tp1_r="1",
        tp2_r="2.5",
        messages=[{"role": "user", "content": "给出ETH开仓建议"}],
    )
    assert request.symbol == "ETHUSDT"

    entry = ManualEntryRequest(
        operation_id="manual-entry-123",
        symbol="BTCUSDT",
        side="LONG",
        leverage=2,
        stop_distance_pct="1",
        tp1_r="1",
        tp2_r="2",
        password="operator-password",
    )
    assert entry.password == "operator-password"

    with pytest.raises(ValidationError, match="tp2_r cannot be below tp1_r"):
        ManualEntryAdvice(
            reply="参数不完整",
            action="OPEN_LONG",
            confidence="0.8",
            stop_distance_pct="1",
            tp1_r="3",
            tp2_r="2",
            leverage=3,
        )


@pytest.mark.parametrize(
    ("request_model", "payload"),
    [
        (PasswordActionRequest, {"password": "operator-password", "unexpected": "old"}),
        (
            ConfigUpdateRequest,
            {"max_leverage": 3, "password": "operator-password", "unexpected": "old"},
        ),
        (
            ReducePositionRequest,
            {
                "position_id": "binance-BTCUSDT-LONG",
                "fraction": "0.5",
                "operation_id": "reduce-123",
                "password": "operator-password",
                "unexpected": "old",
            },
        ),
        (
            ManualEntryRequest,
            {
                "operation_id": "manual-entry-123",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "leverage": 2,
                "stop_distance_pct": "1",
                "tp1_r": "1",
                "tp2_r": "2",
                "password": "operator-password",
                "unexpected": "old",
            },
        ),
    ],
)
def test_sensitive_requests_reject_unknown_fields(
    request_model: type[object], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        request_model(**payload)  # type: ignore[call-arg]


def test_integration_probe_request_rejects_unknown_fields() -> None:
    request = IntegrationProbeRequest(target="testnet")
    assert request.target == "testnet"
    with pytest.raises(ValidationError):
        IntegrationProbeRequest(target="testnet", unexpected="old")
