from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated, cast

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from trading_system.config import Settings


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    csrf_token: str


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int = 900) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        attempts = self.attempts[key]
        while attempts and attempts[0] < now - self.window_seconds:
            attempts.popleft()
        if len(attempts) >= self.max_attempts:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")
        attempts.append(now)

    def reset(self, key: str) -> None:
        self.attempts.pop(key, None)


class SecurityService:
    cookie_name = "frc_session"
    csrf_cookie_name = "frc_csrf"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.password_hasher = PasswordHasher()
        self.serializer = URLSafeTimedSerializer(settings.session_secret, salt="frc-session-v1")
        self.rate_limiter = LoginRateLimiter(settings.login_attempts_per_15_minutes)

    def login(self, username: str, password: str, client_ip: str) -> tuple[str, str]:
        self.rate_limiter.check(client_ip)
        if self.authentication_required:
            password_hash = self.settings.auth_password_hash
            username_valid = secrets.compare_digest(username, self.settings.auth_username)
            password_valid = bool(
                password_hash and self._verify_password(password_hash, password)
            )
            if not username_valid or not password_valid:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
        csrf_token = secrets.token_urlsafe(32)
        token = self.serializer.dumps(
            {"username": self.settings.auth_username, "csrf": csrf_token}
        )
        self.rate_limiter.reset(client_ip)
        return token, csrf_token

    def set_auth_cookies(self, response: Response, session_token: str, csrf_token: str) -> None:
        response.set_cookie(
            self.cookie_name,
            session_token,
            max_age=self.settings.session_ttl_seconds,
            httponly=True,
            secure=self.settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        response.set_cookie(
            self.csrf_cookie_name,
            csrf_token,
            max_age=self.settings.session_ttl_seconds,
            httponly=False,
            secure=self.settings.cookie_secure,
            samesite="strict",
            path="/",
        )

    def clear_auth_cookies(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/")
        response.delete_cookie(self.csrf_cookie_name, path="/")

    def authenticate(self, token: str | None) -> AuthenticatedUser:
        if not self.authentication_required and not token:
            return AuthenticatedUser(username=self.settings.auth_username, csrf_token="development")
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
        try:
            payload = self.serializer.loads(token, max_age=self.settings.session_ttl_seconds)
        except SignatureExpired as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired") from error
        except BadSignature as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session") from error
        return AuthenticatedUser(username=payload["username"], csrf_token=payload["csrf"])

    def verify_csrf(
        self,
        user: AuthenticatedUser,
        header_token: str | None,
        cookie_token: str | None,
    ) -> None:
        if not self.authentication_required:
            return
        if self.settings.binance_environment == "live" and not self.production_configured():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Live authentication is not configured"
            )
        if not header_token or not cookie_token:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token required")
        if not secrets.compare_digest(header_token, cookie_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token mismatch")
        if not secrets.compare_digest(header_token, user.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token invalid")

    def verify_totp(self, code: str) -> bool:
        if not self.authentication_required:
            return True
        secret = self.settings.auth_totp_secret
        if not secret or not code:
            return False
        return bool(pyotp.TOTP(secret).verify(code, valid_window=1))

    def production_configured(self) -> bool:
        if not self.authentication_required:
            return self.settings.app_env != "production"
        password_hash = self.settings.auth_password_hash
        totp_secret = self.settings.auth_totp_secret
        session_secret = self.settings.read_secret("session_secret")
        totp_valid = False
        if totp_secret:
            try:
                pyotp.TOTP(totp_secret).at(0)
                totp_valid = True
            except (TypeError, ValueError):
                totp_valid = False
        return all(
            (
                password_hash and password_hash.startswith("$argon2id$"),
                totp_valid,
                session_secret and len(session_secret) >= 32,
                self.settings.cookie_secure,
            )
        )

    @property
    def authentication_required(self) -> bool:
        """Live trading never inherits a development authentication bypass."""
        return self.settings.auth_required or self.settings.binance_environment == "live"

    def _verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return self.password_hasher.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False


def security_from_request(request: Request) -> SecurityService:
    return cast(SecurityService, request.app.state.security)


def current_user(
    request: Request,
    session_token: Annotated[str | None, Cookie(alias=SecurityService.cookie_name)] = None,
) -> AuthenticatedUser:
    return security_from_request(request).authenticate(session_token)


def mutating_user(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    csrf_cookie: Annotated[str | None, Cookie(alias=SecurityService.csrf_cookie_name)] = None,
) -> AuthenticatedUser:
    security_from_request(request).verify_csrf(user, csrf_header, csrf_cookie)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(current_user)]
MutatingUser = Annotated[AuthenticatedUser, Depends(mutating_user)]
