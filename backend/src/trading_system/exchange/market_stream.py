from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets


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
