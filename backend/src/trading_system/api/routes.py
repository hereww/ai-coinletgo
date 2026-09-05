from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status

from trading_system.api.controller import SystemController
from trading_system.api.schemas import (
    ConfigUpdateRequest,
    FactorResearchRequest,
    IntegrationProbeRequest,
    LoginRequest,
    ManualEntryAdviceRequest,
    ManualEntryRequest,
    ModelProfileSelectRequest,
    ModelRelayUpdateRequest,
    PasswordActionRequest,
    PnlSyncRequest,
    ReducePositionRequest,
    ReplayRequest,
)
from trading_system.api.security import CurrentUser, MutatingUser, SecurityService
from trading_system.backtest.service import ReplayService
from trading_system.config import Settings
from trading_system.exchange.base import ExchangeError
from trading_system.persistence.repository import Repository
from trading_system.strategy.factor_service import FactorResearchService

router = APIRouter(prefix="/api/v1")


def controller(request: Request) -> SystemController:
    return cast(SystemController, request.app.state.controller)


def repository(request: Request) -> Repository:
    return cast(Repository, request.app.state.repository)


def settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def security_service(request: Request) -> SecurityService:
    return cast(SecurityService, request.app.state.security)


def replay_service(request: Request) -> ReplayService:
    return cast(ReplayService, request.app.state.replay_service)


def factor_research_service(request: Request) -> FactorResearchService:
    return cast(FactorResearchService, request.app.state.factor_research_service)


Controller = Annotated[SystemController, Depends(controller)]
Repo = Annotated[Repository, Depends(repository)]
AppSettings = Annotated[Settings, Depends(settings)]
Security = Annotated[SecurityService, Depends(security_service)]
Replays = Annotated[ReplayService, Depends(replay_service)]
FactorResearch = Annotated[FactorResearchService, Depends(factor_research_service)]


def validate_pnl_date_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "end_date cannot precede start_date",
        )
    if (end_date - start_date).days > 365:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "pnl range cannot exceed 366 days",
        )


def pnl_window_ms(start_date: date, end_date: date, timezone_name: str) -> tuple[int, int]:
    timezone = ZoneInfo(timezone_name)
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone).astimezone(
        UTC
    )
    end_day = end_date + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone).astimezone(UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1


@router.get("/health/live")
async def live_health() -> dict[str, str]:
    return {"status": "alive", "time": datetime.now(UTC).isoformat()}


@router.get("/health/ready")
async def ready_health(service: Controller, _: CurrentUser) -> dict[str, Any]:
    report = await service.health()
    return report.model_dump(mode="json")


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    security: Security,
    repo: Repo,
) -> dict[str, str]:
    client_ip = request.client.host if request.client else "unknown"
    try:
        session_token, csrf_token = security.login(payload.username, payload.password, client_ip)
    except HTTPException:
        await repo.audit(
            actor="anonymous",
            action="login",
            resource="session",
            outcome="denied",
            ip_address=client_ip,
        )
        raise
    security.set_auth_cookies(response, session_token, csrf_token)
    await repo.audit(
        actor=security.settings.auth_username,
        action="login",
        resource="session",
        outcome="success",
        ip_address=client_ip,
    )
    return {"username": security.settings.auth_username, "csrf_token": csrf_token}


@router.post("/auth/logout")
async def logout(
    response: Response, user: MutatingUser, security: Security, repo: Repo
) -> dict[str, bool]:
    security.clear_auth_cookies(response)
    await repo.audit(actor=user.username, action="logout", resource="session", outcome="success")
    return {"ok": True}


@router.get("/auth/me")
async def me(user: CurrentUser, app_settings: AppSettings) -> dict[str, Any]:
    return {
        "username": user.username,
        "auth_required": app_settings.auth_required,
        "environment": app_settings.binance_environment,
    }


@router.get("/dashboard")
async def dashboard(service: Controller, _: CurrentUser) -> dict[str, Any]:
    return await service.dashboard()


@router.get("/positions")
async def positions(service: Controller, _: CurrentUser) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], (await service.dashboard())["positions"])


@router.get("/signals")
async def signals(service: Controller, repo: Repo, _: CurrentUser) -> list[dict[str, Any]]:
    del service
    return await repo.list_signals()


