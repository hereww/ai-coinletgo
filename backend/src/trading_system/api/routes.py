from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status

from trading_system.api.controller import SystemController
from trading_system.api.schemas import (
    ConfigUpdateRequest,
    LoginRequest,
    ManualEntryAdviceRequest,
    ManualEntryRequest,
    ModelProfileSelectRequest,
    ModelRelayUpdateRequest,
    PasswordActionRequest,
    ReducePositionRequest,
    ReplayRequest,
)
from trading_system.api.security import CurrentUser, MutatingUser, SecurityService
from trading_system.backtest.service import ReplayService
from trading_system.config import Settings
from trading_system.persistence.repository import Repository

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


Controller = Annotated[SystemController, Depends(controller)]
Repo = Annotated[Repository, Depends(repository)]
AppSettings = Annotated[Settings, Depends(settings)]
Security = Annotated[SecurityService, Depends(security_service)]
Replays = Annotated[ReplayService, Depends(replay_service)]


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
        session_token, csrf_token = security.login(
            payload.username, payload.password, client_ip
        )
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
        "entry_symbols": app_settings.entry_symbols,
        "model_name": app_settings.active_model_name,
        "model_daily_request_limit": app_settings.model_daily_request_limit,
        "portfolio_strategy_enabled": app_settings.portfolio_strategy_enabled,
        "portfolio_rebalance_deadband_fraction": app_settings.portfolio_rebalance_deadband_fraction,
        "portfolio_rebalance_cooldown_minutes": app_settings.portfolio_rebalance_cooldown_minutes,
    }


@router.get("/integrations")
async def integrations(service: Controller, _: CurrentUser) -> dict[str, Any]:
    return await service.integration_status()


@router.post("/integrations/probe")
async def probe_integration(
    payload: PasswordActionRequest,
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
        "model_daily_request_limit": payload.daily_request_limit,
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
        "daily_request_limit": app_settings.model_daily_request_limit,
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
    if updates.get("portfolio_strategy_enabled") and app_settings.binance_environment != "testnet":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Portfolio-v1 strategy is limited to Binance testnet",
        )
    # The UI confirms this whole configuration change with the operator password.
    proposed_min_stop = updates.get("min_stop_atr", app_settings.min_stop_atr)
    proposed_max_stop = updates.get("max_stop_atr", app_settings.max_stop_atr)
    if proposed_min_stop > proposed_max_stop:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "minimum stop ATR cannot exceed maximum stop ATR",
        )
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
            }
            else float(value)
            if key not in {
                "entry_direction",
                "entry_trigger",
                "entry_symbols",
                "portfolio_strategy_enabled",
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
    # Testnet cycles are bounded by the testnet gateway and hard risk gates, so
    # the dashboard can trigger them directly. Live manual cycles still require
    # the operator password even though live unlock has a separate gate.
    if service.settings.binance_environment == "live" and not security.verify_password(
        payload.password
    ):
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
    request: Request,
    user: MutatingUser,
    service: Controller,
    repo: Repo,
) -> dict[str, str]:
    try:
        mode = await service.resume_testnet()
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    await repo.audit(
        actor=user.username,
        action="resume_testnet",
        resource="system",
        outcome="success",
        detail={"confirmation": "direct_button"},
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
    service: Controller,
    repo: Repo,
) -> dict[str, Any]:
    if payload.confirmation != "OPEN TESTNET POSITION":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manual entry confirmation failed")
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
    parameters = payload.model_dump()
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
