from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from trading_system.config import Settings
from trading_system.domain.enums import OrderStatus, PositionSide
from trading_system.domain.models import (
    AccountState,
    Candle,
    ExchangeFilters,
    ExecutionIntent,
    OrderState,
    PositionState,
    UniverseSymbol,
)
from trading_system.exchange.base import (
    ExchangeError,
    ExchangeGateway,
    ExchangeUnknownStatusError,
)

logger = logging.getLogger("trading-worker.binance")


class BinanceUSDMarketClient(ExchangeGateway):
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = settings.binance_api_key or ""
        self.api_secret = settings.binance_api_secret or ""
        self.base_url = settings.binance_base_url
        self.time_offset_ms = 0
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=15,
            transport=transport,
            headers={"X-MBX-APIKEY": self.api_key},
            proxy=settings.binance_http_proxy_url,
            trust_env=False,
        )
        self._filter_cache: dict[str, ExchangeFilters] = {}
        self._leverage_cache: dict[str, int] = {}
        self.last_income_ledger: list[dict[str, object]] = []
        self._income_cached_at = 0.0
        self._all_algo_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._cancel_first_stop_keys: set[tuple[str, str]] = set()
        # Binance weighs signed endpoints aggressively.  The worker has both
        # a periodic strategy cycle and a protection monitor, so allow those
        # callers to share a small serialized request gate instead of
        # bursting position/order reads concurrently.  A server-side 418/429
        # response also opens a short circuit so every background loop does
        # not immediately repeat the banned request.
        self._request_gate = asyncio.Lock()
        self._last_request_at = 0.0
        self._request_min_interval = 0.20
        self._request_backoff_until = 0.0
        self._request_backoff_reason = ""
        self._positions_cache: tuple[float, list[PositionState]] | None = None
        self._positions_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    async def close(self) -> None:
        await self.http.aclose()

    async def health_check(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "Binance credentials not configured"
        if self.settings.binance_proxy_enabled and not self.settings.binance_http_proxy_configured:
            return False, self.settings.binance_http_proxy_detail
        try:
            await self._sync_time()
            # The timestamp is signed with the measured server offset.  A
            # proxy can add roughly 1 second of asymmetric latency even when
            # the host clock is synchronized.  Binance validates the final
            # timestamp against recvWindow, so reject only offsets that leave
            # less than a one-second safety margin instead of treating a
            # harmless proxy offset as an unavailable exchange.
            drift_limit_ms = max(500, self.settings.binance_recv_window_ms - 1_000)
            if abs(self.time_offset_ms) > drift_limit_ms:
                return False, f"clock drift {self.time_offset_ms}ms exceeds limit"
            account, position_mode, multi_assets, positions = await asyncio.gather(
                self._request("GET", "/fapi/v2/account", signed=True),
                self._request("GET", "/fapi/v1/positionSide/dual", signed=True),
                self._request("GET", "/fapi/v1/multiAssetsMargin", signed=True),
                self._request("GET", "/fapi/v2/positionRisk", signed=True),
            )
            if account.get("canTrade") is not True:
                return False, "API key does not have futures trading permission"
            if str(position_mode.get("dualSidePosition", "")).lower() != "true":
                return False, "Binance account is not in hedge position mode"
            if str(multi_assets.get("multiAssetsMargin", "")).lower() == "true":
                return False, "Binance account is in multi-assets margin mode"
            for position in positions:
                if Decimal(position.get("positionAmt", "0")) == 0:
                    continue
                if position.get("marginType") != "isolated":
                    return False, f"{position['symbol']} is not isolated"
                if int(position.get("leverage", "999")) > self.settings.max_leverage:
                    return (
                        False,
                        f"{position['symbol']} leverage exceeds configured "
                        f"{self.settings.max_leverage}x maximum",
                    )
            return True, "time, permissions, hedge mode, and isolated positions healthy"
        except httpx.HTTPError:
            detail = (
                "Binance connection failed through HTTP proxy"
                if self.settings.binance_http_proxy_configured
                else "Binance connection failed"
            )
            return False, detail
        except (ExchangeError, KeyError, ValueError) as error:
            return False, str(error)

    async def get_account_state(self) -> AccountState:
        body = await self._request("GET", "/fapi/v2/account", signed=True)
        equity = Decimal(body["totalMarginBalance"])
        local_now = datetime.now(ZoneInfo(self.settings.app_timezone))
        start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        start_ms = int(start_of_day.timestamp() * 1000)
        now = time.monotonic()
        if now - self._income_cached_at < 5:
            income_rows = self.last_income_ledger
        else:
            income_rows = await self.get_income_history(start_ms)
            self._income_cached_at = now
        self.last_income_ledger = income_rows
        realized = sum(
            (
                Decimal(str(row["income"]))
                for row in income_rows
                if row["income_type"] == "REALIZED_PNL"
            ),
            Decimal("0"),
        )
        fees = sum(
            (
                abs(Decimal(str(row["income"])))
                for row in income_rows
                if row["income_type"] == "COMMISSION"
            ),
            Decimal("0"),
        )
        funding = sum(
            (
                Decimal(str(row["income"]))
                for row in income_rows
                if row["income_type"] == "FUNDING_FEE"
            ),
            Decimal("0"),
        )
        return AccountState(
            equity=equity,
            available_balance=Decimal(body["availableBalance"]),
            realized_pnl_today=realized,
            unrealized_pnl=Decimal(body["totalUnrealizedProfit"]),
            fees_today=fees,
            funding_today=funding,
            day_start_equity=equity,
            high_water_mark=equity,
            total_margin_used=Decimal(body["totalInitialMargin"]),
        )

    async def get_income_history(
        self, start_ms: int, end_ms: int | None = None, limit: int = 1_000
    ) -> list[dict[str, object]]:
        if limit < 1 or limit > 1_000:
            raise ExchangeError("income history limit must be between 1 and 1000")
        cursor_ms = start_ms
        rows: list[dict[str, Any]] = []
        seen_batches: set[str] = set()
        while True:
            params: dict[str, Any] = {
                "startTime": cursor_ms,
                "limit": limit,
            }
            if end_ms is not None:
                params["endTime"] = end_ms
            batch = await self._request("GET", "/fapi/v1/income", params, signed=True)
            if not isinstance(batch, list):
                break
            batch_rows = [row for row in batch if isinstance(row, dict)]
            batch_ids = {
                f"{row.get('tranId', '')}:{row.get('incomeType', '')}:"
                f"{row.get('time', '')}:{row.get('symbol', '')}"
                for row in batch_rows
            }
            if batch_ids and batch_ids.issubset(seen_batches):
                break
            seen_batches.update(batch_ids)
            rows.extend(batch_rows)
            if len(batch) < limit:
                break
            timestamps = [int(row.get("time", 0)) for row in batch_rows]
            if not timestamps:
                break
            next_cursor = max(timestamps) + 1
            if next_cursor <= cursor_ms or (end_ms is not None and next_cursor > end_ms):
                break
            cursor_ms = next_cursor
            if len(seen_batches) > 100_000:
                raise ExchangeError("income history pagination exceeded safety limit")
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in rows:
            income_type = str(row.get("incomeType", ""))
            event_time = datetime.fromtimestamp(int(row.get("time", 0)) / 1000, tz=UTC)
            income_id = (
                f"{row.get('tranId', '')}:{row.get('incomeType', '')}:"
                f"{row.get('time', '')}:{row.get('symbol', '')}"
            )
            if income_id in seen:
                continue
            seen.add(income_id)
            result.append(
                {
                    "income_id": income_id,
                    "symbol": str(row.get("symbol", "")),
                    "income_type": income_type,
                    "income": str(row.get("income", "0")),
                    "asset": str(row.get("asset", "USDT")),
                    "trade_id": str(row["tradeId"]) if row.get("tradeId") else None,
                    "event_time": event_time,
                    "payload": dict(row),
                }
            )
        return result

    async def get_positions(self) -> list[PositionState]:
        now = time.monotonic()
        if self._positions_cache is not None and now - self._positions_cache[0] < 8:
            return [item.model_copy(deep=True) for item in self._positions_cache[1]]
        async with self._positions_lock:
            now = time.monotonic()
            if self._positions_cache is not None and now - self._positions_cache[0] < 8:
                return [item.model_copy(deep=True) for item in self._positions_cache[1]]
            rows = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        symbols = sorted(
            {
                str(row.get("symbol"))
                for row in rows
                if isinstance(row, dict)
                and Decimal(str(row.get("positionAmt", "0"))) != 0
            }
        )
        algo_batches = await asyncio.gather(
            *(
                self._request(
                    "GET",
                    "/fapi/v1/openAlgoOrders",
                    {"symbol": symbol},
                    signed=True,
                )
                for symbol in symbols
            )
        )
        open_algo_orders: list[dict[str, Any]] = []
        for batch in algo_batches:
            open_algo_orders.extend(self._algo_orders(batch))
        protection_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        take_profit_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for order in self._algo_orders(open_algo_orders):
            order_type = str(order.get("orderType") or order.get("type") or "")
            client_id = self._algo_client_id(order)
            if (
                not client_id.startswith("frc_")
                or not self._algo_active(order)
                or order.get("positionSide") not in {"LONG", "SHORT"}
            ):
                continue
            trigger = Decimal(
                str(order.get("triggerPrice") or order.get("stopPrice") or "0")
            )
            if trigger <= 0:
                continue
            key = (str(order["symbol"]), str(order["positionSide"]))
            if (
                order_type == "STOP_MARKET"
                and str(order.get("closePosition", "")).lower() == "true"
            ):
                protection_by_key.setdefault(key, []).append(order)
            elif order_type in {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}:
                take_profit_by_key.setdefault(key, []).append(order)
        positions: list[PositionState] = []
        for row in rows:
            quantity = abs(Decimal(row["positionAmt"]))
            if quantity == 0 or row.get("positionSide") not in {"LONG", "SHORT"}:
                continue
            entry = Decimal(row["entryPrice"])
            mark = Decimal(row["markPrice"])
            side = PositionSide(row["positionSide"])
            protections = protection_by_key.get((row["symbol"], side.value), [])
            protected = len(protections) == 1
            stop = (
                Decimal(str(protections[0].get("triggerPrice") or protections[0].get("stopPrice")))
                if protected
                else entry
            )
            take_profits = sorted(
                {
                    Decimal(
                        str(
                            order.get("triggerPrice")
                            or order.get("stopPrice")
                            or "0"
                        )
                    )
                    for order in take_profit_by_key.get((row["symbol"], side.value), [])
                },
                reverse=side == PositionSide.SHORT,
            )
            tp1 = take_profits[0] if take_profits else None
            tp2 = take_profits[1] if len(take_profits) > 1 else tp1
            risk_per_unit = max(Decimal("0.00000001"), abs(entry - stop))
            direction = Decimal("1") if side == PositionSide.LONG else Decimal("-1")
            positions.append(
                PositionState(
                    position_id=f"binance-{row['symbol']}-{side.value}",
                    symbol=row["symbol"],
                    side=side,
                    quantity=quantity,
                    initial_quantity=quantity,
                    entry_price=entry,
                    mark_price=mark,
                    stop_price=stop,
                    tp1_price=tp1,
                    tp2_price=tp2,
                    original_stop_price=stop,
                    initial_risk_usdt=quantity * risk_per_unit,
                    unrealized_pnl=Decimal(row["unRealizedProfit"]),
                    margin_used=Decimal(row.get("isolatedWallet", "0")),
                    current_r=(mark - entry) * direction / risk_per_unit,
                    protected=protected,
                )
            )
        self._positions_cache = (
            time.monotonic(),
            [item.model_copy(deep=True) for item in positions],
        )
        return positions

    async def cancel_orphan_protection_orders(
        self, active_positions: set[tuple[str, str]]
    ) -> int:
        """Remove managed Algo protections that no longer have an exchange position."""
        orders = await self._all_open_algo_orders()
        canceled = 0
        for order in orders:
            client_id = self._algo_client_id(order)
            key = (str(order.get("symbol", "")), str(order.get("positionSide", "")))
            order_type = str(order.get("orderType") or order.get("type") or "")
            if (
                client_id.startswith("frc_")
                and key not in active_positions
                and order_type in {"STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}
                and self._algo_active(order)
            ):
                await self._cancel_algo_order(key[0], order)
                canceled += 1
        return canceled

    async def _all_open_algo_orders(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._all_algo_cache is not None and now - self._all_algo_cache[0] < 15:
            return list(self._all_algo_cache[1])
        orders = self._algo_orders(
            await self._request("GET", "/fapi/v1/openAlgoOrders", signed=True)
        )
        self._all_algo_cache = (now, orders)
        return list(orders)

    async def get_universe(self, limit: int) -> list[UniverseSymbol]:
        exchange_info, tickers, books, premiums = await asyncio.gather(
            self._request("GET", "/fapi/v1/exchangeInfo"),
            self._request("GET", "/fapi/v1/ticker/24hr"),
            self._request("GET", "/fapi/v1/ticker/bookTicker"),
            self._request("GET", "/fapi/v1/premiumIndex"),
        )
        ticker_map = {row["symbol"]: row for row in tickers}
        book_map = {row["symbol"]: row for row in books}
        premium_map = {row["symbol"]: row for row in premiums}
        now_ms = int(time.time() * 1000)
        rows: list[UniverseSymbol] = []
        for symbol_info in exchange_info["symbols"]:
            symbol = symbol_info["symbol"]
            if (
                # Testnet occasionally exposes non-standard placeholder symbols.
                # Reject them before constructing the typed domain object so one
                # malformed exchange row cannot abort the whole 15-minute cycle.
                not symbol.isascii()
                or not symbol.isalnum()
                or symbol != symbol.upper()
                or not 5 <= len(symbol) <= 20
                or
                symbol_info.get("quoteAsset") != "USDT"
                or symbol_info.get("contractType") != "PERPETUAL"
                or symbol_info.get("status") != "TRADING"
                or symbol not in ticker_map
                or symbol not in book_map
                or symbol not in premium_map
            ):
                continue
            delivery_date = int(symbol_info.get("deliveryDate", 4_133_404_800_000))
            if delivery_date < now_ms + 7 * 86_400_000:
                continue
            book = book_map[symbol]
            premium = premium_map[symbol]
            if Decimal(book["bidPrice"]) <= 0 or Decimal(book["askPrice"]) <= 0:
                continue
            rows.append(
                UniverseSymbol(
                    symbol=symbol,
                    status=symbol_info["status"],
                    listing_days=max(
                        0, (now_ms - int(symbol_info.get("onboardDate", now_ms))) // 86_400_000
                    ),
                    quote_volume_24h=Decimal(ticker_map[symbol]["quoteVolume"]),
                    best_bid=Decimal(book["bidPrice"]),
                    best_ask=Decimal(book["askPrice"]),
                    mark_price=Decimal(premium["markPrice"]),
                    index_price=Decimal(premium["indexPrice"]),
                    funding_rate=Decimal(premium["lastFundingRate"]),
                )
            )
        rows.sort(key=lambda item: item.quote_volume_24h, reverse=True)
        return rows if limit <= 0 else rows[:limit]

    async def get_open_interest(self, symbol: str) -> Decimal:
        body = await self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol})
        return Decimal(body["openInterest"])

    async def get_book_depth(self, symbol: str, limit: int = 20) -> Decimal:
        body = await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        bids = sum(
            (Decimal(price) * Decimal(quantity) for price, quantity in body.get("bids", [])),
            Decimal("0"),
        )
        asks = sum(
            (Decimal(price) * Decimal(quantity) for price, quantity in body.get("asks", [])),
            Decimal("0"),
        )
        return min(bids, asks)

    async def get_filters(self, symbol: str) -> ExchangeFilters:
        if symbol in self._filter_cache:
            return self._filter_cache[symbol]
        body = await self._request("GET", "/fapi/v1/exchangeInfo")
        row = next((item for item in body["symbols"] if item["symbol"] == symbol), None)
        if row is None:
            raise ExchangeError(f"unknown symbol {symbol}")
        filters = {item["filterType"]: item for item in row["filters"]}
        lot = filters.get("LOT_SIZE") or filters["MARKET_LOT_SIZE"]
        market_lot = filters.get("MARKET_LOT_SIZE") or lot
        price = filters["PRICE_FILTER"]
        minimum = filters.get("MIN_NOTIONAL", {})
        result = ExchangeFilters(
            tick_size=Decimal(price["tickSize"]),
            step_size=Decimal(lot["stepSize"]),
            min_quantity=Decimal(lot["minQty"]),
            max_quantity=Decimal(lot["maxQty"]) if lot.get("maxQty") else None,
            min_notional=Decimal(minimum.get("notional", "5")),
            market_step_size=Decimal(market_lot["stepSize"]),
            market_min_quantity=Decimal(market_lot.get("minQty", lot["minQty"])),
            market_max_quantity=(
                Decimal(market_lot["maxQty"]) if market_lot.get("maxQty") else None
            ),
        )
        self._filter_cache[symbol] = result
        return result

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[Candle]:
        rows = await self._request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        now = datetime.now(UTC)
        return [
            Candle(
                open_time=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                close_time=datetime.fromtimestamp(row[6] / 1000, tz=UTC),
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
            )
            for row in rows
            if datetime.fromtimestamp(row[6] / 1000, tz=UTC) <= now
        ]

    async def get_historical_klines(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[Candle]:
        candles: list[Candle] = []
        cursor = start_ms
        while cursor < end_ms:
            rows = await self._request(
                "GET",
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1500,
                },
            )
            if not rows:
                break
            batch = [
                Candle(
                    open_time=datetime.fromtimestamp(row[0] / 1000, tz=UTC),
                    close_time=datetime.fromtimestamp(row[6] / 1000, tz=UTC),
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[5]),
                )
                for row in rows
            ]
            candles.extend(batch)
            next_cursor = int(rows[-1][6]) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return candles

    async def get_historical_funding_rates(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> dict[datetime, Decimal]:
        rates: dict[datetime, Decimal] = {}
        cursor = start_ms
        while cursor < end_ms:
            rows = await self._request(
                "GET",
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not rows:
                break
            for row in rows:
                funding_time = int(row["fundingTime"])
                if start_ms <= funding_time < end_ms:
                    rates[datetime.fromtimestamp(funding_time / 1000, tz=UTC)] = Decimal(
                        row["fundingRate"]
                    )
            next_cursor = int(rows[-1]["fundingTime"]) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(rows) < 1000:
                break
        return rates

    async def configure_symbol(self, symbol: str, leverage: int) -> None:
        if leverage < 1 or leverage > 30:
            raise ExchangeError("leverage exceeds hard maximum")
        try:
            await self._request(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": "ISOLATED"},
                signed=True,
            )
        except ExchangeError as error:
            if error.code == -4046:
                # Binance reports this when the symbol is already isolated.
                pass
            elif error.code == -4067:
                # On testnet, changing margin type while managed protection
                # orders are open can return -4067 even when the symbol is
                # already isolated. Verify the exchange state before treating
                # this as an idempotent success; never suppress it for a
                # crossed-margin symbol.
                try:
                    current_margin_type = await self._symbol_margin_type(symbol)
                except ExchangeError:
                    raise error from None
                if current_margin_type != "isolated":
                    raise
                logger.info(
                    "binance margin type already isolated; ignored open-order"
                    " conflict symbol=%s code=%s",
                    symbol,
                    error.code,
                )
            else:
                raise
        try:
            await self._request(
                "POST",
                "/fapi/v1/leverage",
                {"symbol": symbol, "leverage": leverage},
                signed=True,
            )
        except ExchangeError as error:
            # Binance symbols can impose a lower leverage ceiling than the
            # global console setting.  Resolve that symbol-specific ceiling
            # and retry only when Binance explicitly rejected the leverage
            # value; never turn an unrelated exchange failure into a retry.
            message = str(error).lower()
            if error.http_status != 400 or "leverage" not in message:
                raise
            maximum = await self._symbol_max_leverage(symbol)
            if maximum is None or maximum >= leverage:
                raise
            logger.warning(
                "binance symbol leverage capped symbol=%s requested=%d effective=%d",
                symbol,
                leverage,
                maximum,
            )
            await self._request(
                "POST",
                "/fapi/v1/leverage",
                {"symbol": symbol, "leverage": maximum},
                signed=True,
            )

    async def _symbol_margin_type(self, symbol: str) -> str | None:
        """Read a symbol's margin mode without disturbing open protections."""
        rows = await self._request(
            "GET",
            "/fapi/v2/positionRisk",
            {"symbol": symbol},
            signed=True,
        )
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and row.get("symbol") == symbol:
                value = row.get("marginType")
                return str(value).lower() if value is not None else None
        return None

    async def _symbol_max_leverage(self, symbol: str) -> int | None:
        cached = self._leverage_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            body = await self._request(
                "GET", "/fapi/v1/leverageBracket", {"symbol": symbol}, signed=True
            )
        except ExchangeError:
            logger.exception("unable to resolve Binance leverage bracket symbol=%s", symbol)
            return None
        rows = body if isinstance(body, list) else []
        if not rows or not isinstance(rows[0], dict):
            return None
        brackets = rows[0].get("brackets", [])
        levels = [
            int(item["initialLeverage"])
            for item in brackets
            if isinstance(item, dict) and str(item.get("initialLeverage", "")).isdigit()
        ]
        if not levels:
            return None
        maximum = max(levels)
        self._leverage_cache[symbol] = maximum
        return maximum

    async def best_entry_price(self, symbol: str, side: str) -> Decimal:
        body = await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        # Use the marketable side of the spread.  A BUY at the best ask and a
        # SELL at the best bid can fill immediately while remaining a bounded
        # LIMIT order; the caller still applies the model-approved entry
        # interval before submitting it.  The old passive-side quote (BUY at
        # bid / SELL at ask) routinely sat unfilled until the 30-second
        # reprice deadline, making approved portfolio entries look like
        # strategy failures.
        return Decimal(body["askPrice"] if side == "BUY" else body["bidPrice"])

    async def get_mark_price(self, symbol: str) -> Decimal:
        body = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        return Decimal(body["markPrice"])

    async def place_limit_entry(
        self, intent: ExecutionIntent, client_order_id: str, price: Decimal
    ) -> OrderState:
        side = "BUY" if intent.side == PositionSide.LONG else "SELL"
        parameters = {
            "symbol": intent.symbol,
            "side": side,
            "positionSide": intent.side.value,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": self._fmt(intent.quantity),
            "price": self._fmt(price),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        try:
            body = await self._request("POST", "/fapi/v1/order", parameters, signed=True)
        except ExchangeError as error:
            if self._is_ambiguous_write_error(error):
                return await self._resolve_ambiguous_order(
                    intent.symbol, client_order_id, error
                )
            if "duplicate" not in str(error).lower():
                raise
            body = await self._request(
                "GET",
                "/fapi/v1/order",
                {"symbol": intent.symbol, "origClientOrderId": client_order_id},
                signed=True,
            )
        self._invalidate_position_cache()
        return self._order_state(body)

    async def get_order(self, symbol: str, client_order_id: str) -> OrderState:
        body = await self._request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )
        return self._order_state(body)

    async def cancel_order(self, symbol: str, client_order_id: str) -> OrderState:
        body = await self._request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
        )
        self._invalidate_position_cache()
        return self._order_state(body)

    async def upsert_protection(
        self, intent: ExecutionIntent, filled_quantity: Decimal, average_price: Decimal
    ) -> list[OrderState]:
        filters = await self.get_filters(intent.symbol)
        q1 = self._round_quantity(filled_quantity * Decimal("0.4"), filters.step_size)
        q2 = self._round_quantity(filled_quantity * Decimal("0.4"), filters.step_size)
        close_side = "SELL" if intent.side == PositionSide.LONG else "BUY"
        tp1_price = self._tp1_for_fill(intent, average_price)
        existing = self._algo_orders(
            await self._request(
                "GET", "/fapi/v1/openAlgoOrders", {"symbol": intent.symbol}, signed=True
            )
        )
        stop_suffix = hashlib.sha256(
            f"{intent.intent_id}:{self._fmt(intent.stop_price)}".encode()
        ).hexdigest()[:12]
        tranche_suffix = hashlib.sha256(
            f"{intent.intent_id}:{self._fmt(filled_quantity)}".encode()
        ).hexdigest()[:12]
        specs: list[tuple[str, str, Decimal, bool]] = [
            (f"frc_{stop_suffix}_sl", "STOP_MARKET", intent.stop_price, True),
        ]
        if q1 >= filters.min_quantity:
            specs.append(
                (f"frc_{tranche_suffix}_t1", "TAKE_PROFIT_MARKET", tp1_price, False)
            )
        if q2 >= filters.min_quantity:
            specs.append(
                (f"frc_{tranche_suffix}_t2", "TAKE_PROFIT_MARKET", intent.tp2_price, False)
            )
        desired_ids = {client_id for client_id, *_ in specs}
        orders: list[OrderState] = []
        for client_id, order_type, trigger, close_position in specs:
            current = next(
                (
                    order
                    for order in existing
                    if self._algo_client_id(order) == client_id and self._algo_active(order)
                ),
                None,
            )
            quantity = Decimal("0") if close_position else (q1 if client_id.endswith("_t1") else q2)
            if current is not None and self._algo_matches(
                current,
                symbol=intent.symbol,
                side=close_side,
                position_side=intent.side.value,
                order_type=order_type,
                trigger=trigger,
                quantity=quantity,
                close_position=close_position,
            ):
                orders.append(self._algo_order_state(current))
                continue
            if current is not None:
                await self._cancel_algo_order(intent.symbol, current)
            orders.append(
                await self._submit_algo_order(
                    {
                        "symbol": intent.symbol,
                        "side": close_side,
                        "positionSide": intent.side.value,
                        "algoType": "CONDITIONAL",
                        "type": order_type,
                        "triggerPrice": self._fmt(trigger),
                        "workingType": "MARK_PRICE",
                        "priceProtect": (
                            "TRUE" if self.settings.binance_stop_price_protect else "FALSE"
                        ),
                        "clientAlgoId": client_id,
                        **(
                            {"closePosition": "true"}
                            if close_position
                            else {"quantity": self._fmt(quantity)}
                        ),
                    }
                )
            )
        for order in existing:
            if (
                order.get("positionSide") == intent.side.value
                and str(order.get("orderType") or order.get("type") or "")
                in {"STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}
                and self._algo_client_id(order).startswith("frc_")
                and self._algo_active(order)
                and self._algo_client_id(order) not in desired_ids
            ):
                await self._cancel_algo_order(intent.symbol, order)
        final = self._algo_orders(
            await self._request(
                "GET", "/fapi/v1/openAlgoOrders", {"symbol": intent.symbol}, signed=True
            )
        )
        active_stops = [
            order
            for order in final
            if order.get("positionSide") == intent.side.value
            and str(order.get("orderType") or order.get("type") or "") == "STOP_MARKET"
            and self._algo_client_id(order).startswith("frc_")
            and self._algo_active(order)
        ]
        if len(active_stops) != 1 or self._algo_client_id(active_stops[0]) != specs[0][0]:
            raise ExchangeError(
                "protection invariant violated: expected exactly one active hard stop"
            )
        self._invalidate_position_cache()
        return orders

    @staticmethod
    def _tp1_for_fill(intent: ExecutionIntent, average_price: Decimal) -> Decimal:
        """Derive the 1R tranche from the actual weighted fill price.

        A limit entry may fill anywhere inside the approved interval.  Using a
        fixed interval anchor for TP1 can make the first tranche immediately
        trigger when the fill is near the opposite edge.  The hard stop and
        model-provided TP2 remain unchanged; only the mechanical 1R target
        follows the real fill price.
        """

        if average_price <= 0:
            return intent.tp1_price
        risk_distance = abs(average_price - intent.stop_price)
        if risk_distance <= 0:
            return intent.tp1_price
        if intent.side == PositionSide.LONG:
            candidate = average_price + risk_distance
            return candidate if candidate < intent.tp2_price else intent.tp1_price
        candidate = average_price - risk_distance
        return candidate if candidate > intent.tp2_price else intent.tp1_price

    async def close_position_market(self, position: PositionState, reason: str) -> OrderState:
        return await self.close_position_quantity_market(position, position.quantity, reason)

    async def close_position_market_orders(
        self, position: PositionState, reason: str
    ) -> list[OrderState]:
        return await self.close_position_quantity_market_orders(
            position, position.quantity, reason
        )

    async def place_limit_exit(
        self,
        position: PositionState,
        quantity: Decimal,
        price: Decimal,
        operation_id: str,
    ) -> OrderState:
        filters = await self.get_filters(position.symbol)
        quantity = self._round_quantity(min(quantity, position.quantity), filters.step_size)
        if quantity < filters.min_quantity:
            raise ExchangeError("reduce quantity is below the exchange minimum")
        side = "SELL" if position.side == PositionSide.LONG else "BUY"
        digest = hashlib.sha256(
            f"{position.position_id}:{operation_id}:{self._fmt(quantity)}:{self._fmt(price)}".encode()
        ).hexdigest()[:16]
        client_order_id = f"frc_exit_{digest}"
        parameters = {
            "symbol": position.symbol,
            "side": side,
            "positionSide": position.side.value,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": self._fmt(quantity),
            "price": self._fmt(price),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        try:
            body = await self._request("POST", "/fapi/v1/order", parameters, signed=True)
        except ExchangeError as error:
            # Even a rejected exit can race with an exchange-side stop/TP that
            # flattened the position just before this request.  Do not let a
            # cached position survive that write failure; the exit manager
            # needs a fresh read to classify Binance's narrow "no open
            # position" response as an idempotent no-op.
            self._invalidate_position_cache()
            if self._is_ambiguous_write_error(error):
                return await self._resolve_ambiguous_order(
                    position.symbol, client_order_id, error
                )
            if "duplicate" not in str(error).lower():
                raise
            body = await self._request(
                "GET",
                "/fapi/v1/order",
                {"symbol": position.symbol, "origClientOrderId": client_order_id},
                signed=True,
            )
        self._invalidate_position_cache()
        return self._order_state(body)

    async def close_position_quantity_market(
        self,
        position: PositionState,
        quantity: Decimal,
        operation_id: str,
    ) -> OrderState:
        orders = await self.close_position_quantity_market_orders(
            position, quantity, operation_id
        )
        return orders[-1]

    async def close_position_quantity_market_orders(
        self,
        position: PositionState,
        quantity: Decimal,
        operation_id: str,
    ) -> list[OrderState]:
        filters = await self.get_filters(position.symbol)
        market_step = filters.market_step_size or filters.step_size
        market_min = filters.market_min_quantity or filters.min_quantity
        quantity = self._round_quantity(min(quantity, position.quantity), market_step)
        if quantity < market_min:
            raise ExchangeError("reduce quantity is below the exchange minimum")
        side = "SELL" if position.side == PositionSide.LONG else "BUY"
        max_chunk = filters.market_max_quantity or quantity
        remaining = quantity
        part = 0
        orders: list[OrderState] = []
        while remaining >= market_min:
            chunk = self._round_quantity(min(remaining, max_chunk), market_step)
            if chunk < market_min:
                break
            digest = hashlib.sha256(
                f"{position.position_id}:{operation_id}:{self._fmt(quantity)}:{part}".encode()
            ).hexdigest()[:16]
            client_order_id = f"frc_close_{digest}"
            parameters = {
                "symbol": position.symbol,
                "side": side,
                "positionSide": position.side.value,
                "type": "MARKET",
                "quantity": self._fmt(chunk),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "RESULT",
            }
            try:
                body = await self._request("POST", "/fapi/v1/order", parameters, signed=True)
            except ExchangeError as error:
                if self._is_ambiguous_write_error(error):
                    state = await self._resolve_ambiguous_order(
                        position.symbol, client_order_id, error
                    )
                    orders.append(state)
                    self._invalidate_position_cache()
                    filled = min(chunk, state.filled_quantity)
                    if filled <= 0:
                        raise ExchangeError(
                            f"market close order {client_order_id} returned no fill"
                        ) from error
                    remaining -= filled
                    part += 1
                    continue
                if error.code not in {-2010, -4116} and "duplicate" not in str(error).lower():
                    raise
                body = await self._request(
                    "GET",
                    "/fapi/v1/order",
                    {"symbol": position.symbol, "origClientOrderId": client_order_id},
                    signed=True,
                )
            state = self._order_state(body)
            orders.append(state)
            self._invalidate_position_cache()
            filled = min(chunk, state.filled_quantity)
            if filled <= 0:
                raise ExchangeError(
                    f"market close order {client_order_id} returned no fill"
                )
            remaining -= filled
            part += 1
        if not orders:
            raise ExchangeError("market close quantity is below the exchange minimum")
        return orders

    async def cancel_position_take_profits(self, position: PositionState) -> None:
        orders = self._algo_orders(
            await self._request(
                "GET", "/fapi/v1/openAlgoOrders", {"symbol": position.symbol}, signed=True
            )
        )
        for order in orders:
            if (
                order.get("positionSide") == position.side.value
                and str(order.get("orderType") or order.get("type") or "").startswith("TAKE_PROFIT")
                and self._algo_client_id(order).startswith("frc_")
                and self._algo_active(order)
            ):
                await self._cancel_algo_order(position.symbol, order)

    async def cancel_position_protection(self, position: PositionState) -> None:
        orders = self._algo_orders(
            await self._request(
                "GET", "/fapi/v1/openAlgoOrders", {"symbol": position.symbol}, signed=True
            )
        )
        for order in orders:
            if (
                order.get("positionSide") == position.side.value
                and self._algo_client_id(order).startswith("frc_")
                and self._algo_active(order)
            ):
                await self._cancel_algo_order(position.symbol, order)

    async def tighten_stop(self, position: PositionState, new_stop: Decimal) -> OrderState:
        filters = await self.get_filters(position.symbol)
        rounding = ROUND_DOWN if position.side == PositionSide.LONG else ROUND_UP
        new_stop = (new_stop / filters.tick_size).to_integral_value(
            rounding=rounding
        ) * filters.tick_size
        if position.side == PositionSide.LONG and new_stop <= position.stop_price:
            raise ExchangeError("new long stop does not tighten risk")
        if position.side == PositionSide.SHORT and new_stop >= position.stop_price:
            raise ExchangeError("new short stop does not tighten risk")
        mark_price = await self.get_mark_price(position.symbol)
        if position.side == PositionSide.LONG and new_stop >= mark_price:
            raise ExchangeError("new long stop is not below mark price")
        if position.side == PositionSide.SHORT and new_stop <= mark_price:
            raise ExchangeError("new short stop is not above mark price")
        existing = self._algo_orders(
            await self._request(
                "GET", "/fapi/v1/openAlgoOrders", {"symbol": position.symbol}, signed=True
            )
        )
        close_side = "SELL" if position.side == PositionSide.LONG else "BUY"
        token = hashlib.sha256(
            f"{position.position_id}:{self._fmt(new_stop)}".encode()
        ).hexdigest()[:14]
        client_id = f"frc_tsl_{token}"
        current = next(
            (
                order
                for order in existing
                if self._algo_client_id(order) == client_id and self._algo_active(order)
            ),
            None,
        )
        current_matches = current is not None and self._algo_matches(
            current,
            symbol=position.symbol,
            side=close_side,
            position_side=position.side.value,
            order_type="STOP_MARKET",
            trigger=new_stop,
            quantity=Decimal("0"),
            close_position=True,
        )
        parameters = self._stop_algo_parameters(
            position, close_side=close_side, stop_price=new_stop, client_id=client_id
        )
        canceled_stop_ids: set[str] = set()
        if not current_matches:
            if current is not None:
                await self._cancel_algo_order(position.symbol, current)
                canceled_stop_ids.add(self._algo_client_id(current))
            active_stops = [
                order
                for order in existing
                if str(order.get("orderType") or order.get("type") or "") == "STOP_MARKET"
                and order.get("positionSide") == position.side.value
                and self._algo_client_id(order).startswith("frc_")
                and self._algo_active(order)
                and self._algo_client_id(order) != client_id
            ]
            stop_key = (position.symbol, position.side.value)
            if stop_key in self._cancel_first_stop_keys and len(active_stops) == 1:
                old_stop = active_stops[0]
                replacement = await self._cancel_then_create_stop(
                    position,
                    old_stop=old_stop,
                    close_side=close_side,
                    new_stop=new_stop,
                    parameters=parameters,
                    canceled_stop_ids=canceled_stop_ids,
                )
                logger.info(
                    "stop replacement used known cancel-create path symbol=%s side=%s "
                    "previous_stop=%s new_stop=%s",
                    position.symbol,
                    position.side.value,
                    self._algo_trigger(old_stop),
                    new_stop,
                )
            else:
                try:
                    replacement = await self._submit_algo_order(parameters)
                except ExchangeError as error:
                    logger.warning(
                        "create-before-cancel stop replacement rejected symbol=%s side=%s "
                        "current_stop=%s proposed_stop=%s mark_price=%s binance_code=%s "
                        "http_status=%s error=%s",
                        position.symbol,
                        position.side.value,
                        position.stop_price,
                        new_stop,
                        mark_price,
                        error.code,
                        error.http_status,
                        error,
                    )
                    if not self._stop_replace_requires_cancel(error) or len(active_stops) != 1:
                        raise
                    self._cancel_first_stop_keys.add(stop_key)
                    old_stop = active_stops[0]
                    replacement = await self._cancel_then_create_stop(
                        position,
                        old_stop=old_stop,
                        close_side=close_side,
                        new_stop=new_stop,
                        parameters=parameters,
                        canceled_stop_ids=canceled_stop_ids,
                    )
                    logger.info(
                        "stop replacement used cancel-create fallback symbol=%s side=%s "
                        "previous_stop=%s new_stop=%s initial_binance_code=%s",
                        position.symbol,
                        position.side.value,
                        self._algo_trigger(old_stop),
                        new_stop,
                        error.code,
                    )
        else:
            assert current is not None
            replacement = self._algo_order_state(current)
        for order in existing:
            if (
                str(order.get("orderType") or order.get("type") or "") == "STOP_MARKET"
                and order.get("positionSide") == position.side.value
                and self._algo_client_id(order).startswith("frc_")
                and self._algo_client_id(order) != client_id
                and self._algo_client_id(order) not in canceled_stop_ids
                and self._algo_active(order)
            ):
                await self._cancel_algo_order(position.symbol, order)
        final = self._algo_orders(
            await self._request(
                "GET", "/fapi/v1/openAlgoOrders", {"symbol": position.symbol}, signed=True
            )
        )
        active_stops = [
            order
            for order in final
            if order.get("positionSide") == position.side.value
            and str(order.get("orderType") or order.get("type") or "") == "STOP_MARKET"
            and self._algo_client_id(order).startswith("frc_")
            and self._algo_active(order)
        ]
        if len(active_stops) != 1 or self._algo_client_id(active_stops[0]) != client_id:
            raise ExchangeError("stop replacement invariant violated")
        self._invalidate_position_cache()
        return replacement

    async def _cancel_then_create_stop(
        self,
        position: PositionState,
        *,
        old_stop: dict[str, Any],
        close_side: str,
        new_stop: Decimal,
        parameters: dict[str, Any],
        canceled_stop_ids: set[str],
    ) -> OrderState:
        await self._cancel_algo_order(position.symbol, old_stop)
        canceled_stop_ids.add(self._algo_client_id(old_stop))
        try:
            refreshed_mark = await self.get_mark_price(position.symbol)
            if (
                position.side == PositionSide.LONG and new_stop >= refreshed_mark
            ) or (
                position.side == PositionSide.SHORT and new_stop <= refreshed_mark
            ):
                raise ExchangeError(
                    "replacement stop would trigger immediately after old stop cancellation",
                    code=-2021,
                )
            return await self._submit_algo_order(parameters)
        except Exception as replacement_error:
            try:
                await self._restore_stop(position, old_stop, close_side)
            except Exception as rollback_error:
                logger.critical(
                    "stop replacement rollback failed symbol=%s side=%s old_stop=%s "
                    "proposed_stop=%s replacement_error=%s rollback_error=%s",
                    position.symbol,
                    position.side.value,
                    self._algo_trigger(old_stop),
                    new_stop,
                    replacement_error,
                    rollback_error,
                )
                raise ExchangeError(
                    "stop replacement failed and rollback could not restore hard stop"
                ) from rollback_error
            logger.warning(
                "stop replacement failed after cancel; previous hard stop restored "
                "symbol=%s side=%s restored_stop=%s error=%s",
                position.symbol,
                position.side.value,
                self._algo_trigger(old_stop),
                replacement_error,
            )
            raise

    def _stop_algo_parameters(
        self,
        position: PositionState,
        *,
        close_side: str,
        stop_price: Decimal,
        client_id: str,
    ) -> dict[str, Any]:
        return {
            "symbol": position.symbol,
            "side": close_side,
            "positionSide": position.side.value,
            "algoType": "CONDITIONAL",
            "type": "STOP_MARKET",
            "triggerPrice": self._fmt(stop_price),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "TRUE" if self.settings.binance_stop_price_protect else "FALSE",
            "clientAlgoId": client_id,
        }

    async def _restore_stop(
        self, position: PositionState, old_stop: dict[str, Any], close_side: str
    ) -> OrderState:
        stop_price = self._algo_trigger(old_stop)
        rollback_token = hashlib.sha256(
            f"rollback:{position.position_id}:{self._fmt(stop_price)}:{time.time_ns()}".encode()
        ).hexdigest()[:14]
        return await self._submit_algo_order(
            self._stop_algo_parameters(
                position,
                close_side=close_side,
                stop_price=stop_price,
                client_id=f"frc_rb_{rollback_token}",
            )
        )

    @staticmethod
    def _algo_trigger(order: dict[str, Any]) -> Decimal:
        return Decimal(str(order.get("triggerPrice") or order.get("stopPrice") or "0"))

    @staticmethod
    def _stop_replace_requires_cancel(error: ExchangeError) -> bool:
        """Return whether Binance requires cancel-before-create for a close stop.

        Binance has returned the same conflict with both the documented -4130
        code and slightly different message spellings (``closePosition``,
        ``close_position`` and ``CLOSE POSITION``).  Treat only that narrow
        family as recoverable; all other 400 responses still fail closed.
        """
        if error.code == -4130:
            return True
        message = "".join(char.lower() if char.isalnum() else " " for char in str(error))
        compact = message.replace(" ", "")
        close_position = "closeposition" in compact or "closepositionorder" in compact
        conflict = any(token in message for token in ("exist", "already", "conflict", "open stop"))
        return close_position and conflict

    async def cancel_all_entry_orders(self) -> None:
        orders = await self._request("GET", "/fapi/v1/openOrders", signed=True)
        for order in orders:
            if (
                order.get("type") == "LIMIT"
                and order.get("clientOrderId", "").startswith("frc_")
                and order.get("status", "NEW") in {"NEW", "PARTIALLY_FILLED"}
            ):
                await self.cancel_order(order["symbol"], order["clientOrderId"])
        remaining = await self._request("GET", "/fapi/v1/openOrders", signed=True)
        if any(
            order.get("type") == "LIMIT"
            and order.get("clientOrderId", "").startswith("frc_")
            and order.get("status", "NEW") in {"NEW", "PARTIALLY_FILLED"}
            for order in remaining
        ):
            raise ExchangeError("managed entry orders remain after cancellation")

    async def _submit_algo_order(self, parameters: dict[str, Any]) -> OrderState:
        client_algo_id = str(parameters["clientAlgoId"])
        try:
            body = await self._request("POST", "/fapi/v1/algoOrder", parameters, signed=True)
        except ExchangeError as error:
            if self._is_ambiguous_write_error(error):
                return await self._resolve_ambiguous_algo_order(
                    str(parameters["symbol"]), client_algo_id, parameters, error
                )
            if "duplicate" not in str(error).lower() and "already" not in str(error).lower():
                raise
            return await self._get_algo_order(str(parameters["symbol"]), client_algo_id, parameters)
        candidate = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(candidate, dict) or not candidate.get("symbol"):
            return await self._get_algo_order(str(parameters["symbol"]), client_algo_id, parameters)
        self._all_algo_cache = None
        self._invalidate_position_cache()
        return self._algo_order_state(candidate, parameters)

    async def _get_algo_order(
        self, symbol: str, client_algo_id: str, fallback: dict[str, Any] | None = None
    ) -> OrderState:
        body = await self._request(
            "GET",
            "/fapi/v1/algoOrder",
            {"symbol": symbol, "clientAlgoId": client_algo_id},
            signed=True,
        )
        return self._algo_order_state(body, fallback)

    async def _cancel_algo_order(self, symbol: str, order: dict[str, Any]) -> None:
        client_algo_id = self._algo_client_id(order)
        algo_id = order.get("algoId")
        params: dict[str, Any] = {"symbol": symbol}
        if algo_id:
            params["algoId"] = str(algo_id)
        elif client_algo_id:
            params["clientAlgoId"] = client_algo_id
        else:
            raise ExchangeError("cannot cancel algo order without algoId or clientAlgoId")
        try:
            await self._request("DELETE", "/fapi/v1/algoOrder", params, signed=True)
        except ExchangeError as error:
            message = str(error).lower()
            if not any(
                token in message
                for token in ("not found", "unknown", "already canceled", "already cancelled")
            ):
                raise
        self._all_algo_cache = None

    @staticmethod
    def _algo_orders(body: Any) -> list[dict[str, Any]]:
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        if isinstance(body, dict):
            rows = body.get("orders") or body.get("data") or []
            if isinstance(rows, dict):
                rows = [rows]
            return [row for row in rows if isinstance(row, dict)]
        return []

    @staticmethod
    def _algo_client_id(order: dict[str, Any]) -> str:
        return str(order.get("clientAlgoId") or order.get("clientOrderId") or "")

    @classmethod
    def _algo_active(cls, order: dict[str, Any]) -> bool:
        status = str(order.get("algoStatus") or order.get("status") or "NEW").upper()
        return status not in {"CANCELED", "CANCELLED", "EXPIRED", "FINISHED", "REJECTED"}

    @classmethod
    def _algo_matches(
        cls,
        order: dict[str, Any],
        *,
        symbol: str,
        side: str,
        position_side: str,
        order_type: str,
        trigger: Decimal,
        quantity: Decimal,
        close_position: bool,
    ) -> bool:
        actual_type = str(order.get("orderType") or order.get("type") or "")
        actual_trigger = Decimal(str(order.get("triggerPrice") or order.get("stopPrice") or "0"))
        actual_quantity = Decimal(str(order.get("quantity") or order.get("origQty") or "0"))
        actual_close = str(order.get("closePosition", "false")).lower() == "true"
        return (
            order.get("symbol") == symbol
            and order.get("side") == side
            and order.get("positionSide") == position_side
            and actual_type == order_type
            and actual_trigger == trigger
            and actual_quantity == quantity
            and actual_close == close_position
        )

    @classmethod
    def _algo_order_state(
        cls, body: dict[str, Any], fallback: dict[str, Any] | None = None
    ) -> OrderState:
        row = body.get("data", body)
        if not isinstance(row, dict):
            row = {}
        fallback = fallback or {}
        status_map = {
            "NEW": OrderStatus.SUBMITTED,
            "TRIGGER_PENDING": OrderStatus.SUBMITTED,
            "WORKING": OrderStatus.SUBMITTED,
            "EXECUTING": OrderStatus.PARTIALLY_FILLED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "FINISHED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "CANCELLED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }
        status = str(row.get("algoStatus") or row.get("status") or "NEW").upper()
        client_id = str(
            row.get("clientAlgoId")
            or row.get("clientOrderId")
            or fallback.get("clientAlgoId", "")
        )
        symbol = str(row.get("symbol") or fallback.get("symbol", ""))
        side = str(row.get("side") or fallback.get("side", ""))
        position_side = str(row.get("positionSide") or fallback.get("positionSide", ""))
        order_type = str(row.get("orderType") or row.get("type") or fallback.get("type", ""))
        if not client_id or not symbol or position_side not in {"LONG", "SHORT"}:
            raise ExchangeError("Binance Algo response omitted order identity")
        quantity = row.get("quantity") or row.get("origQty") or fallback.get("quantity", "0")
        trigger = row.get("triggerPrice") or row.get("stopPrice") or fallback.get("triggerPrice")
        return OrderState(
            client_order_id=client_id,
            exchange_order_id=str(row.get("algoId") or row.get("orderId") or "") or None,
            symbol=symbol,
            side=side,
            position_side=PositionSide(position_side),
            order_type=order_type,
            quantity=Decimal(str(quantity)),
            stop_price=Decimal(str(trigger)) if trigger is not None else None,
            status=status_map.get(status, OrderStatus.CREATED),
        )

    @staticmethod
    def _is_ambiguous_write_error(error: ExchangeError) -> bool:
        """Return whether Binance may have accepted a write despite the error."""
        return error.http_status in {502, 503, 504} or error.http_status is None

    async def _resolve_ambiguous_order(
        self, symbol: str, client_order_id: str, original: ExchangeError
    ) -> OrderState:
        """Resolve an uncertain order write before allowing another action."""
        last_error: Exception = original
        for delay in (0.0, 0.25, 0.75, 1.5):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.get_order(symbol, client_order_id)
            except ExchangeError as error:
                last_error = error
                if error.code not in {-2013, -2014}:
                    continue
        raise ExchangeUnknownStatusError(
            f"Binance order status remains unknown for {client_order_id}",
            client_order_id=client_order_id,
            code=original.code,
            http_status=original.http_status,
            headers=original.headers,
        ) from last_error

    async def _resolve_ambiguous_algo_order(
        self,
        symbol: str,
        client_algo_id: str,
        fallback: dict[str, Any],
        original: ExchangeError,
    ) -> OrderState:
        last_error: Exception = original
        for delay in (0.0, 0.25, 0.75, 1.5):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._get_algo_order(symbol, client_algo_id, fallback)
            except ExchangeError as error:
                last_error = error
                if error.code not in {-2013, -2014}:
                    continue
        raise ExchangeUnknownStatusError(
            f"Binance Algo order status remains unknown for {client_algo_id}",
            client_order_id=client_algo_id,
            code=original.code,
            http_status=original.http_status,
            headers=original.headers,
        ) from last_error

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        _retry_time_sync: int = 2,
    ) -> Any:
        await self._request_gate_wait()
        if self.settings.binance_proxy_enabled and not self.settings.binance_http_proxy_configured:
            raise ExchangeError(self.settings.binance_http_proxy_detail)
        values = dict(params or {})
        if signed:
            if not self.configured:
                raise ExchangeError("Binance credentials are not configured")
            values["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            values["recvWindow"] = self.settings.binance_recv_window_ms
            query = urlencode(values, doseq=True)
            values["signature"] = hmac.new(
                self.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
        try:
            response = await self.http.request(method, path, params=values)
        except httpx.HTTPError:
            detail = (
                "Binance request failed through HTTP proxy"
                if self.settings.binance_http_proxy_configured
                else "Binance request failed"
            )
            raise ExchangeError(detail) from None
        if response.is_error:
            try:
                payload = response.json()
                detail = payload.get("msg", "Binance API error")
                code = payload.get("code")
            except ValueError:
                detail = "Binance API error"
                code = None
            if response.status_code in {418, 429} or code == -1003:
                retry_after = self._rate_limit_delay(response, detail)
                self._request_backoff_until = max(
                    self._request_backoff_until,
                    time.monotonic() + retry_after,
                )
                self._request_backoff_reason = detail
                raise ExchangeError(
                    f"{response.status_code} [{code}]: {detail}; retry after {retry_after:.0f}s",
                    code=int(code) if isinstance(code, int) else -1003,
                    http_status=response.status_code,
                    headers=dict(response.headers),
                    retry_after_seconds=retry_after,
                )
            if signed and _retry_time_sync > 0 and code == -1021:
                await self._sync_time()
                await asyncio.sleep(0.05)
                return await self._request(
                    method,
                    path,
                    params,
                    signed=signed,
                    _retry_time_sync=_retry_time_sync - 1,
                )
            raise ExchangeError(
                f"{response.status_code} [{code}]: {detail}",
                code=int(code) if isinstance(code, int) else None,
                http_status=response.status_code,
                headers=dict(response.headers),
            )
        body = response.json()
        # Any successful order/algo mutation can change the exchange position
        # or protection state.  Invalidate both short-lived read caches so a
        # subsequent compiler/reconciliation pass observes the fresh state.
        if method in {"POST", "DELETE"} and path in {
            "/fapi/v1/order",
            "/fapi/v1/algoOrder",
            "/fapi/v1/leverage",
            "/fapi/v1/marginType",
        }:
            self._invalidate_position_cache()
            self._all_algo_cache = None
        return body

    async def _request_gate_wait(self) -> None:
        """Serialize request starts and fail fast during a Binance ban window."""

        async with self._request_gate:
            now = time.monotonic()
            if now < self._request_backoff_until:
                remaining = self._request_backoff_until - now
                raise ExchangeError(
                    f"Binance REST backoff active for {remaining:.0f}s"
                    + (f": {self._request_backoff_reason}" if self._request_backoff_reason else ""),
                    code=-1003,
                    http_status=429,
                    retry_after_seconds=remaining,
                )
            wait = self._request_min_interval - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _rate_limit_delay(response: httpx.Response, detail: str) -> float:
        """Use Binance's ban deadline when present, otherwise Retry-After."""

        match = re.search(r"banned until (\d+)", detail, flags=re.IGNORECASE)
        if match:
            try:
                return min(max((int(match.group(1)) / 1000) - time.time(), 5.0), 900.0)
            except ValueError:
                pass
        raw_retry_after = response.headers.get("Retry-After")
        if raw_retry_after:
            try:
                return min(max(float(raw_retry_after), 5.0), 900.0)
            except ValueError:
                pass
        return 60.0

    def _invalidate_position_cache(self) -> None:
        self._positions_cache = None

    async def _sync_time(self) -> None:
        before = time.time() * 1000
        body = await self._request("GET", "/fapi/v1/time", _retry_time_sync=0)
        after = time.time() * 1000
        server_time = int(body["serverTime"])
        self.time_offset_ms = int(server_time - ((before + after) / 2))

    @staticmethod
    def _order_state(body: dict[str, Any]) -> OrderState:
        status_map = {
            "NEW": OrderStatus.SUBMITTED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }
        return OrderState(
            client_order_id=body["clientOrderId"],
            exchange_order_id=str(body.get("orderId", "")) or None,
            symbol=body["symbol"],
            side=body["side"],
            position_side=PositionSide(body["positionSide"]),
            order_type=body["type"],
            quantity=Decimal(body.get("origQty", "0")),
            price=Decimal(body["price"]) if body.get("price") else None,
            stop_price=Decimal(body["stopPrice"]) if body.get("stopPrice") else None,
            filled_quantity=Decimal(body.get("executedQty", "0")),
            average_price=Decimal(body.get("avgPrice", "0")),
            status=status_map.get(body["status"], OrderStatus.CREATED),
        )

    @staticmethod
    def _fmt(value: Decimal) -> str:
        return format(value.normalize(), "f")

    @staticmethod
    def _round_quantity(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step