@router.get("/portfolio-decisions")
async def portfolio_decisions(repo: Repo, _: CurrentUser) -> list[dict[str, Any]]:
    return await repo.list_portfolio_decisions()


@router.get("/cycle-status")
async def cycle_status(service: Controller, _: CurrentUser) -> dict[str, Any]:
    return await service.latest_cycle_status()


@router.get("/orders")
async def orders(repo: Repo, _: CurrentUser) -> list[dict[str, Any]]:
    return await repo.list_orders()


@router.get("/pnl/trades")
async def trade_pnl(
    start_date: date, end_date: date, repo: Repo, _: CurrentUser
) -> list[dict[str, Any]]:
    validate_pnl_date_range(start_date, end_date)
    return await repo.list_trade_pnl(start_date, end_date)


@router.get("/pnl/daily")
async def daily_pnl(
    start_date: date, end_date: date, repo: Repo, _: CurrentUser
) -> list[dict[str, Any]]:
    validate_pnl_date_range(start_date, end_date)
    return await repo.list_daily_pnl(start_date, end_date)


@router.get("/pnl/ledger")
async def income_ledger(
    start_date: date, end_date: date, repo: Repo, _: CurrentUser
) -> list[dict[str, Any]]:
    validate_pnl_date_range(start_date, end_date)
    return await repo.list_income_ledger(start_date, end_date)


