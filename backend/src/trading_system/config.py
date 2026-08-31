from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        "model_reasoning_effort",
        "model_timeout_seconds",
        "model_daily_request_limit",
        "strategy_profile",
        "entry_direction",
        "entry_trigger",
        "candidate_count",
        "min_confidence",
        "min_net_reward_risk",
        "min_stop_atr",
        "max_stop_atr",
        "entry_symbols",
        "scan_interval_minutes",
        "portfolio_strategy_enabled",
        "portfolio_rebalance_deadband_fraction",
        "portfolio_rebalance_cooldown_minutes",
    }
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
    model_daily_request_limit: int = 110
    model_prompt_version: str = "signal-v1"
    portfolio_prompt_version: str = "portfolio-v1.3"
    strategy_profile: Literal[
        "conservative", "balanced", "trend_following", "scalping"
    ] = "trend_following"
    entry_direction: Literal["both", "long_only", "short_only"] = "both"
    entry_trigger: Literal["breakout_or_pullback", "breakout_only", "pullback_only"] = (
        "breakout_or_pullback"
    )
    entry_symbols: list[str] = Field(default_factory=list, max_length=30)

    capital_limit_usdt: float = Field(default=1_000.0, gt=0)
    single_trade_risk_pct: float = Field(default=0.0025, gt=0)
    portfolio_risk_pct: float = Field(default=0.0075, gt=0)
    daily_loss_pct: float = Field(default=0.01, gt=0)
    max_drawdown_pct: float = Field(default=0.05, gt=0)
    max_leverage: int = Field(default=3, ge=1, le=30)
    max_margin_pct: float = Field(default=0.20, gt=0)
    max_positions: int = Field(default=3, ge=1)
    max_same_direction: int = Field(default=2, ge=1)
    correlation_limit: float = Field(default=0.80, ge=0, le=1)
    min_stop_atr: float = Field(default=0.80, gt=0)
    max_stop_atr: float = Field(default=2.50, gt=0)
    min_confidence: float = Field(default=0.75, ge=0, le=1)
    min_net_reward_risk: float = Field(default=2.0, gt=0)
    max_spread_pct: float = 0.0015
    max_abs_funding_rate: float = 0.001
    max_abs_basis_pct: float = 0.01
    min_book_depth_usdt: float = 50_000.0
    universe_size: int = 30
    candidate_count: int = Field(default=5, ge=1)
    scan_interval_minutes: int = Field(default=15, ge=15, le=120)
    min_listing_days: int = 90
    # Portfolio-v1 is opt-in and may only execute on testnet.  Keeping it
    # disabled by default preserves the established signal-v1 behavior until
    # the operator has completed the testnet acceptance checklist.
    portfolio_strategy_enabled: bool = False
    portfolio_rebalance_deadband_fraction: float = Field(default=0.10, ge=0, le=1)
    portfolio_rebalance_cooldown_minutes: int = Field(default=30, ge=0, le=1_440)

    telegram_enabled: bool = False

    @model_validator(mode="after")
    def enforce_live_security(self) -> Settings:
        if self.min_stop_atr > self.max_stop_atr:
            raise ValueError("min_stop_atr cannot exceed max_stop_atr")
        if self.binance_environment == "live":
            if self.app_env != "production":
                raise ValueError("live Binance environment requires APP_ENV=production")
            if not self.auth_required:
                raise ValueError("live Binance environment requires AUTH_REQUIRED=true")
            if not self.cookie_secure:
                raise ValueError("live Binance environment requires COOKIE_SECURE=true")
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
        if self.portfolio_strategy_enabled and self.binance_environment != "testnet":
            raise ValueError("Portfolio-v1 strategy is limited to Binance testnet")
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

    def read_secret(self, name: str) -> str | None:
        path = self.secret_dir / name
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            return None
        return value or None

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
