from __future__ import annotations

import asyncio
import csv
import io
import zipfile
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from trading_system.config import Settings
from trading_system.domain.models import Candle
from trading_system.exchange.base import ExchangeError


class BinanceHistoricalDataClient:
    """Read USD-M history from Binance's public data archive.

    This client deliberately has no Binance API key and never calls a Futures
    REST endpoint. It is used by research workloads so large historical
    downloads cannot consume the trading client's REST request budget.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        base_url: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.binance_historical_data_url).rstrip("/")
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=60,
            transport=transport,
            proxy=proxy_url,
            trust_env=False,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self._archive_cache: dict[str, bytes] = {}
        self._archive_downloads: dict[str, asyncio.Task[bytes | None]] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def get_historical_klines(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[Candle]:
        if interval not in {"15m", "1h", "4h"}:
            raise ExchangeError(f"unsupported archived kline interval: {interval}")
        if end_ms <= start_ms:
            return []

        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
        rows: list[Candle] = []
        current_month = datetime.now(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        for month_start in self._iter_months(start, end):
            month_end = self._next_month(month_start)
            if month_start == current_month:
                payloads = await self._download_daily_klines(
                    symbol, interval, max(start, month_start), min(end, month_end)
                )
            else:
                monthly_path = (
                    f"/data/futures/um/monthly/klines/{symbol}/{interval}/"
                    f"{symbol}-{interval}-{month_start:%Y-%m}.zip"
                )
                monthly = await self._download_archive(monthly_path)
                payloads = [monthly] if monthly is not None else []
                if monthly is None:
                    # Newly listed symbols and the most recently completed
                    # month may not have a monthly archive yet.
                    payloads = await self._download_daily_klines(
                        symbol, interval, max(start, month_start), min(end, month_end)
                    )
            for payload in payloads:
                rows.extend(self._parse_klines(payload, start_ms, end_ms))

        by_open_time = {int(candle.open_time.timestamp() * 1000): candle for candle in rows}
        return [by_open_time[key] for key in sorted(by_open_time)]

    async def get_historical_funding_rates(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> dict[datetime, Decimal]:
        if end_ms <= start_ms:
            return {}
        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        end = datetime.fromtimestamp(end_ms / 1000, tz=UTC)
        rates: dict[datetime, Decimal] = {}
        for month_start in self._iter_months(start, end):
            path = (
                f"/data/futures/um/monthly/fundingRate/{symbol}/"
                f"{symbol}-fundingRate-{month_start:%Y-%m}.zip"
            )
            payload = await self._download_archive(path)
            if payload is not None:
                rates.update(self._parse_funding_rates(payload, start_ms, end_ms))
        return dict(sorted(rates.items()))

    async def _download_daily_klines(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> list[bytes]:
        day = max(start.date(), date(start.year, start.month, 1))
        end_date = end.date()
        payloads: list[bytes] = []
        while day < end_date or (day == end_date and end.time() != datetime.min.time()):
            path = (
                f"/data/futures/um/daily/klines/{symbol}/{interval}/"
                f"{symbol}-{interval}-{day:%Y-%m-%d}.zip"
            )
            payload = await self._download_archive(path)
            if payload is not None:
                payloads.append(payload)
            day = date.fromordinal(day.toordinal() + 1)
        return payloads

    async def _download_archive(self, path: str) -> bytes | None:
        cached = self._archive_cache.get(path)
        if cached is not None:
            return cached
        task = self._archive_downloads.get(path)
        if task is None:
            task = asyncio.create_task(self._fetch_archive(path))
            self._archive_downloads[path] = task
        try:
            payload = await asyncio.shield(task)
        finally:
            if task.done() and self._archive_downloads.get(path) is task:
                self._archive_downloads.pop(path, None)
        if payload is not None:
            self._archive_cache[path] = payload
        return payload

    async def _fetch_archive(self, path: str) -> bytes | None:
        try:
            response = await self.http.get(path)
        except httpx.HTTPError as error:
            raise ExchangeError("Binance historical data archive request failed") from error
        if response.status_code == 404:
            return None
        if response.is_error:
            raise ExchangeError(
                f"Binance historical data archive returned HTTP {response.status_code}"
            )
        return response.content

    @staticmethod
    def _iter_months(start: datetime, end: datetime) -> list[datetime]:
        cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows: list[datetime] = []
        while cursor < end:
            rows.append(cursor)
            cursor = BinanceHistoricalDataClient._next_month(cursor)
        return rows

    @staticmethod
    def _next_month(value: datetime) -> datetime:
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1)
        return value.replace(month=value.month + 1)

    @classmethod
    def _parse_klines(cls, payload: bytes, start_ms: int, end_ms: int) -> list[Candle]:
        rows: list[Candle] = []
        for row in cls._csv_rows(payload):
            try:
                open_ms = cls._timestamp_ms(cls._required(row, "open_time", "Open time"))
                close_ms = cls._timestamp_ms(cls._required(row, "close_time", "Close time"))
                if open_ms < start_ms or open_ms >= end_ms:
                    continue
                rows.append(
                    Candle(
                        open_time=datetime.fromtimestamp(open_ms / 1000, tz=UTC),
                        close_time=datetime.fromtimestamp(close_ms / 1000, tz=UTC),
                        open=Decimal(cls._required(row, "open", "Open")),
                        high=Decimal(cls._required(row, "high", "High")),
                        low=Decimal(cls._required(row, "low", "Low")),
                        close=Decimal(cls._required(row, "close", "Close")),
                        volume=Decimal(cls._required(row, "volume", "Volume")),
                    )
                )
            except (ArithmeticError, KeyError, TypeError, ValueError):
                continue
        return rows

    @classmethod
    def _parse_funding_rates(
        cls, payload: bytes, start_ms: int, end_ms: int
    ) -> dict[datetime, Decimal]:
        rates: dict[datetime, Decimal] = {}
        for row in cls._csv_rows(payload):
            try:
                timestamp = cls._timestamp_ms(
                    cls._required(row, "fundingTime", "calc_time", "calcTime")
                )
                if not start_ms <= timestamp < end_ms:
                    continue
                rate = Decimal(
                    cls._required(row, "fundingRate", "last_funding_rate", "lastFundingRate")
                )
                rates[datetime.fromtimestamp(timestamp / 1000, tz=UTC)] = rate
            except (ArithmeticError, KeyError, TypeError, ValueError):
                continue
        return rates

    @staticmethod
    def _csv_rows(payload: bytes) -> list[dict[str, str | None]]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
                if not names:
                    raise ExchangeError("Binance historical data archive has no CSV")
                with archive.open(names[0]) as raw_file:
                    text = io.TextIOWrapper(raw_file, encoding="utf-8-sig", newline="")
                    return [
                        {str(key): value for key, value in row.items() if key is not None}
                        for row in csv.DictReader(text)
                    ]
        except (OSError, zipfile.BadZipFile) as error:
            raise ExchangeError("Binance historical data archive is malformed") from error

    @staticmethod
    def _required(row: Mapping[str, str | None], *names: str) -> str:
        for name in names:
            value = row.get(name)
            if value is not None and value != "":
                return value
        raise KeyError(names[0])

    @staticmethod
    def _timestamp_ms(value: str) -> int:
        timestamp = int(value)
        # Keep this tolerant of microsecond files if the parser is reused by
        # a caller outside USD-M Futures.
        return timestamp // 1_000 if timestamp >= 100_000_000_000_000 else timestamp
