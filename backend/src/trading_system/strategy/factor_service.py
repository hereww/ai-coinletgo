from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trading_system.config import HISTORICAL_RESEARCH_DISABLED_MESSAGE
from trading_system.exchange.binance_historical import BinanceHistoricalDataClient
from trading_system.persistence.repository import Repository
from trading_system.strategy.factor_research import (
    align_funding_point_in_time,
    run_factor_research,
)
from trading_system.strategy.factors import FactorBar, factor_definitions_payload

DATA_SOURCES: list[dict[str, object]] = [
    {
        "key": "price",
        "label": "价格K线",
        "available": True,
        "detail": "Binance USD-M 公共历史归档K线，按收盘时间使用，不占 Futures REST 配额",
    },
    {"key": "volume", "label": "成交量", "available": True, "detail": "来自同一历史K线"},
    {
        "key": "funding",
        "label": "资金费率",
        "available": True,
        "detail": "公共历史归档资金费率，仅向前对齐已发生的事件",
    },
    {
        "key": "open_interest",
        "label": "历史OI",
        "available": False,
        "detail": "项目尚未接入可验证的历史OI序列",
    },
    {
        "key": "basis",
        "label": "历史基差",
        "available": False,
        "detail": "项目尚未接入可验证的标记价/指数价历史",
    },
    {
        "key": "order_book",
        "label": "历史盘口",
        "available": False,
        "detail": "项目只有实时L2数据，没有历史盘口回放源",
    },
]