@router.post("/pnl/sync")
async def sync_income_ledger(
    payload: PnlSyncRequest,
    request: Request,
    user: MutatingUser,
    app_settings: AppSettings,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    start_ms, end_ms = pnl_window_ms(
        payload.start_date, payload.end_date, app_settings.app_timezone
    )
    try:
        rows = await service.exchange.get_income_history(start_ms, end_ms)
    except ExchangeError as error:
        await repo.audit(
            actor=user.username,
            action="sync_income_ledger",
            resource="binance-income",
            outcome="failed",
            detail={
                "start_date": payload.start_date.isoformat(),
                "end_date": payload.end_date.isoformat(),
                "reason": str(error),
            },
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    inserted = await repo.save_income_ledger(rows)
    await repo.audit(
        actor=user.username,
        action="sync_income_ledger",
        resource="binance-income",
        outcome="success",
        detail={
            "start_date": payload.start_date.isoformat(),
            "end_date": payload.end_date.isoformat(),
            "fetched_rows": len(rows),
            "inserted_rows": inserted,
        },
        ip_address=request.client.host if request.client else None,
    )
    return {
        "start_date": payload.start_date.isoformat(),
        "end_date": payload.end_date.isoformat(),
        "fetched_rows": len(rows),
        "inserted_rows": inserted,
    }


@router.get("/market")
async def market(repo: Repo, _: CurrentUser) -> list[dict[str, Any]]:
    return await repo.latest_market()


@router.get("/audit")
async def audit(repo: Repo, _: CurrentUser, limit: int = 100) -> list[dict[str, Any]]:
    return await repo.list_audit(limit=min(max(limit, 1), 250))


@router.get("/config")
async def get_config(app_settings: AppSettings, _: CurrentUser) -> dict[str, Any]:
    return {
        "capital_limit_usdt": app_settings.capital_limit_usdt,
        "single_trade_risk_pct": app_settings.single_trade_risk_pct,
        "portfolio_risk_pct": app_settings.portfolio_risk_pct,
        "daily_loss_pct": app_settings.daily_loss_pct,
        "max_drawdown_pct": app_settings.max_drawdown_pct,
        "max_leverage": app_settings.max_leverage,
        "max_margin_pct": app_settings.max_margin_pct,
        "max_positions": app_settings.max_positions,
        "max_same_direction": app_settings.max_same_direction,
        "correlation_limit": app_settings.correlation_limit,
        "entry_direction": app_settings.entry_direction,
        "entry_trigger": app_settings.entry_trigger,
        "candidate_count": app_settings.candidate_count,
        "scan_interval_minutes": app_settings.scan_interval_minutes,
        "min_confidence": app_settings.min_confidence,
        "min_net_reward_risk": app_settings.min_net_reward_risk,
        "min_stop_atr": app_settings.min_stop_atr,
        "max_stop_atr": app_settings.max_stop_atr,
        "manual_exit_levels_enabled": app_settings.manual_exit_levels_enabled,
        "manual_stop_atr": app_settings.manual_stop_atr,
        "manual_take_profit_atr": app_settings.manual_take_profit_atr,
        "model_primary_portfolio_enabled": app_settings.model_primary_portfolio_enabled,
        "strong_trend_entry_override_enabled": app_settings.strong_trend_entry_override_enabled,
        "strong_trend_adx_min": app_settings.strong_trend_adx_min,
        "trend_adx_min": app_settings.trend_adx_min,
        "volatility_soft_limit_percentile": app_settings.volatility_soft_limit_percentile,
        "volatility_hard_limit_percentile": app_settings.volatility_hard_limit_percentile,
        "elevated_volatility_risk_multiplier": app_settings.elevated_volatility_risk_multiplier,
        "high_volatility_risk_multiplier": app_settings.high_volatility_risk_multiplier,
        "entry_symbols": app_settings.entry_symbols,
        "model_name": app_settings.active_model_name,
        "strategy_profile": app_settings.strategy_profile,
        "portfolio_strategy_enabled": app_settings.portfolio_strategy_enabled,
        "portfolio_rebalance_deadband_fraction": app_settings.portfolio_rebalance_deadband_fraction,
        "portfolio_rebalance_cooldown_minutes": app_settings.portfolio_rebalance_cooldown_minutes,
        "hft_enabled": app_settings.hft_enabled,
        "hft_dry_run": app_settings.hft_dry_run,
        "hft_symbols": app_settings.hft_symbols,
        "hft_event_interval_ms": app_settings.hft_event_interval_ms,
        "hft_max_spread_pct": app_settings.hft_max_spread_pct,
        "hft_min_depth_usdt": app_settings.hft_min_depth_usdt,
        "hft_order_notional_usdt": app_settings.hft_order_notional_usdt,
        "hft_max_inventory_usdt": app_settings.hft_max_inventory_usdt,
        "hft_cooldown_seconds": app_settings.hft_cooldown_seconds,
        "hft_market_stale_seconds": app_settings.hft_market_stale_seconds,
        "hft_max_consecutive_losses": app_settings.hft_max_consecutive_losses,
        "hft_imbalance_threshold": app_settings.hft_imbalance_threshold,
    }


@router.get("/integrations")
async def integrations(service: Controller, _: CurrentUser) -> dict[str, Any]:
    return await service.integration_status()


@router.post("/integrations/probe")
async def probe_integration(
    payload: IntegrationProbeRequest,
    request: Request,
    user: MutatingUser,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    if payload.target not in {"testnet", "model"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Unknown integration probe target",
        )
    target = "binance_testnet" if payload.target == "testnet" else "model_relay"
    try:
        result = await service.probe_integration(target)
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="probe_integration",
        resource=target,
        outcome="success" if result["state"] == "HEALTHY" else "failed",
        detail={"state": result["state"], "detail": result.get("detail")},
        ip_address=request.client.host if request.client else None,
    )
    return result


@router.patch("/integrations/model")
async def update_model_integration(
    payload: ModelRelayUpdateRequest,
    request: Request,
    user: MutatingUser,
    app_settings: AppSettings,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    if (
        app_settings.app_env == "production"
        and payload.base_url
        and not payload.base_url.startswith("https://")
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Production model relay must use HTTPS",
        )
    updates = {
        "model_base_url": payload.base_url,
        "model_name": payload.model_name,
        "model_reasoning_effort": payload.reasoning_effort,
        "model_timeout_seconds": payload.timeout_seconds,
        "strategy_profile": payload.strategy_profile,
    }
    await repo.save_runtime_config(updates)
    for key, value in updates.items():
        setattr(app_settings, key, value)
    service.invalidate_health_cache()
    await repo.audit(
        actor=user.username,
        action="update_model_integration",
        resource="model_relay",
        outcome="success",
        detail={"base_url_configured": bool(payload.base_url), "model_name": payload.model_name},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "base_url": app_settings.model_base_url,
        "model_name": app_settings.model_name,
        "reasoning_effort": app_settings.model_reasoning_effort,
        "timeout_seconds": app_settings.model_timeout_seconds,
        "strategy_profile": app_settings.strategy_profile,
        "api_key_configured": bool(app_settings.model_api_key),
    }


@router.patch("/integrations/model/profile")
async def select_model_profile(
    payload: ModelProfileSelectRequest,
    request: Request,
    user: MutatingUser,
    app_settings: AppSettings,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    profiles = {str(item["id"]): item for item in app_settings.model_profiles}
    selected = profiles[payload.profile_id]
    if not selected["configured"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Model profile {payload.profile_id} is not fully configured",
        )
    await repo.save_runtime_config({"model_profile": payload.profile_id})
    app_settings.model_profile = payload.profile_id
    service.invalidate_health_cache()
    await repo.audit(
        actor=user.username,
        action="select_model_profile",
        resource="model_relay",
        outcome="success",
        detail={
            "profile_id": payload.profile_id,
            "model_name": app_settings.active_model_name,
        },
        ip_address=request.client.host if request.client else None,
    )
    return cast(dict[str, Any], (await service.integration_status())["model"])


@router.patch("/config")
async def update_config(
    payload: ConfigUpdateRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    app_settings: AppSettings,
    repo: Repo,
) -> dict[str, Any]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    updates = payload.model_dump(exclude_none=True, exclude={"password"})
    if (
        updates.get("portfolio_strategy_enabled") or updates.get("hft_enabled")
    ) and app_settings.binance_environment != "testnet":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Portfolio-v1 and HFT strategies are limited to Binance testnet",
        )
    if updates.get("manual_exit_levels_enabled") and app_settings.binance_environment != "testnet":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "manual exit levels are limited to Binance testnet",
        )
    if (
        updates.get("model_primary_portfolio_enabled")
        and app_settings.binance_environment != "testnet"
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "model-primary portfolio mode is limited to Binance testnet",
        )
    if (
        updates.get("strong_trend_entry_override_enabled")
        and app_settings.binance_environment != "testnet"
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "strong trend entry override is limited to Binance testnet",
        )
    # The UI confirms this whole configuration change with the operator password.
    proposed_min_stop = updates.get("min_stop_atr", app_settings.min_stop_atr)
    proposed_max_stop = updates.get("max_stop_atr", app_settings.max_stop_atr)
    if proposed_min_stop > proposed_max_stop:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "minimum stop ATR cannot exceed maximum stop ATR",
        )
    proposed_manual_stop = updates.get("manual_stop_atr", app_settings.manual_stop_atr)
    manual_exits_enabled = updates.get(
        "manual_exit_levels_enabled", app_settings.manual_exit_levels_enabled
    )
    if manual_exits_enabled and proposed_manual_stop > proposed_max_stop:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "manual stop ATR cannot exceed maximum stop ATR",
        )
    proposed_soft_vol = updates.get(
        "volatility_soft_limit_percentile", app_settings.volatility_soft_limit_percentile
    )
    proposed_hard_vol = updates.get(
        "volatility_hard_limit_percentile", app_settings.volatility_hard_limit_percentile
    )
    if proposed_soft_vol > proposed_hard_vol:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "volatility soft limit cannot exceed hard limit",
        )
    if app_settings.binance_environment == "live":
        live_caps = {
            "max_leverage": (3, "live Binance environment caps max_leverage at 3x"),
            "candidate_count": (5, "live Binance environment caps candidate_count at 5"),
            "max_positions": (3, "live Binance environment caps max_positions at 3"),
            "single_trade_risk_pct": (
                0.0025,
                "live Binance environment caps single_trade_risk_pct at 0.25%",
            ),
            "portfolio_risk_pct": (
                0.0075,
                "live Binance environment caps portfolio_risk_pct at 0.75%",
            ),
        }
        for key, (maximum, message) in live_caps.items():
            if key in updates and updates[key] > maximum:
                raise HTTPException(status.HTTP_409_CONFLICT, message)
    persisted = {
        key: (
            int(value)
            if key
            in {
                "max_leverage",
                "max_positions",
                "max_same_direction",
                "candidate_count",
                "scan_interval_minutes",
                "portfolio_rebalance_cooldown_minutes",
                "hft_event_interval_ms",
                "hft_cooldown_seconds",
                "hft_max_consecutive_losses",
            }
            else float(value)
            if key
            not in {
                "entry_direction",
                "entry_trigger",
                "entry_symbols",
                "portfolio_strategy_enabled",
                "manual_exit_levels_enabled",
                "model_primary_portfolio_enabled",
                "strong_trend_entry_override_enabled",
                "hft_symbols",
                "hft_enabled",
                "hft_dry_run",
            }
            else value
        )
        for key, value in updates.items()
    }
    await repo.save_runtime_config(persisted)
    for key, value in persisted.items():
        setattr(app_settings, key, value)
    await repo.audit(
        actor=user.username,
        action="update_config",
        resource="risk_limits",
        outcome="success",
        detail={"changed_fields": sorted(updates), "confirmation": "password"},
        ip_address=request.client.host if request.client else None,
    )
    return await get_config(app_settings, user)


