from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx
import websockets


class BinancePublicMarketCache:
    """Keep Binance's public ticker, book, and mark data off the REST quota."""

    _streams = "!ticker@arr/!bookTicker/!markPrice@arr@1s"

    def __init__(
        self,
        ws_base_url: str,
        reconnect_delay: float = 2.0,
        proxy_url: str | None = None,
        stale_after_seconds: float = 10.0,
    ) -> None:
        self.ws_base_url = ws_base_url.rstrip("/")
        self.reconnect_delay = reconnect_delay
        self.proxy_url = proxy_url
        self.stale_after_seconds = stale_after_seconds
        self._stopped = False
        self._connected = False
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._rows: dict[str, dict[str, Any]] = {}
        self._last_update_at = 0.0

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="binance-public-market-stream")

    def stop(self) -> None:
        self._stopped = True

    async def close(self) -> None:
        self.stop()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait_for_symbols(self, symbols: set[str], wait_seconds: float = 8.0) -> bool:
        """Wait briefly for a complete public snapshot before using REST fallback."""

        if not symbols:
            return True
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            if self.has_symbols(symbols):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=min(0.25, remaining))
            except TimeoutError:
                pass
            self._ready.clear()

    def has_symbols(self, symbols: set[str]) -> bool:
        return all(
            self._is_complete_and_fresh(self._rows.get(symbol))
            and all(
                key in self._rows[symbol]
                for key in (
                    "quote_volume_24h",
                    "best_bid",
                    "best_ask",
                    "mark_price",
                    "index_price",
                    "funding_rate",
                )
            )
            for symbol in symbols
        )

    def snapshot(self, symbol: str) -> dict[str, Any] | None:
        row = self._rows.get(symbol.upper())
        if row is None or not self._is_complete_and_fresh(row):
            return None
        return dict(row)

    def book_snapshot(self, symbol: str) -> dict[str, Any] | None:
        row = self._rows.get(symbol.upper())
        if row is None or not self._timestamp_is_fresh(row.get("book_updated_at")):
            return None
        if "best_bid" not in row or "best_ask" not in row:
            return None
        return {"best_bid": row["best_bid"], "best_ask": row["best_ask"]}

    def mark_snapshot(self, symbol: str) -> dict[str, Any] | None:
        row = self._rows.get(symbol.upper())
        if row is None or not self._timestamp_is_fresh(row.get("mark_updated_at")):
            return None
        if "mark_price" not in row:
            return None
        return {"mark_price": row["mark_price"]}

    def snapshots(self) -> dict[str, dict[str, Any]]:
        return {
            symbol: dict(row)
            for symbol, row in self._rows.items()
            if self._is_complete_and_fresh(row)
        }

    async def _run(self) -> None:
        url = f"{self.ws_base_url}/stream?streams={self._streams}"
        while not self._stopped:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    proxy=self.proxy_url if self.proxy_url else None,
                ) as socket:
                    self._connected = True
                    async for message in socket:
                        payload = json.loads(message)
                        if isinstance(payload, dict):
                            self._apply_payload(payload)
            except (OSError, websockets.WebSocketException, json.JSONDecodeError):
                if not self._stopped:
                    await asyncio.sleep(self.reconnect_delay)
            finally:
                self._connected = False

    def _apply_payload(self, payload: dict[str, Any]) -> None:
        data = payload.get("data", payload)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._apply_row(item)
            return
        if isinstance(data, dict):
            self._apply_row(data)

    def _apply_row(self, data: dict[str, Any]) -> None:
        symbol = str(data.get("s", "")).upper()
        if not symbol:
            return
        row = self._rows.setdefault(symbol, {})
        event_type = str(data.get("e", ""))
        updated_at = asyncio.get_running_loop().time()
        if "q" in data:
            row["quote_volume_24h"] = str(data["q"])
            row["ticker_updated_at"] = updated_at
        if "b" in data and "a" in data:
            row["best_bid"] = str(data["b"])
            row["best_ask"] = str(data["a"])
            row["book_updated_at"] = updated_at
        if "p" in data and "i" in data and "r" in data:
            row["mark_price"] = str(data["p"])
            row["index_price"] = str(data["i"])
            row["funding_rate"] = str(data["r"])
            row["mark_updated_at"] = updated_at
        if event_type:
            row["last_event"] = event_type
        self._last_update_at = updated_at
        self._ready.set()

    def _is_complete_and_fresh(self, row: dict[str, Any] | None) -> bool:
        if row is None:
            return False
        return all(
            self._timestamp_is_fresh(row.get(key))
            for key in ("ticker_updated_at", "book_updated_at", "mark_updated_at")
        )

    def _timestamp_is_fresh(self, updated_at: Any) -> bool:
        return (
            isinstance(updated_at, (int, float))
            and asyncio.get_running_loop().time() - updated_at <= self.stale_after_seconds
        )


