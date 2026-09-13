from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote, urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

RUNTIME_CONFIG_FIELDS = frozenset(
    {
        "capital_limit_usdt",
        "single_trade_risk_pct",
        "portfolio_risk_pct",
        "daily_loss_pct",
        "max_drawdown_pct",
        "max_leverage",
        "max_margin_pct",
        "max_positions",
        "max_same_direction",
        "correlation_limit",
        "model_base_url",
        "model_name",
        "model_profile",
        "vllm_model_base_url",
        "vllm_model_name",
        "vllm_model_label",
        "model_reasoning_effort",
        "model_timeout_seconds",
        "strategy_profile",
        "entry_direction",
        "entry_trigger",
        "candidate_count",
        "min_confidence",
        "min_net_reward_risk",
        "min_stop_atr",
        "max_stop_atr",
        "manual_exit_levels_enabled",
        "manual_stop_atr",
        "manual_take_profit_atr",
        "model_primary_portfolio_enabled",
        "strong_trend_entry_override_enabled",
        "strong_trend_adx_min",
        "trend_adx_min",
        "volatility_soft_limit_percentile",
        "volatility_hard_limit_percentile",
        "elevated_volatility_risk_multiplier",
        "high_volatility_risk_multiplier",
        "entry_symbols",
        "scan_interval_minutes",
        "model_strategy_enabled",
        "portfolio_strategy_enabled",
        "portfolio_rebalance_deadband_fraction",
        "portfolio_rebalance_cooldown_minutes",
        "factor_policy_enabled",
        "factor_rank_weight",
        "factor_min_risk_multiplier",
        "factor_promotion_windows",
        "hft_enabled",
        "hft_dry_run",
        "hft_symbols",
        "hft_event_interval_ms",
        "hft_max_spread_pct",
        "hft_min_depth_usdt",
        "hft_order_notional_usdt",
        "hft_max_inventory_usdt",
        "hft_cooldown_seconds",
        "hft_market_stale_seconds",
        "hft_max_consecutive_losses",
        "hft_imbalance_threshold",
    }
)