@router.post("/actions/pause")
async def pause(
    request: Request, user: MutatingUser, service: Controller, repo: Repo
) -> dict[str, str]:
    mode = await service.pause()
    await repo.audit(
        actor=user.username,
        action="pause_entries",
        resource="system",
        outcome="success",
        ip_address=request.client.host if request.client else None,
    )
    return {"mode": mode.value}


@router.post("/actions/run-cycle")
async def run_cycle(
    payload: PasswordActionRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, str]:
    # A cycle may place testnet orders, so every manual trigger uses the same
    # operator-password confirmation.  Live mode still has its separate unlock
    # gate in addition to this check.
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    try:
        operation_id = await service.queue_cycle()
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="run_cycle",
        resource="trading-cycle",
        outcome="queued",
        detail={"operation_id": operation_id},
        ip_address=request.client.host if request.client else None,
    )
    return {"status": "QUEUED", "operation_id": operation_id}


@router.post("/actions/resume-testnet")
async def resume_testnet(
    payload: PasswordActionRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, str]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    try:
        mode = await service.resume_testnet()
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="resume_testnet",
        resource="system",
        outcome="success",
        detail={"confirmation": "password"},
        ip_address=request.client.host if request.client else None,
    )
    return {"mode": mode.value}


@router.post("/actions/reconcile")
async def reconcile_positions(
    payload: PasswordActionRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, str]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    try:
        mode = await service.reconcile_positions()
    except ValueError as error:
        await repo.audit(
            actor=user.username,
            action="reconcile_positions",
            resource="positions",
            outcome="denied",
            detail={"reason": str(error)},
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="reconcile_positions",
        resource="positions",
        outcome="success",
        ip_address=request.client.host if request.client else None,
    )
    return {"mode": mode.value}


