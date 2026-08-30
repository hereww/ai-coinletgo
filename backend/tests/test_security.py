from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException

from trading_system.api.security import SecurityService
from trading_system.config import Settings


def secured_settings(tmp_path: object) -> Settings:
    tmp_path.joinpath("auth_password_hash").write_text(
        PasswordHasher().hash("correct-password"), encoding="utf-8"
    )
    tmp_path.joinpath("session_secret").write_text("s" * 48, encoding="utf-8")
    return Settings(auth_required=True, secret_dir=tmp_path)


def test_login_session_and_csrf(tmp_path: object) -> None:
    settings = secured_settings(tmp_path)
    service = SecurityService(settings)
    token, csrf = service.login("admin", "correct-password", "127.0.0.1")
    user = service.authenticate(token)
    assert user.username == "admin"
    service.verify_csrf(user, csrf, csrf)
    with pytest.raises(HTTPException, match="CSRF token mismatch"):
        service.verify_csrf(user, "wrong", csrf)


def test_login_rate_limit(tmp_path: object) -> None:
    settings = secured_settings(tmp_path)
    settings.login_attempts_per_15_minutes = 2
    service = SecurityService(settings)
    for _ in range(2):
        with pytest.raises(HTTPException, match="Invalid credentials"):
            service.login("admin", "wrong", "10.0.0.1")
    with pytest.raises(HTTPException) as caught:
        service.login("admin", "wrong", "10.0.0.1")
    assert caught.value.status_code == 429


@pytest.mark.parametrize(
    ("username", "password"),
    (("not-admin", "correct-password"), ("admin", "wrong-password")),
)
def test_login_rejects_invalid_username_or_password(
    tmp_path: object, username: str, password: str
) -> None:
    settings = secured_settings(tmp_path)
    service = SecurityService(settings)
    with pytest.raises(HTTPException, match="Invalid credentials") as caught:
        service.login(username, password, "127.0.0.1")
    assert caught.value.status_code == 401


def test_sensitive_actions_require_the_configured_password(tmp_path: object) -> None:
    settings = secured_settings(tmp_path)
    service = SecurityService(settings)
    assert service.verify_password("") is False
    assert service.verify_password("wrong-password") is False
    assert service.verify_password("correct-password") is True


def test_live_environment_rejects_insecure_settings(tmp_path: object) -> None:
    with pytest.raises(ValueError, match="production"):
        Settings(binance_environment="live", app_env="development", secret_dir=tmp_path)
    with pytest.raises(ValueError, match="AUTH_REQUIRED"):
        Settings(
            binance_environment="live",
            app_env="production",
            auth_required=False,
            secret_dir=tmp_path,
        )
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        Settings(
            binance_environment="live",
            app_env="production",
            auth_required=True,
            cookie_secure=False,
            secret_dir=tmp_path,
        )


def test_live_password_is_fail_closed_even_if_auth_flag_is_mutated(tmp_path: object) -> None:
    settings = Settings(
        secret_dir=tmp_path,
        app_env="production",
        auth_required=True,
        cookie_secure=True,
        binance_environment="live",
    )
    service = SecurityService(settings)
    settings.auth_required = False
    assert service.verify_password("") is False


def test_production_rejects_non_official_binance_endpoints(tmp_path: object) -> None:
    with pytest.raises(ValueError, match="official endpoint"):
        Settings(
            secret_dir=tmp_path,
            app_env="production",
            auth_required=True,
            cookie_secure=True,
            binance_environment="testnet",
            binance_testnet_base_url="https://example.invalid",
        )

    settings = Settings(
        secret_dir=tmp_path,
        app_env="production",
        auth_required=True,
        cookie_secure=True,
        binance_environment="testnet",
    )
    assert settings.binance_base_url == "https://testnet.binancefuture.com"


def test_production_requires_argon2id_password_hash(tmp_path: object) -> None:
    settings = secured_settings(tmp_path)
    settings.app_env = "production"
    settings.cookie_secure = True
    assert SecurityService(settings).production_configured() is True
    tmp_path.joinpath("auth_password_hash").write_text(
        "$argon2i$v=19$m=65536,t=3,p=4$bad$bad", encoding="utf-8"
    )
    assert SecurityService(settings).production_configured() is False


def test_production_rejects_weak_session_and_missing_password_hash(tmp_path: object) -> None:
    settings = secured_settings(tmp_path)
    settings.app_env = "production"
    settings.cookie_secure = True
    tmp_path.joinpath("session_secret").write_text("too-short", encoding="utf-8")
    assert SecurityService(settings).production_configured() is False
    tmp_path.joinpath("session_secret").write_text("s" * 48, encoding="utf-8")
    tmp_path.joinpath("auth_password_hash").write_text("not-a-password-hash", encoding="utf-8")
    assert SecurityService(settings).production_configured() is False


def test_database_password_is_loaded_from_secret_and_url_encoded(tmp_path: object) -> None:
    tmp_path.joinpath("postgres_password").write_text("p@ss:/word", encoding="utf-8")
    settings = Settings(
        secret_dir=tmp_path,
        database_url="postgresql+asyncpg://trading@postgres:5432/trading",
    )
    assert settings.database_connection_url == (
        "postgresql+asyncpg://trading:p%40ss%3A%2Fword@postgres:5432/trading"
    )


def test_http_proxy_secret_is_hidden_and_requires_explicit_enablement(tmp_path: object) -> None:
    tmp_path.joinpath("http_proxy_url").write_text(
        "http://proxy-user:proxy-pass@example.com:8080/", encoding="utf-8"
    )
    disabled = Settings(secret_dir=tmp_path)
    assert disabled.http_proxy_url is None
    assert disabled.http_proxy_detail == "HTTP 代理未启用"

    enabled = Settings(secret_dir=tmp_path, http_proxy_enabled=True)
    assert enabled.http_proxy_configured is True
    assert enabled.http_proxy_url == "http://proxy-user:proxy-pass@example.com:8080"
    assert "proxy-pass" not in enabled.http_proxy_detail

    tmp_path.joinpath("http_proxy_url").write_text("socks5://proxy.example:1080", encoding="utf-8")
    invalid = Settings(secret_dir=tmp_path, http_proxy_enabled=True)
    assert invalid.http_proxy_configured is False
    assert "仅支持 http:// 或 https://" in invalid.http_proxy_detail