class FactorResearchService:
    def __init__(
        self,
        repository: Repository,
        exchange: BinanceHistoricalDataClient,
        timezone_name: str,
        *,
        enabled: bool = False,
    ) -> None:
        self.repository = repository
        self.exchange = exchange
        self.timezone_name = timezone_name
        self.enabled = enabled
        self._run_semaphore = asyncio.Semaphore(1)

    def catalog(self) -> dict[str, object]:
        return {
            "factors": factor_definitions_payload(),
            "data_sources": DATA_SOURCES,
            "market_source": "Binance USD-M public data archive (data.binance.vision)",
            "live_trading_connected": False,
            "historical_research_enabled": self.enabled,
        }

    async def create(self, parameters: dict[str, object]) -> str:
        self._require_enabled()
        return await self.repository.create_factor_research_run(parameters)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError(HISTORICAL_RESEARCH_DISABLED_MESSAGE)

    async def execute(self, run_id: str, parameters: dict[str, object]) -> None:
        async with self._run_semaphore:
            await self.repository.set_factor_research_running(run_id)
            try:
                raw_symbols = parameters["symbols"]
                if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, str):
                    raise ValueError("factor research symbols must be a list")
                report = await self.run(
                    symbols=[str(item) for item in raw_symbols],
                    start_date=date.fromisoformat(str(parameters["start_date"])),
                    end_date=date.fromisoformat(str(parameters["end_date"])),
                    interval=str(parameters["interval"]),
                    forward_bars=int(str(parameters["forward_bars"])),
                    rebalance_bars=int(str(parameters["rebalance_bars"])),
                    winsorize_quantile=Decimal(str(parameters["winsorize_quantile"])),
                    min_cross_section=int(str(parameters["min_cross_section"])),
                    maker_fee_rate=Decimal(str(parameters.get("maker_fee_rate", "0.0002"))),
                    taker_fee_rate=Decimal(str(parameters.get("taker_fee_rate", "0.0005"))),
                    slippage_rate=Decimal(str(parameters.get("slippage_rate", "0.0005"))),
                    funding_rate_fallback=Decimal(
                        str(parameters.get("funding_rate_fallback", "0.0001"))
                    ),
                    walk_forward_folds=int(str(parameters.get("walk_forward_folds", 4))),
                    portfolio_quantile=Decimal(str(parameters.get("portfolio_quantile", "0.2"))),
                )
                await self.repository.complete_factor_research(run_id, report)
            except Exception as error:
                await self.repository.fail_factor_research(run_id, str(error)[:500])

    async def run(
        self,
        *,
        symbols: list[str],
        start_date: date,
        end_date: date,
        interval: str,
        forward_bars: int,
        rebalance_bars: int,
        winsorize_quantile: Decimal,
        min_cross_section: int,
        maker_fee_rate: Decimal = Decimal("0.0002"),
        taker_fee_rate: Decimal = Decimal("0.0005"),
        slippage_rate: Decimal = Decimal("0.0005"),
        funding_rate_fallback: Decimal = Decimal("0.0001"),
        walk_forward_folds: int = 4,
        portfolio_quantile: Decimal = Decimal("0.2"),
    ) -> dict[str, Any]:
        self._require_enabled()
        bars_per_day_by_interval = {"1h": 24, "4h": 6}
        if interval not in bars_per_day_by_interval:
            raise ValueError("unsupported factor interval")
        timezone = ZoneInfo(self.timezone_name)
        start = datetime.combine(start_date, time.min, timezone).astimezone(UTC)
        end = datetime.combine(end_date + timedelta(days=1), time.min, timezone).astimezone(UTC)
        warmup_start = start - timedelta(days=30)
        semaphore = asyncio.Semaphore(3)

        async def load_symbol(symbol: str) -> tuple[str, list[FactorBar]]:
            async with semaphore:
                candles, funding = await asyncio.gather(
                    self.exchange.get_historical_klines(
                        symbol,
                        interval,
                        int(warmup_start.timestamp() * 1000),
                        int(end.timestamp() * 1000),
                    ),
                    self.exchange.get_historical_funding_rates(
                        symbol,
                        int(warmup_start.timestamp() * 1000),
                        int(end.timestamp() * 1000),
                    ),
                )
            now = datetime.now(UTC)
            closed = [
                candle for candle in candles if candle.close_time < end and candle.close_time <= now
            ]
            return symbol, align_funding_point_in_time(closed, funding)

        loaded = await asyncio.gather(*(load_symbol(symbol) for symbol in symbols))
        markets = {symbol: rows for symbol, rows in loaded if rows}
        if len(markets) < min_cross_section:
            raise ValueError(
                f"only {len(markets)} symbols returned history; {min_cross_section} required"
            )
        bars_per_day = bars_per_day_by_interval[interval]
        report = await asyncio.to_thread(
            run_factor_research,
            markets,
            evaluation_start=start,
            evaluation_end=end,
            bars_per_day=bars_per_day,
            bars_per_year=bars_per_day * 365,
            forward_bars=forward_bars,
            rebalance_bars=rebalance_bars,
            min_cross_section=min_cross_section,
            winsorize_quantile=winsorize_quantile,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            slippage_rate=slippage_rate,
            funding_rate_fallback=funding_rate_fallback,
            walk_forward_folds=walk_forward_folds,
            portfolio_quantile=portfolio_quantile,
        )
        report["parameters"] = {
            "symbols": list(markets),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "interval": interval,
            "forward_bars": forward_bars,
            "rebalance_bars": rebalance_bars,
            "winsorize_quantile": str(winsorize_quantile),
            "min_cross_section": min_cross_section,
            "maker_fee_rate": str(Decimal(str(maker_fee_rate))),
            "taker_fee_rate": str(Decimal(str(taker_fee_rate))),
            "slippage_rate": str(Decimal(str(slippage_rate))),
            "funding_rate_fallback": str(Decimal(str(funding_rate_fallback))),
            "walk_forward_folds": walk_forward_folds,
            "portfolio_quantile": str(Decimal(str(portfolio_quantile))),
        }
        report["data_sources"] = DATA_SOURCES
        report["market_source"] = "Binance USD-M public data archive (data.binance.vision)"
        report["live_trading_connected"] = False
        return report
