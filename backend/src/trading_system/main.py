from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from trading_system import __version__
from trading_system.ai.client import ResponsesModelClient
from trading_system.api.controller import SystemController
from trading_system.api.routes import router
from trading_system.api.security import SecurityService
from trading_system.backtest.service import ReplayService
from trading_system.config import get_settings
from trading_system.exchange.binance import BinanceUSDMarketClient
from trading_system.exchange.market_stream import BinancePublicMarketCache
from trading_system.notifications.telegram import TelegramNotifier
from trading_system.persistence.database import Database
from trading_system.persistence.repository import Repository
from trading_system.strategy.factor_service import FactorResearchService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    security = SecurityService(settings)
    if (
        settings.app_env == "production" or settings.binance_environment == "live"
    ) and not security.production_configured():
        raise RuntimeError("production authentication and secure cookies are not configured")
    database = Database(settings.database_connection_url)
    if settings.app_env != "production":
        await database.create_schema()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    repository = Repository(database)
    await repository.apply_runtime_config(settings)
    exchange = BinanceUSDMarketClient(settings)
    market_stream = BinancePublicMarketCache(
        settings.binance_ws_url,
        proxy_url=settings.binance_http_proxy_url,
    )
    exchange.attach_market_cache(market_stream)
    if exchange.configured and (
        not settings.binance_proxy_enabled or settings.binance_http_proxy_configured
    ):
        market_stream.start()
    research_exchange = BinanceUSDMarketClient(
        settings,
        public_base_url=settings.binance_live_base_url,
        # Factor research reads production public history. It may use the
        # shared outbound proxy even when signed trading requests stay direct.
        proxy_url=settings.http_proxy_url,
    )
    model = ResponsesModelClient(settings)
    notifier = TelegramNotifier(settings)
    replay_service = ReplayService(repository, research_exchange, notifier, settings)
    factor_research_service = FactorResearchService(
        repository, research_exchange, settings.app_timezone
    )
    controller = SystemController(settings, database, redis, repository, exchange, model)

    app.state.settings = settings
    app.state.database = database
    app.state.redis = redis
    app.state.repository = repository
    app.state.exchange = exchange
    app.state.research_exchange = research_exchange
    app.state.model = model
    app.state.security = security
    app.state.controller = controller
    app.state.notifier = notifier
    app.state.replay_service = replay_service
    app.state.factor_research_service = factor_research_service
    yield

    await model.close()
    await notifier.close()
    await market_stream.close()
    await research_exchange.close()
    await exchange.close()
    await redis.aclose()
    await database.dispose()


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version=__version__,
    docs_url="/api/docs" if settings.app_env != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
app.include_router(router)
