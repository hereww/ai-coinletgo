from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_system.api.schemas import (
    ConfigUpdateRequest,
    FactorResearchRequest,
    IntegrationProbeRequest,
    ManualEntryAdviceRequest,
    ManualEntryRequest,
    ModelProfileUpdateRequest,
    ModelProfileSelectRequest,
    ModelRelayUpdateRequest,
    PasswordActionRequest,
    PnlSyncRequest,
    ReducePositionRequest,
    ReplayBacktestConfigRequest,
    ReplayRequest,
)
from trading_system.config import Settings
from trading_system.domain.enums import PositionSide, ReviewAction
from trading_system.domain.models import ManualEntryAdvice, PositionReview, RiskLimits


def test_factor_research_request_normalizes_symbols_and_bounds_work() -> None:
    request = FactorResearchRequest(
        symbols=[" btcusdt ", "ETHUSDT", "btcusdt", "SOLUSDT"],
        start_date="2025-01-01",
        end_date="2025-02-01",
    )
    assert request.symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    with pytest.raises(ValueError, match="at least 3 items"):
        FactorResearchRequest(
            symbols=["BTCUSDT", "ETHUSDT"],
            start_date="2025-01-01",
            end_date="2025-02-01",
        )
    with pytest.raises(ValueError, match="cannot precede"):
        FactorResearchRequest(
            symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            start_date="2025-02-01",
            end_date="2025-01-01",
        )


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

    configured = ReplayRequest(
        symbols=["BTCUSDT"],
        start_date="2025-01-01",
        end_date="2025-01-02",
        factor_research_run_id="0d5f81b1-4a64-4ac9-8d72-e1c4a2f70162",
        backtest_config={
            "max_leverage": 30,
            "trend_adx_min": "18",
            "volatility_soft_limit_percentile": "0.7",
            "volatility_hard_limit_percentile": "0.9",
        },
    )
    assert configured.backtest_config is not None
    assert configured.backtest_config.max_leverage == 30
    assert configured.factor_research_run_id is not None
    with pytest.raises(ValidationError, match="only supported for deterministic"):
        ReplayRequest(
            mode="recorded_portfolio",
            portfolio_decision_id="0d5f81b1-4a64-4ac9-8d72-e1c4a2f70162",
            factor_research_run_id="1d5f81b1-4a64-4ac9-8d72-e1c4a2f70162",
        )
    with pytest.raises(ValidationError, match="cannot exceed max_stop_atr"):
        ReplayBacktestConfigRequest(stop_atr="3", max_stop_atr="2")


def test_model_relay_config_accepts_http_urls_without_a_daily_request_ceiling() -> None:
    request = ModelRelayUpdateRequest(
        base_url="https://relay.example.com/",
        model_name="gpt-5.6",
        reasoning_effort="medium",
    )
    assert request.base_url == "https://relay.example.com"

    with pytest.raises(ValidationError, match="http:// or https://"):
        ModelRelayUpdateRequest(base_url="relay.example.com", model_name="gpt-5.6")
    assert ModelProfileSelectRequest(profile_id="vllm").profile_id == "vllm"
    with pytest.raises(ValidationError):
        ModelProfileSelectRequest(profile_id="unknown")


def test_model_profile_update_validates_vllm_fields_and_normalizes_secrets() -> None:
    request = ModelProfileUpdateRequest(
        profile_id="vllm",
        base_url="http://vllm.example:8000/v1/",
        model_name="Qwen/Qwen3.8-27B-FP8",
        api_key="  vllm-secret  ",
        reasoning_effort="high",
        timeout_seconds=120,
        strategy_profile="balanced",
    )
    assert request.base_url == "http://vllm.example:8000/v1"
    assert request.api_key == "vllm-secret"
    assert request.reasoning_effort == "high"

    with pytest.raises(ValidationError):
        ModelProfileUpdateRequest(
            profile_id="vllm",
            base_url="vllm.example:8000/v1",
            model_name="Qwen/Qwen3.8-27B-FP8",
        )
    with pytest.raises(ValidationError):
        ModelProfileUpdateRequest(
            profile_id="vllm",
            base_url="http://vllm.example/v1",
            model_name="Qwen/Qwen3.8-27B-FP8",
            historical_research_enabled=True,
        )


def test_pnl_sync_request_requires_an_inclusive_date_range_of_at_most_one_year() -> None:
    request = PnlSyncRequest(start_date="2026-09-04", end_date="2026-09-05")
    assert request.start_date.isoformat() == "2026-09-04"

    with pytest.raises(ValidationError, match="end_date cannot precede start_date"):
        PnlSyncRequest(start_date="2026-09-05", end_date="2026-09-04")
    with pytest.raises(ValidationError, match="pnl range cannot exceed 366 days"):
        PnlSyncRequest(start_date="2025-01-01", end_date="2026-01-02")
    with pytest.raises(ValidationError):
        PnlSyncRequest(start_date="2026-09-04", end_date="2026-09-05", unexpected=True)


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
        manual_exit_levels_enabled=True,
        manual_stop_atr="1.5",
        manual_take_profit_atr="5.0",
    )
    assert request.entry_direction == "long_only"
    assert request.entry_trigger == "pullback_only"
    assert request.manual_exit_levels_enabled is True
    assert request.manual_stop_atr == Decimal("1.5")

    disabled_model = ConfigUpdateRequest(model_strategy_enabled=False)
    assert disabled_model.model_strategy_enabled is False

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
    assert settings.min_net_reward_risk == 2.5

    with pytest.raises(ValueError, match="min_stop_atr cannot exceed max_stop_atr"):
        Settings(min_stop_atr=4.5, max_stop_atr=0.2)


def test_runtime_model_secret_overrides_deployment_secret_without_echoing_it(tmp_path: object) -> None:
    deployment = tmp_path / "deployment"
    runtime = tmp_path / "runtime"
    deployment.mkdir()
    (deployment / "vllm_model_api_key").write_text("deployment-key\n", encoding="utf-8")
    settings = Settings(secret_dir=deployment, runtime_secret_dir=runtime)

    assert settings.vllm_model_api_key == "deployment-key"
    settings.write_runtime_secret("vllm_model_api_key", "runtime-key")
    assert settings.vllm_model_api_key == "runtime-key"
    assert (runtime / "vllm_model_api_key").read_text(encoding="utf-8") == "runtime-key\n"
    assert (runtime / "vllm_model_api_key").stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError):
        settings.write_runtime_secret("historical_research_enabled", "true")


def test_risk_limits_default_to_the_tightened_cost_aware_policy() -> None:
    limits = RiskLimits()

    assert limits.min_net_reward_risk == Decimal("2.5")
    assert limits.portfolio_rebalance_deadband_fraction == Decimal("0.25")


def test_live_settings_keep_a_two_r_minimum_reward_risk_floor(tmp_path: object) -> None:
    settings = Settings(
        binance_environment="live",
        app_env="production",
        auth_required=True,
        cookie_secure=True,
        secret_dir=tmp_path,
        max_leverage=3,
        single_trade_risk_pct=0.0025,
        portfolio_risk_pct=0.0075,
        candidate_count=5,
        max_positions=3,
        min_net_reward_risk=1.5,
        portfolio_strategy_enabled=False,
    )
    assert settings.min_net_reward_risk == 2.0


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