class BinanceMarketStream:
    def __init__(
        self,
        ws_base_url: str,
        reconnect_delay: float = 2.0,
        proxy_url: str | None = None,
    ) -> None:
        self.ws_base_url = ws_base_url.rstrip("/")
        self.reconnect_delay = reconnect_delay
        self.proxy_url = proxy_url
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def events(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        streams: list[str] = []
        for symbol in symbols:
            name = symbol.lower()
            streams.extend((f"{name}@kline_15m", f"{name}@bookTicker", f"{name}@markPrice@1s"))
        url = f"{self.ws_base_url}/stream?streams={'/'.join(streams)}"
        while not self._stopped:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    proxy=self.proxy_url if self.proxy_url else None,
                ) as socket:
                    async for message in socket:
                        payload = json.loads(message)
                        if isinstance(payload, dict):
                            yield payload
            except (OSError, websockets.WebSocketException, json.JSONDecodeError):
                if not self._stopped:
                    await asyncio.sleep(self.reconnect_delay)

    async def depth_events(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield raw Binance diff-depth events for the independent HFT data plane."""
        streams = [f"{symbol.lower()}@depth@100ms" for symbol in symbols]
        if not streams:
            return
        url = f"{self.ws_base_url}/stream?streams={'/'.join(streams)}"
        while not self._stopped:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    proxy=self.proxy_url if self.proxy_url else None,
                ) as socket:
                    yield {"_stream_event": "connected"}
                    async for message in socket:
                        payload = json.loads(message)
                        if not isinstance(payload, dict):
                            continue
                        data = payload.get("data", payload)
                        if isinstance(data, dict) and data.get("e") == "depthUpdate":
                            yield data
            except (OSError, websockets.WebSocketException, json.JSONDecodeError):
                if not self._stopped:
                    await asyncio.sleep(self.reconnect_delay)


class BinanceUserDataStream:
    def __init__(
        self,
        rest_base_url: str,
        ws_base_url: str,
        api_key: str,
        reconnect_delay: float = 2.0,
        proxy_url: str | None = None,
    ) -> None:
        self.ws_base_url = ws_base_url.rstrip("/")
        self.reconnect_delay = reconnect_delay
        self.proxy_url = proxy_url
        self.http = httpx.AsyncClient(
            base_url=rest_base_url,
            timeout=15,
            headers={"X-MBX-APIKEY": api_key},
            proxy=proxy_url,
            trust_env=False,
        )
        self._stopped = False
        self.connected = False

    def stop(self) -> None:
        self._stopped = True

    async def close(self) -> None:
        self.stop()
        await self.http.aclose()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while not self._stopped:
            keepalive: asyncio.Task[None] | None = None
            listen_key: str | None = None
            try:
                response = await self.http.post("/fapi/v1/listenKey")
                response.raise_for_status()
                listen_key = str(response.json()["listenKey"])
                async with websockets.connect(
                    f"{self.ws_base_url}/ws/{listen_key}",
                    ping_interval=20,
                    ping_timeout=10,
                    proxy=self.proxy_url if self.proxy_url else None,
                ) as socket:
                    self.connected = True
                    keepalive = asyncio.create_task(self._keepalive(listen_key, socket))
                    receive_task: asyncio.Task[Any] = asyncio.create_task(socket.recv())
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {receive_task, keepalive},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if keepalive in done:
                                keepalive.result()
                                raise OSError("Binance user stream keepalive stopped")
                            if receive_task not in done:
                                continue
                            message = receive_task.result()
                            if message is None:
                                break
                            payload = json.loads(message)
                            if isinstance(payload, dict):
                                if payload.get("e") == "listenKeyExpired":
                                    break
                                yield payload
                            receive_task = asyncio.create_task(socket.recv())
                    finally:
                        receive_task.cancel()
                        try:
                            await receive_task
                        except asyncio.CancelledError:
                            pass
            except (
                OSError,
                httpx.HTTPError,
                KeyError,
                ValueError,
                websockets.WebSocketException,
                json.JSONDecodeError,
            ):
                if not self._stopped:
                    await asyncio.sleep(self.reconnect_delay)
            finally:
                self.connected = False
                if keepalive is not None:
                    keepalive.cancel()
                    try:
                        await keepalive
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                if listen_key is not None:
                    try:
                        response = await self.http.delete(
                            "/fapi/v1/listenKey", params={"listenKey": listen_key}
                        )
                        response.raise_for_status()
                    except (httpx.HTTPError, OSError):
                        pass

    async def _keepalive(self, listen_key: str, socket: Any) -> None:
        while not self._stopped:
            await asyncio.sleep(30 * 60)
            try:
                response = await self.http.put(
                    "/fapi/v1/listenKey", params={"listenKey": listen_key}
                )
                response.raise_for_status()
            except Exception:
                await socket.close()
                raise
