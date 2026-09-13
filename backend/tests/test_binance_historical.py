from __future__ import annotations

import asyncio
import csv
import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from trading_system.config import Settings
from trading_system.exchange.binance_historical import BinanceHistoricalDataClient


def archive(filename: str, header: list[str], rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as result:
        result.writestr(filename, output.getvalue())
    return payload.getvalue()


def settings(tmp_path: object) -> Settings:
    return Settings(
        secret_dir=tmp_path,
        binance_historical_data_url="https://data.example",
    )


@pytest.mark.asyncio
async def test_historical_client_reads_monthly_kline_and_funding_archives(
    tmp_path: object,
) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(datetime(2026, 8, 1, 2, tzinfo=UTC).timestamp() * 1000)
    files = {
        "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-08.zip": archive(
            "BTCUSDT-1h-2026-08.csv",
            ["open_time", "open", "high", "low", "close", "volume", "close_time"],
            [
                [str(start_ms), "100", "102", "99", "101", "10", str(start_ms + 3_599_999)],
                [
                    str(start_ms + 3_600_000),
                    "101",
                    "103",
                    "100",
                    "102",
                    "11",
                    str(start_ms + 7_199_999),
                ],
            ],
        ),
        "/data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-08.zip": archive(
            "BTCUSDT-fundingRate-2026-08.csv",
            ["calc_time", "funding_interval_hours", "last_funding_rate"],
            [[str(start_ms), "8", "0.0001"], [str(end_ms), "8", "0.0002"]],
        ),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = files.get(request.url.path)
        return httpx.Response(200, content=payload) if payload is not None else httpx.Response(404)

    client = BinanceHistoricalDataClient(settings(tmp_path), httpx.MockTransport(handler))
    try:
        candles, funding = await asyncio.gather(
            client.get_historical_klines("BTCUSDT", "1h", start_ms, end_ms),
            client.get_historical_funding_rates("BTCUSDT", start_ms, end_ms),
        )
    finally:
        await client.close()

    assert [item.close for item in candles] == [Decimal("101"), Decimal("102")]
    assert funding == {start: Decimal("0.0001")}


@pytest.mark.asyncio
async def test_historical_client_deduplicates_concurrent_archive_downloads(
    tmp_path: object,
) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(datetime(2026, 8, 1, 2, tzinfo=UTC).timestamp() * 1000)
    path = "/data/futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-08.zip"
    payload = archive(
        "BTCUSDT-1h-2026-08.csv",
        ["open_time", "open", "high", "low", "close", "volume", "close_time"],
        [[str(start_ms), "100", "102", "99", "101", "10", str(start_ms + 3_599_999)]],
    )
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.url.path == path
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=payload)

    client = BinanceHistoricalDataClient(settings(tmp_path), httpx.MockTransport(handler))
    try:
        results = await asyncio.gather(
            client.get_historical_klines("BTCUSDT", "1h", start_ms, end_ms),
            client.get_historical_klines("BTCUSDT", "1h", start_ms, end_ms),
        )
    finally:
        await client.close()

    assert calls == 1
    assert [len(result) for result in results] == [1, 1]


@pytest.mark.asyncio
async def test_historical_client_daily_fallback_includes_partial_end_day(
    tmp_path: object,
) -> None:
    start = datetime(2026, 7, 1, 1, tzinfo=UTC)
    end = datetime(2026, 7, 1, 2, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    daily_path = "/data/futures/um/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-07-01.zip"
    payload = archive(
        "BTCUSDT-1h-2026-07-01.csv",
        ["open_time", "open", "high", "low", "close", "volume", "close_time"],
        [[str(start_ms), "100", "102", "99", "101", "10", str(start_ms + 3_599_999)]],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("2026-07.zip"):
            return httpx.Response(404)
        if request.url.path == daily_path:
            return httpx.Response(200, content=payload)
        return httpx.Response(404)

    client = BinanceHistoricalDataClient(settings(tmp_path), httpx.MockTransport(handler))
    try:
        candles = await client.get_historical_klines("BTCUSDT", "1h", start_ms, end_ms)
    finally:
        await client.close()

    assert [item.close for item in candles] == [Decimal("101")]