HISTORICAL_RESEARCH_DISABLED_MESSAGE = (
    "历史研究已暂停，仅保留模型驱动的 Binance 测试网自动交易"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Futures Risk Console"
    app_timezone: str = "Asia/Shanghai"
    database_url: str = "sqlite+aiosqlite:///./trading.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_dir: Path = Path("/run/secrets")
    runtime_secret_dir: Path = Path("/run/runtime-secrets")
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    auth_required: bool = False
    auth_username: str = Field(
        default="admin",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    cookie_secure: bool = False
    session_ttl_seconds: int = 28_800
    login_attempts_per_15_minutes: int = 5

    binance_environment: Literal["testnet", "live"] = "testnet"
    binance_testnet_base_url: str = "https://testnet.binancefuture.com"
    binance_live_base_url: str = "https://fapi.binance.com"
    binance_historical_data_url: str = "https://data.binance.vision"
    historical_research_enabled: bool = False
    binance_ws_testnet_url: str = "wss://stream.binancefuture.com"
    binance_ws_live_url: str = "wss://fstream.binance.com"
    binance_recv_window_ms: int = 5_000
    # Binance's priceProtect can defer a stop during mark/contract price
    # divergence. Hard stops default to FALSE so the exchange trigger remains
    # the last-resort protection during volatile conditions.
    binance_stop_price_protect: bool = False

    # The proxy URL is intentionally stored in a Docker secret rather than in
    # environment variables or the runtime configuration table.  This keeps
    # credentials out of the database, API responses, and audit records.
    http_proxy_enabled: bool = False
    # Binance and the model relay may need different egress paths.  ``None``
    # preserves the legacy shared-proxy behavior for existing installations;
    # production deployments can explicitly set this to false when a proxy
    # exit is rate-limited by Binance while the model relay still needs it.
    binance_http_proxy_enabled: bool | None = None

    model_base_url: str | None = None
    model_name: str = "gpt-5.6"
    model_profile: Literal["relay", "vllm"] = "relay"
    vllm_model_base_url: str | None = None
    vllm_model_name: str = "Qwen/Qwen3.8-27B-FP8"
    vllm_model_label: str = "自建 vLLM"
    model_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    # The self-hosted Qwen portfolio endpoint may need more than one minute
    # to generate a complete structured decision for the full watchlist.
    # Keep the wait bounded, but do not turn a slow valid decision into a
    # misleading "model interrupted" state.
    model_timeout_seconds: float = 120.0
    model_prompt_version: str = "signal-v1"
    portfolio_prompt_version: str = "portfolio-v1.4-factor-policy"
    strategy_profile: Literal[
        "conservative", "balanced", "trend_following", "scalping"
    ] = "trend_following"
    entry_direction: Literal["both", "long_only", "short_only"] = "both"
    entry_trigger: Literal["breakout_or_pullback", "breakout_only", "pullback_only"] = (
        "breakout_or_pullback"
    )
    entry_symbols: list[str] = Field(default_factory=list, max_length=30)

    capital_limit_usdt: float = Field(default=1_000.0, gt=0)
    # Testnet can use a larger bounded risk budget for faster acceptance
    # testing.  These values are deliberately capped for live mode below.
    single_trade_risk_pct: float = Field(default=0.004, gt=0)
    portfolio_risk_pct: float = Field(default=0.012, gt=0)
    daily_loss_pct: float = Field(default=0.01, gt=0)
    max_drawdown_pct: float = Field(default=0.05, gt=0)
    max_leverage: int = Field(default=30, ge=1, le=30)
    max_margin_pct: float = Field(default=0.20, gt=0)
    max_positions: int = Field(default=4, ge=1)
    max_same_direction: int = Field(default=2, ge=1)
    correlation_limit: float = Field(default=0.80, ge=0, le=1)
    min_stop_atr: float = Field(default=0.80, gt=0)
    max_stop_atr: float = Field(default=4.00, gt=0)
    # When enabled, new portfolio entries use these deterministic ATR-based
    # exits instead of trusting model-proposed absolute prices. Existing
    # positions keep their current stop and are never widened automatically.
    manual_exit_levels_enabled: bool = False
    manual_stop_atr: float = Field(default=1.80, gt=0, le=10)
    manual_take_profit_atr: float = Field(default=5.00, gt=0, le=20)
    # Testnet can delegate opportunity selection to the model.  The risk
    # compiler still owns hard stops, sizing, margin/balance checks, exchange
    # constraints, system mode, and circuit breakers.
    model_primary_portfolio_enabled: bool = False
    # Testnet may bypass ordinary opportunity-policy filters when both higher
    # timeframes show a strong uptrend. Hard stop geometry, portfolio risk,
    # margin/balance, total positions, exchange constraints and circuit
    # breakers remain mandatory.
    strong_trend_entry_override_enabled: bool = False
    strong_trend_adx_min: float = Field(default=30.0, ge=0)
    trend_adx_min: float = Field(default=20.0, ge=0)
    volatility_soft_limit_percentile: float = Field(default=0.75, ge=0, le=1)
    volatility_hard_limit_percentile: float = Field(default=0.90, ge=0, le=1)
    elevated_volatility_risk_multiplier: float = Field(default=0.75, gt=0, le=1)
    high_volatility_risk_multiplier: float = Field(default=0.50, gt=0, le=1)
    min_confidence: float = Field(default=0.75, ge=0, le=1)
    min_net_reward_risk: float = Field(default=2.5, gt=0)
    max_spread_pct: float = 0.0015
    max_abs_funding_rate: float = 0.001
    max_abs_basis_pct: float = 0.01
    min_book_depth_usdt: float = 50_000.0
    universe_size: int = 30
    candidate_count: int = Field(default=3, ge=1)
    # The scheduler uses a small, explicit cadence set so model expiry and
    # operator expectations remain predictable.
    scan_interval_minutes: Literal[5, 15, 30, 60] = 5
    min_listing_days: int = 90
    # Portfolio-v1 and HFT are testnet-only features. HFT defaults to shadow
    # execution: it consumes the live depth stream but never submits orders.
    # When disabled on testnet, the worker uses the deterministic rule-based
    # fallback and does not call the remote model.
    model_strategy_enabled: bool = True
    portfolio_strategy_enabled: bool = False
    portfolio_rebalance_deadband_fraction: float = Field(default=0.25, ge=0, le=1)
    portfolio_rebalance_cooldown_minutes: int = Field(default=120, ge=0, le=1_440)
    factor_policy_enabled: bool = True
    factor_rank_weight: float = Field(default=0.20, ge=0, le=1)
    factor_min_risk_multiplier: float = Field(default=0.75, gt=0, le=1)
    factor_promotion_windows: int = Field(default=30, ge=1, le=365)
    hft_enabled: bool = False
    hft_dry_run: bool = True
    hft_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT"], max_length=10
    )
    hft_event_interval_ms: int = Field(default=100, ge=50, le=5_000)
    hft_max_spread_pct: float = Field(default=0.0008, gt=0, le=0.02)
    hft_min_depth_usdt: float = Field(default=25_000.0, gt=0)
    hft_order_notional_usdt: float = Field(default=50.0, gt=0)
    hft_max_inventory_usdt: float = Field(default=250.0, gt=0)
    hft_cooldown_seconds: int = Field(default=3, ge=0, le=3_600)
    hft_market_stale_seconds: float = Field(default=2.0, gt=0, le=60)
    hft_max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    hft_imbalance_threshold: float = Field(default=0.20, gt=0, lt=1)

    telegram_enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def apply_environment_risk_defaults(cls, value: object) -> object:
        """Use an active testnet profile without weakening live defaults."""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        live = str(data.get("binance_environment", "testnet")).lower() == "live"
        defaults = (
            {
                "single_trade_risk_pct": 0.0025,
                "portfolio_risk_pct": 0.0075,
                "max_leverage": 3,
                "max_positions": 3,
                "candidate_count": 5,
                "min_net_reward_risk": 2.0,
                "max_stop_atr": 2.5,
                "manual_exit_levels_enabled": False,
                "model_primary_portfolio_enabled": False,
                "strong_trend_entry_override_enabled": False,
                "factor_policy_enabled": False,
            }
            if live
            else {
                "single_trade_risk_pct": 0.004,
                "portfolio_risk_pct": 0.012,
                "max_leverage": 30,
                "max_positions": 4,
                "candidate_count": 3,
                "min_net_reward_risk": 2.5,
                "max_stop_atr": 4.0,
                "manual_exit_levels_enabled": True,
                "model_primary_portfolio_enabled": False,
                "strong_trend_entry_override_enabled": False,
                "model_strategy_enabled": True,
                "portfolio_strategy_enabled": False,
                "portfolio_rebalance_deadband_fraction": 0.25,
                "portfolio_rebalance_cooldown_minutes": 120,
                "factor_policy_enabled": True,
            }
        )
        for key, default in defaults.items():
            current = data.get(key)
            if current is None:
                data[key] = default
                continue
            if not live:
                continue
            # Settings merges constructor values and dotenv values before this
            # validator runs.  Clamp a stale testnet-only value here so a live
            # process can never inherit the aggressive testnet profile.
            if key in {"max_leverage", "max_positions", "candidate_count"}:
                data[key] = min(int(current), int(default))
            elif key == "min_net_reward_risk":
                data[key] = max(float(current), float(default))
            elif key == "max_stop_atr":
                data[key] = min(float(current), float(default))
            elif key == "manual_exit_levels_enabled":
                data[key] = False
            elif key == "model_primary_portfolio_enabled":
                data[key] = False
            elif key == "strong_trend_entry_override_enabled":
                data[key] = False
            elif key == "factor_policy_enabled":
                data[key] = False
            elif key in {"single_trade_risk_pct", "portfolio_risk_pct"}:
                data[key] = min(float(current), float(default))
        return data

    @model_validator(mode="after")
    def enforce_live_security(self) -> Settings:
        if self.min_stop_atr > self.max_stop_atr:
            raise ValueError("min_stop_atr cannot exceed max_stop_atr")
        if self.manual_exit_levels_enabled and self.manual_stop_atr > self.max_stop_atr:
            raise ValueError("manual_stop_atr cannot exceed max_stop_atr")
        if self.volatility_soft_limit_percentile > self.volatility_hard_limit_percentile:
            raise ValueError(
                "volatility_soft_limit_percentile cannot exceed volatility_hard_limit_percentile"
            )
        if self.binance_environment == "live":
            if self.app_env != "production":
                raise ValueError("live Binance environment requires APP_ENV=production")
            if not self.auth_required:
                raise ValueError("live Binance environment requires AUTH_REQUIRED=true")
            if not self.cookie_secure:
                raise ValueError("live Binance environment requires COOKIE_SECURE=true")
            if self.max_leverage > 3:
                raise ValueError("live Binance environment caps max_leverage at 3x")
            if self.single_trade_risk_pct > 0.0025:
                raise ValueError("live Binance environment caps single_trade_risk_pct at 0.25%")
            if self.portfolio_risk_pct > 0.0075:
                raise ValueError("live Binance environment caps portfolio_risk_pct at 0.75%")
            if self.candidate_count > 5:
                raise ValueError("live Binance environment caps candidate_count at 5")
            if self.max_positions > 3:
                raise ValueError("live Binance environment caps max_positions at 3")
            if self.manual_exit_levels_enabled:
                raise ValueError("manual exit levels are limited to Binance testnet")
            if self.model_primary_portfolio_enabled:
                raise ValueError("model-primary portfolio mode is limited to Binance testnet")
            if self.strong_trend_entry_override_enabled:
                raise ValueError("strong trend entry override is limited to Binance testnet")
            if not self.model_strategy_enabled:
                raise ValueError("model strategy cannot be disabled for live Binance environment")
            if self.factor_policy_enabled:
                raise ValueError("factor policy execution is limited to Binance testnet")
        if self.app_env == "production":
            allowed_rest_hosts = (
                {"fapi.binance.com"}
                if self.binance_environment == "live"
                else {"testnet.binancefuture.com", "demo-fapi.binance.com"}
            )
            allowed_ws_hosts = (
                {"fstream.binance.com"}
                if self.binance_environment == "live"
                else {"stream.binancefuture.com", "demo-fstream.binance.com"}
            )
            rest = urlparse(self.binance_base_url)
            websocket = urlparse(self.binance_ws_url)
            if rest.scheme != "https" or rest.hostname not in allowed_rest_hosts:
                raise ValueError(
                    "production Binance REST URL is not an approved official endpoint"
                )
            if websocket.scheme != "wss" or websocket.hostname not in allowed_ws_hosts:
                raise ValueError(
                    "production Binance WebSocket URL is not an approved official endpoint"
                )
            historical = urlparse(self.binance_historical_data_url)
            if historical.scheme != "https" or historical.hostname != "data.binance.vision":
                raise ValueError(
                    "production Binance historical data URL is not an approved official endpoint"
                )
        if self.binance_environment == "live":
            if self.portfolio_strategy_enabled:
                raise ValueError("Portfolio-v1 strategy is limited to Binance testnet")
            if self.hft_enabled:
                raise ValueError("HFT strategy is limited to Binance testnet")
        return self

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("entry_symbols", mode="before")
    @classmethod
    def normalize_entry_symbols(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            symbol = str(item).strip().upper()
            valid = symbol.isascii() and symbol.isalnum() and 5 <= len(symbol) <= 20
            if symbol and not valid:
                raise ValueError("entry_symbols must contain valid Binance symbols")
            if symbol and symbol not in normalized:
                normalized.append(symbol)
        return normalized

    @field_validator("hft_symbols", mode="before")
    @classmethod
    def normalize_hft_symbols(cls, value: object) -> object:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return value
        normalized = []
        for item in value:
            symbol = str(item).strip().upper()
            valid = symbol.isascii() and symbol.isalnum() and 5 <= len(symbol) <= 20
            if symbol and not valid:
                raise ValueError("hft_symbols must contain valid Binance symbols")
            if symbol and symbol not in normalized:
                normalized.append(symbol)
        return normalized

    @field_validator("scan_interval_minutes", mode="before")
    @classmethod
    def normalize_scan_interval(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    def read_secret(self, name: str) -> str | None:
        # Runtime-managed secrets take precedence over immutable deployment
        # secrets. This lets the authenticated settings API rotate model keys
        # without putting them in the database or browser payloads.
        for directory in (self.runtime_secret_dir, self.secret_dir):
            path = directory / name
            try:
                value = path.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                continue
            if value:
                return value
        return None

    def write_runtime_secret(self, name: str, value: str) -> None:
        if name not in {"model_api_key", "vllm_model_api_key"}:
            raise ValueError("unsupported runtime secret")
        normalized = value.strip()
        if not normalized:
            raise ValueError("API key cannot be empty")
        directory = self.runtime_secret_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        temporary = directory / f".{name}.tmp"
        temporary.write_text(f"{normalized}\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    @property
    def database_connection_url(self) -> str:
        password = self.read_secret("postgres_password")
        if not password or not self.database_url.startswith("postgresql"):
            return self.database_url
        scheme, separator, remainder = self.database_url.partition("://")
        if not separator or "@" not in remainder:
            return self.database_url
        credentials, location = remainder.rsplit("@", 1)
        if ":" in credentials:
            return self.database_url
        return f"{scheme}://{credentials}:{quote(password, safe='')}@{location}"

    @property
    def session_secret(self) -> str:
        return self.read_secret("session_secret") or "development-only-session-secret"

    @property
    def auth_password_hash(self) -> str | None:
        return self.read_secret("auth_password_hash")

    @property
    def model_api_key(self) -> str | None:
        return self.read_secret("model_api_key")

    @property
    def vllm_model_api_key(self) -> str | None:
        return self.read_secret("vllm_model_api_key")

    @property
    def active_model_base_url(self) -> str | None:
        if self.model_profile == "vllm":
            return self.vllm_model_base_url.rstrip("/") if self.vllm_model_base_url else None
        return self.model_base_url.rstrip("/") if self.model_base_url else None

    @property
    def active_model_name(self) -> str:
        return self.vllm_model_name if self.model_profile == "vllm" else self.model_name

    @property
    def active_model_api_key(self) -> str | None:
        return self.vllm_model_api_key if self.model_profile == "vllm" else self.model_api_key

    @property
    def active_model_label(self) -> str:
        return self.vllm_model_label if self.model_profile == "vllm" else "OpenAI 中转"

    @property
    def active_model_transport_allowed(self) -> bool:
        if self.binance_environment != "live":
            return True
        return urlparse(self.active_model_base_url or "").scheme == "https"

    @property
    def model_profiles(self) -> list[dict[str, object]]:
        relay_configured = bool(self.model_base_url and self.model_api_key)
        vllm_configured = bool(self.vllm_model_base_url and self.vllm_model_api_key)
        return [
            {
                "id": "relay",
                "label": "OpenAI 中转",
                "kind": "relay",
                "base_url": self.model_base_url,
                "model_name": self.model_name,
                "api_key_configured": bool(self.model_api_key),
                "configured": relay_configured,
                "active": self.model_profile == "relay",
            },
            {
                "id": "vllm",
                "label": self.vllm_model_label,
                "kind": "self_hosted",
                "base_url": self.vllm_model_base_url,
                "model_name": self.vllm_model_name,
                "api_key_configured": bool(self.vllm_model_api_key),
                "configured": vllm_configured,
                "active": self.model_profile == "vllm",
            },
        ]

    @property
    def http_proxy_secret(self) -> str | None:
        return self.read_secret("http_proxy_url")

    @property
    def http_proxy_url(self) -> str | None:
        if not self.http_proxy_enabled:
            return None
        value = self.http_proxy_secret
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return value.rstrip("/")

    @property
    def binance_http_proxy_url(self) -> str | None:
        if not self.binance_proxy_enabled:
            return None
        value = self.http_proxy_secret
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return value.rstrip("/")

    @property
    def binance_proxy_enabled(self) -> bool:
        return (
            self.http_proxy_enabled
            if self.binance_http_proxy_enabled is None
            else self.binance_http_proxy_enabled
        )

    @property
    def binance_http_proxy_configured(self) -> bool:
        return self.binance_proxy_enabled and self.binance_http_proxy_url is not None

    @property
    def binance_http_proxy_detail(self) -> str:
        if self.binance_http_proxy_enabled is None:
            return self.http_proxy_detail
        if not self.binance_proxy_enabled:
            return "Binance HTTP 代理未启用"
        if not self.http_proxy_secret:
            return "Binance HTTP 代理已启用但未挂载 http_proxy_url secret"
        if self.binance_http_proxy_url is None:
            return "Binance HTTP 代理地址无效，仅支持 http:// 或 https://"
        return "Binance HTTP 代理已配置"

    @property
    def http_proxy_configured(self) -> bool:
        return self.http_proxy_enabled and self.http_proxy_url is not None

    @property
    def http_proxy_detail(self) -> str:
        if not self.http_proxy_enabled:
            return "HTTP 代理未启用"
        if not self.http_proxy_secret:
            return "HTTP 代理已启用但未挂载 http_proxy_url secret"
        if self.http_proxy_url is None:
            return "HTTP 代理地址无效，仅支持 http:// 或 https://"
        return "HTTP 代理已启用，Binance 与 AI 中转共用"

    @property
    def binance_api_key(self) -> str | None:
        suffix = "live" if self.binance_environment == "live" else "testnet"
        return self.read_secret(f"binance_{suffix}_api_key")

    @property
    def binance_api_secret(self) -> str | None:
        suffix = "live" if self.binance_environment == "live" else "testnet"
        return self.read_secret(f"binance_{suffix}_api_secret")

    @property
    def binance_base_url(self) -> str:
        if self.binance_environment == "live":
            return self.binance_live_base_url.rstrip("/")
        return self.binance_testnet_base_url.rstrip("/")

    @property
    def binance_ws_url(self) -> str:
        if self.binance_environment == "live":
            return self.binance_ws_live_url.rstrip("/")
        return self.binance_ws_testnet_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