@router.post("/actions/unlock-live")
async def unlock_live(
    payload: PasswordActionRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, str]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    try:
        mode = await service.unlock_live()
    except ValueError as error:
        await repo.audit(
            actor=user.username,
            action="unlock_live",
            resource="system",
            outcome="denied",
            detail={"reason": str(error)},
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="unlock_live",
        resource="system",
        outcome="success",
        ip_address=request.client.host if request.client else None,
    )
    return {"mode": mode.value}


@router.post("/actions/emergency-flatten")
async def emergency_flatten(
    payload: PasswordActionRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    orders = await service.emergency_flatten()
    await repo.audit(
        actor=user.username,
        action="emergency_flatten",
        resource="positions",
        outcome="success",
        detail={"closed_orders": len(orders)},
        ip_address=request.client.host if request.client else None,
    )
    return {"mode": "PAUSED", "orders": orders}


@router.post("/actions/reduce-position")
async def reduce_position(
    payload: ReducePositionRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    try:
        order = await service.reduce_position(
            payload.position_id, payload.fraction, payload.operation_id
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="reduce_position",
        resource=payload.position_id,
        outcome="success",
        detail={
            "fraction": str(payload.fraction),
            "operation_id": payload.operation_id,
            "client_order_id": order["client_order_id"],
        },
        ip_address=request.client.host if request.client else None,
    )
    return order


@router.post("/actions/manual-entry")
async def manual_entry(
    payload: ManualEntryRequest,
    request: Request,
    user: MutatingUser,
    security: Security,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    if not security.verify_password(payload.password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Password verification failed")
    try:
        result = await service.manual_entry(
            operation_id=payload.operation_id,
            symbol=payload.symbol,
            side=payload.side,
            leverage=payload.leverage,
            stop_distance_pct=payload.stop_distance_pct,
            tp1_r=payload.tp1_r,
            tp2_r=payload.tp2_r,
        )
    except (ValueError, RuntimeError) as error:
        await repo.audit(
            actor=user.username,
            action="manual_entry",
            resource=payload.symbol,
            outcome="denied",
            detail={"reason": str(error), "operation_id": payload.operation_id},
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    entry = result["entry"]
    await repo.audit(
        actor=user.username,
        action="manual_entry",
        resource=payload.symbol,
        outcome="success",
        detail={
            "operation_id": payload.operation_id,
            "side": payload.side,
            "leverage": payload.leverage,
            "client_order_id": entry["client_order_id"],
            "filled_quantity": entry["filled_quantity"],
            "protected": bool(result.get("position") and result["position"]["protected"]),
        },
        ip_address=request.client.host if request.client else None,
    )
    return result


@router.post("/actions/manual-entry/advice")
async def manual_entry_advice(
    payload: ManualEntryAdviceRequest,
    request: Request,
    user: MutatingUser,
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    try:
        result = await service.advise_manual_entry(
            symbol=payload.symbol,
            side=payload.side,
            leverage=payload.leverage,
            stop_distance_pct=payload.stop_distance_pct,
            tp1_r=payload.tp1_r,
            tp2_r=payload.tp2_r,
            messages=[item.model_dump() for item in payload.messages],
        )
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="manual_entry_advice",
        resource=payload.symbol,
        outcome="success",
        detail={"side": payload.side, "message_count": len(payload.messages)},
        ip_address=request.client.host if request.client else None,
    )
    return result


@router.post("/replays", status_code=status.HTTP_202_ACCEPTED)
async def create_replay(
    payload: ReplayRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    user: MutatingUser,
    repo: Repo,
    replays: Replays,
) -> dict[str, Any]:
    # JSON mode turns Decimal replay overrides into strings so the immutable
    # request snapshot can be safely stored in the JSON column.
    parameters = payload.model_dump(mode="json")
    replay_id = await replays.create(parameters)
    background_tasks.add_task(replays.run, replay_id, parameters)
    await repo.audit(
        actor=user.username,
        action="create_replay",
        resource=replay_id,
        outcome="queued",
        detail={
            "mode": payload.mode,
            "symbols": payload.symbols,
            "portfolio_decision_id": payload.portfolio_decision_id,
        },
        ip_address=request.client.host if request.client else None,
    )
    return {"id": replay_id, "status": "QUEUED", "parameters": parameters}


@router.get("/replays")
async def list_replays(repo: Repo, _: CurrentUser) -> list[dict[str, Any]]:
    return await repo.list_replays()


@router.get("/factors/catalog")
async def factor_catalog(research: FactorResearch, _: CurrentUser) -> dict[str, object]:
    return research.catalog()


@router.post("/factors/research", status_code=status.HTTP_202_ACCEPTED)
async def research_factors(
    payload: FactorResearchRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    research: FactorResearch,
    user: MutatingUser,
    repo: Repo,
) -> dict[str, Any]:
    parameters = payload.model_dump(mode="json")
    run_id = await research.create(parameters)
    background_tasks.add_task(research.execute, run_id, parameters)
    await repo.audit(
        actor=user.username,
        action="create_factor_research",
        resource=run_id,
        outcome="queued",
        detail={"symbols": payload.symbols, "interval": payload.interval},
        ip_address=request.client.host if request.client else None,
    )
    return {"id": run_id, "status": "QUEUED", "parameters": parameters}


@router.get("/factors/research")
async def list_factor_research(repo: Repo, _: CurrentUser, limit: int = 20) -> list[dict[str, Any]]:
    return await repo.list_factor_research_runs(limit=min(max(limit, 1), 50))
