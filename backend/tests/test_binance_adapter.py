from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from tests.factories import position
from trading_system.config import Settings
from trading_system.domain.enums import PositionSide
from trading_system.domain.models import ExecutionIntent
from trading_system.exchange.base import ExchangeError, ExchangeUnknownStatusError
from trading_system.exchange.binance import BinanceUSDMarketClient


def exchange_settings(tmp_path: object, *, configured: bool = True) -> Settings:
    if configured:
        tmp_path.joinpath("binance_testnet_api_key").write_text("api-key", encoding="utf-8")
        tmp_path.joinpath("binance_testnet_api_secret").write_text("api-secret", encoding="utf-8")
    return Settings(secret_dir=tmp_path, binance_testnet_base_url="https://binance.example")


def proxy_exchange_settings(tmp_path: object) -> Settings:
    tmp_path.joinpath("binance_testnet_api_key").write_text("api-key", encoding="utf-8")
    tmp_path.joinpath("binance_testnet_api_secret").write_text("api-secret", encoding="utf-8")
    tmp_path.joinpath("http_proxy_url").write_text("http://proxy.example:8080", encoding="utf-8")
    return Settings(
        secret_dir=tmp_path,
        binance_testnet_base_url="https://binance.example",
        http_proxy_enabled=True,
    )


def order_body(params: httpx.QueryParams, order_id: int) -> dict[str, object]:
    return {
        "clientOrderId": params.get("newClientOrderId", "order"),
        "orderId": order_id,
        "symbol": params.get("symbol", "BTCUSDT"),
        "side": params.get("side", "SELL"),
        "positionSide": params.get("positionSide", "LONG"),
        "type": params.get("type", "MARKET"),
        "origQty": params.get("quantity", "0"),
        "executedQty": params.get("quantity", "0"),
        "avgPrice": "100",
        "price": params.get("price", "0"),
        "stopPrice": params.get("stopPrice", "0"),
        "status": "FILLED" if params.get("type") == "MARKET" else "NEW",
    }


@pytest.mark.asyncio
async def test_health_without_credentials_does_not_touch_network(tmp_path: object) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = BinanceUSDMarketClient(
        exchange_settings(tmp_path, configured=False), httpx.MockTransport(handler)
    )
    try:
        assert await client.health_check() == (False, "Binance credentials not configured")
    finally:
        await client.close()
    assert calls == 0


@pytest.mark.asyncio
async def test_proxy_mode_is_configured_for_binance_and_missing_proxy_fails_closed(
    tmp_path: object,
) -> None:
    client = BinanceUSDMarketClient(
        proxy_exchange_settings(tmp_path),
        httpx.MockTransport(lambda _: httpx.Response(200, json={"serverTime": 0})),
    )
    try:
        assert client.http._mounts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_binance_proxy_can_be_disabled_independently_of_model_proxy(
    tmp_path: object,
) -> None:
    tmp_path.joinpath("binance_testnet_api_key").write_text("api-key", encoding="utf-8")
    tmp_path.joinpath("binance_testnet_api_secret").write_text("api-secret", encoding="utf-8")
    tmp_path.joinpath("http_proxy_url").write_text("http://proxy.example:8080", encoding="utf-8")
    settings = Settings(
        secret_dir=tmp_path,
        binance_testnet_base_url="https://binance.example",
        http_proxy_enabled=True,
        binance_http_proxy_enabled=False,
    )
    client = BinanceUSDMarketClient(settings)
    try:
        assert not client.http._mounts
        assert settings.http_proxy_configured is True
        assert settings.binance_http_proxy_configured is False
    finally:
        await client.close()

    missing = tmp_path / "missing"
    missing.mkdir()
    missing.joinpath("binance_testnet_api_key").write_text("api-key", encoding="utf-8")
    missing.joinpath("binance_testnet_api_secret").write_text("api-secret", encoding="utf-8")
    settings = Settings(
        secret_dir=missing,
        binance_testnet_base_url="https://binance.example",
        http_proxy_enabled=True,
    )
    client = BinanceUSDMarketClient(settings, httpx.MockTransport(lambda _: httpx.Response(200)))
    try:
        assert await client.health_check() == (
            False,
            "HTTP 代理已启用但未挂载 http_proxy_url secret",
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_universe_skips_non_standard_testnet_symbols(tmp_path: object) -> None:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "quoteAsset": "USDT",
                            "contractType": "PERPETUAL",
                            "status": "TRADING",
                            "onboardDate": now_ms - 100 * 86_400_000,
                        },
                        {
                            "symbol": "测试测试USDT",
                            "quoteAsset": "USDT",
                            "contractType": "PERPETUAL",
                            "status": "TRADING",
                            "onboardDate": now_ms - 100 * 86_400_000,
                        },
                    ]
                },
            )
        if request.url.path == "/fapi/v1/ticker/24hr":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "quoteVolume": "100"}])
        if request.url.path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(
                200, json=[{"symbol": "BTCUSDT", "bidPrice": "99", "askPrice": "101"}]
            )
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "markPrice": "100",
                        "indexPrice": "100",
                        "lastFundingRate": "0",
                    }
                ],
            )
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        universe = await client.get_universe(30)
    finally:
        await client.close()
    assert [item.symbol for item in universe] == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_best_entry_price_uses_marketable_side_of_book(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(200, json={"bidPrice": "99", "askPrice": "101"})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        assert await client.best_entry_price("BTCUSDT", "BUY") == Decimal("101")
        assert await client.best_entry_price("BTCUSDT", "SELL") == Decimal("99")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_account_state_includes_realized_fees_and_funding_ledger(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v2/account":
            return httpx.Response(
                200,
                json={
                    "totalMarginBalance": "1000",
                    "availableBalance": "900",
                    "totalUnrealizedProfit": "2",
                    "totalInitialMargin": "100",
                },
            )
        if request.url.path == "/fapi/v1/income":
            return httpx.Response(
                200,
                json=[
                    {"incomeType": "REALIZED_PNL", "income": "3", "asset": "USDT", "time": 1},
                    {"incomeType": "COMMISSION", "income": "-0.5", "asset": "USDT", "time": 2},
                    {"incomeType": "FUNDING_FEE", "income": "-0.2", "asset": "USDT", "time": 3},
                ],
            )
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        account = await client.get_account_state()
    finally:
        await client.close()
    assert account.realized_pnl_today == Decimal("3")
    assert account.fees_today == Decimal("0.5")
    assert account.funding_today == Decimal("-0.2")
    assert len(client.last_income_ledger) == 3


@pytest.mark.asyncio
async def test_historical_funding_rates_are_timestamped_and_range_bounded(
    tmp_path: object,
) -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/fundingRate"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(
            200,
            json=[
                {"fundingTime": start_ms, "fundingRate": "0.0001"},
                {"fundingTime": end_ms, "fundingRate": "0.0002"},
            ],
        )

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        rates = await client.get_historical_funding_rates("BTCUSDT", start_ms, end_ms)
    finally:
        await client.close()
    assert rates == {start: Decimal("0.0001")}


@pytest.mark.asyncio
async def test_signed_health_and_hedge_position_protection_parsing(tmp_path: object) -> None:
    signed_queries: list[httpx.QueryParams] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(
                200, json={"serverTime": int(datetime.now(UTC).timestamp() * 1000)}
            )
        if request.url.path == "/fapi/v2/account":
            signed_queries.append(request.url.params)
            return httpx.Response(200, json={"canTrade": True})
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": True})
        if request.url.path == "/fapi/v1/multiAssetsMargin":
            return httpx.Response(200, json={"multiAssetsMargin": False})
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "0.2",
                        "entryPrice": "100",
                        "markPrice": "102",
                        "unRealizedProfit": "0.4",
                        "isolatedWallet": "10",
                        "marginType": "isolated",
                        "leverage": "3",
                    }
                ],
                )
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "102"})
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "orderType": "STOP_MARKET",
                        "algoStatus": "NEW",
                        "clientAlgoId": "frc_health_sl",
                        "closePosition": "true",
                        "triggerPrice": "99",
                    }
                ],
            )
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        healthy, _ = await client.health_check()
        positions = await client.get_positions()
    finally:
        await client.close()
    assert healthy is True
    assert signed_queries[0].get("timestamp")
    assert signed_queries[0].get("signature")
    assert signed_queries[0].get("recvWindow") == "5000"
    assert positions[0].protected is True
    assert positions[0].stop_price == Decimal("99")
    assert positions[0].current_r == Decimal("2")


@pytest.mark.asyncio
async def test_health_uses_the_configured_30x_leverage_ceiling(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(
                200, json={"serverTime": int(datetime.now(UTC).timestamp() * 1000)}
            )
        if request.url.path == "/fapi/v2/account":
            return httpx.Response(200, json={"canTrade": True})
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": True})
        if request.url.path == "/fapi/v1/multiAssetsMargin":
            return httpx.Response(200, json={"multiAssetsMargin": False})
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.01",
                        "marginType": "isolated",
                        "leverage": "30",
                    }
                ],
            )
        raise AssertionError(request.url)

    settings = exchange_settings(tmp_path)
    settings.max_leverage = 30
    client = BinanceUSDMarketClient(settings, httpx.MockTransport(handler))
    try:
        assert await client.health_check() == (
            True,
            "time, permissions, hedge mode, and isolated positions healthy",
        )
        settings.max_leverage = 29
        healthy, detail = await client.health_check()
    finally:
        await client.close()
    assert healthy is False
    assert "configured 29x maximum" in detail


@pytest.mark.asyncio
async def test_configure_symbol_ignores_testnet_margin_conflict_when_already_isolated(
    tmp_path: object,
) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(
                400,
                json={
                    "code": -4067,
                    "msg": "Position side cannot be changed if there exists open orders.",
                },
            )
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[{"symbol": "LIGHTUSDT", "marginType": "isolated"}],
            )
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"symbol": "LIGHTUSDT", "leverage": 3})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(
        exchange_settings(tmp_path), httpx.MockTransport(handler)
    )
    try:
        await client.configure_symbol("LIGHTUSDT", 3)
    finally:
        await client.close()

    assert calls == ["/fapi/v1/marginType", "/fapi/v2/positionRisk", "/fapi/v1/leverage"]


@pytest.mark.asyncio
async def test_configure_symbol_does_not_hide_margin_conflict_for_crossed_symbol(
    tmp_path: object,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(
                400,
                json={
                    "code": -4067,
                    "msg": "Position side cannot be changed if there exists open orders.",
                },
            )
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[{"symbol": "LIGHTUSDT", "marginType": "crossed"}],
            )
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(
        exchange_settings(tmp_path), httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(ExchangeError, match="-4067"):
            await client.configure_symbol("LIGHTUSDT", 3)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_signed_request_resynchronizes_time_and_retries_after_timestamp_error(
    tmp_path: object,
) -> None:
    signed_attempts = 0
    time_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal signed_attempts, time_requests
        if request.url.path == "/fapi/v1/time":
            time_requests += 1
            return httpx.Response(
                200,
                json={"serverTime": int(datetime.now(UTC).timestamp() * 1000) + 25},
            )
        if request.url.path == "/fapi/v2/account":
            signed_attempts += 1
            if signed_attempts == 1:
                return httpx.Response(
                    400,
                    json={
                        "code": -1021,
                        "msg": "Timestamp for this request was 1000ms ahead of the server's time.",
                    },
                )
            return httpx.Response(200, json={"totalMarginBalance": "1000"})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        body = await client._request("GET", "/fapi/v2/account", signed=True)
    finally:
        await client.close()

    assert body["totalMarginBalance"] == "1000"
    assert signed_attempts == 2
    assert time_requests == 1


def test_stop_replace_conflict_recognizes_binance_close_position_spellings() -> None:
    for message in (
        "An open stop or take profit order with closePosition in the direction is existing.",
        "An open order with close_position side already exists.",
        "CLOSE POSITION order conflict",
    ):
        assert BinanceUSDMarketClient._stop_replace_requires_cancel(
            ExchangeError(message, http_status=400)
        )


@pytest.mark.asyncio
async def test_protection_orders_use_hedge_side_and_close_position_stop(tmp_path: object) -> None:
    submitted: list[httpx.QueryParams] = []
    algo_orders: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.1",
                                    "minQty": "0.1",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "102"})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            submitted.append(request.url.params)
            row = {
                "algoId": len(submitted),
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": request.url.params["symbol"],
                "side": request.url.params["side"],
                "positionSide": request.url.params["positionSide"],
                "orderType": request.url.params["type"],
                "algoStatus": "NEW",
                "quantity": request.url.params.get("quantity", "0"),
                "triggerPrice": request.url.params.get("triggerPrice", "0"),
                "closePosition": request.url.params.get("closePosition", "false"),
            }
            algo_orders.append(row)
            return httpx.Response(200, json=row)
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        orders = await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
    finally:
        await client.close()
    assert len(orders) == 3
    stop = submitted[0]
    assert stop["positionSide"] == "LONG"
    assert stop["side"] == "SELL"
    assert stop["closePosition"] == "true"
    assert "quantity" not in stop
    assert stop["clientAlgoId"].endswith("_sl")
    assert [item["quantity"] for item in submitted[1:]] == ["0.4", "0.4"]


@pytest.mark.asyncio
async def test_protection_first_target_follows_actual_fill_price(tmp_path: object) -> None:
    submitted: list[httpx.QueryParams] = []
    algo_orders: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.1",
                                    "minQty": "0.1",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            submitted.append(request.url.params)
            row = {
                "algoId": len(submitted),
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": request.url.params["symbol"],
                "side": request.url.params["side"],
                "positionSide": request.url.params["positionSide"],
                "orderType": request.url.params["type"],
                "algoStatus": "NEW",
                "quantity": request.url.params.get("quantity", "0"),
                "triggerPrice": request.url.params["triggerPrice"],
                "closePosition": request.url.params.get("closePosition", "false"),
            }
            algo_orders.append(row)
            return httpx.Response(200, json=row)
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("105.0000000000000003"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        await client.upsert_protection(intent, Decimal("1"), Decimal("100.5"))
    finally:
        await client.close()

    assert submitted[1]["triggerPrice"] == "102"
    assert submitted[2]["triggerPrice"] == "105"


@pytest.mark.asyncio
async def test_protection_rejects_equal_targets_before_any_exchange_request(
    tmp_path: object,
) -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError(request.url)

    intent = ExecutionIntent.model_construct(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("101"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        with pytest.raises(ExchangeError, match="TP1 below TP2"):
            await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
    finally:
        await client.close()

    assert requests == 0


@pytest.mark.asyncio
async def test_protection_tp2_only_stage_does_not_recreate_tp1(tmp_path: object) -> None:
    submitted: list[httpx.QueryParams] = []
    algo_orders: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.1",
                                    "minQty": "0.1",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            submitted.append(request.url.params)
            row = {
                "algoId": len(submitted),
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": request.url.params["symbol"],
                "side": request.url.params["side"],
                "positionSide": request.url.params["positionSide"],
                "orderType": request.url.params["type"],
                "algoStatus": "NEW",
                "quantity": request.url.params.get("quantity", "0"),
                "triggerPrice": request.url.params["triggerPrice"],
                "closePosition": request.url.params.get("closePosition", "false"),
            }
            algo_orders.append(row)
            return httpx.Response(200, json=row)
        raise AssertionError(request.url)

    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("100"),
        entry_max=Decimal("100"),
        stop_price=Decimal("99"),
        tp1_price=None,
        tp2_price=Decimal("105"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        orders = await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
    finally:
        await client.close()

    assert [item.order_type for item in orders] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert [item["type"] for item in submitted] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert submitted[1]["clientAlgoId"].endswith("_t2")
    assert submitted[1]["triggerPrice"] == "105"


@pytest.mark.asyncio
async def test_protection_upsert_is_idempotent_and_cancels_stale_algo_orders(
    tmp_path: object,
) -> None:
    posts = 0
    deletes = 0
    algo_orders: list[dict[str, object]] = [
        {
            "algoId": 99,
            "clientAlgoId": "frc_stale_sl",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "orderType": "STOP_MARKET",
            "algoStatus": "NEW",
            "quantity": "0",
            "triggerPrice": "98",
            "closePosition": "true",
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, deletes
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "102"})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            posts += 1
            row = {
                "algoId": posts,
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": request.url.params["symbol"],
                "side": request.url.params["side"],
                "positionSide": request.url.params["positionSide"],
                "orderType": request.url.params["type"],
                "algoStatus": "NEW",
                "quantity": request.url.params.get("quantity", "0"),
                "triggerPrice": request.url.params.get("triggerPrice", "0"),
                "closePosition": request.url.params.get("closePosition", "false"),
            }
            algo_orders.append(row)
            return httpx.Response(200, json=row)
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "GET":
            match = next(
                row
                for row in algo_orders
                if row["clientAlgoId"] == request.url.params["clientAlgoId"]
            )
            return httpx.Response(200, json=match)
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            deletes += 1
            algo_id = request.url.params.get("algoId")
            client_algo_id = request.url.params.get("clientAlgoId")
            for row in algo_orders:
                matches = (
                    row["algoId"] == int(algo_id)
                    if algo_id
                    else row["clientAlgoId"] == client_algo_id
                )
                if matches:
                    row["algoStatus"] = "CANCELED"
            return httpx.Response(200, json={"success": True, "clientAlgoId": client_algo_id})
        raise AssertionError(request.url)

    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        first = await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
        second = await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
        third = await client.upsert_protection(intent, Decimal("1.5"), Decimal("100"))
    finally:
        await client.close()
    assert posts == 5
    assert deletes == 3
    assert [item.client_order_id for item in first] == [item.client_order_id for item in second]
    assert first[0].client_order_id == third[0].client_order_id
    assert len(
        [
            item
            for item in algo_orders
            if item["orderType"] == "STOP_MARKET" and item["algoStatus"] == "NEW"
        ]
    ) == 1


@pytest.mark.asyncio
async def test_protection_upsert_cancels_old_close_orders_before_replacement(
    tmp_path: object,
) -> None:
    events: list[str] = []
    algo_orders: list[dict[str, object]] = [
        {
            "algoId": 90,
            "clientAlgoId": "frc_previous_sl",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "orderType": "STOP_MARKET",
            "algoStatus": "NEW",
            "quantity": "0",
            "triggerPrice": "98",
            "closePosition": "true",
        },
        {
            "algoId": 91,
            "clientAlgoId": "frc_previous_t1",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "orderType": "TAKE_PROFIT_MARKET",
            "algoStatus": "NEW",
            "quantity": "0.8",
            "triggerPrice": "101",
            "closePosition": "false",
        },
        {
            "algoId": 92,
            "clientAlgoId": "frc_previous_t2",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "orderType": "TAKE_PROFIT_MARKET",
            "algoStatus": "NEW",
            "quantity": "0.8",
            "triggerPrice": "102",
            "closePosition": "false",
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            client_algo_id = request.url.params.get("clientAlgoId")
            algo_id = request.url.params.get("algoId")
            target = next(
                row
                for row in algo_orders
                if (
                    str(row["algoId"]) == algo_id
                    if algo_id
                    else row["clientAlgoId"] == client_algo_id
                )
            )
            target["algoStatus"] = "CANCELED"
            events.append(f"delete:{target['clientAlgoId']}")
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            if request.url.params["type"] == "STOP_MARKET" and any(
                row["algoStatus"] == "NEW" and row["orderType"] == "STOP_MARKET"
                for row in algo_orders
            ):
                # This is the exact Binance conflict that occurred in the
                # production testnet partial-reduction cycle.
                return httpx.Response(
                    400,
                    json={
                        "code": -4130,
                        "msg": (
                            "An open stop or take profit order with GTE and "
                            "closePosition in the direction is existing."
                        ),
                    },
                )
            row = {
                "algoId": 100 + len(algo_orders),
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": request.url.params["symbol"],
                "side": request.url.params["side"],
                "positionSide": request.url.params["positionSide"],
                "orderType": request.url.params["type"],
                "algoStatus": "NEW",
                "quantity": request.url.params.get("quantity", "0"),
                "triggerPrice": request.url.params.get("triggerPrice", "0"),
                "closePosition": request.url.params.get("closePosition", "false"),
            }
            algo_orders.append(row)
            events.append(f"post:{row['orderType']}")
            return httpx.Response(200, json=row)
        raise AssertionError(request.url)

    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        orders = await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
    finally:
        await client.close()

    assert len(orders) == 3
    assert events[:3] == [
        "delete:frc_previous_sl",
        "delete:frc_previous_t1",
        "delete:frc_previous_t2",
    ]
    assert all(event.startswith("delete:") for event in events[:3])
    assert events[3:] == [
        "post:STOP_MARKET",
        "post:TAKE_PROFIT_MARKET",
        "post:TAKE_PROFIT_MARKET",
    ]
    active = [row for row in algo_orders if row["algoStatus"] == "NEW"]
    assert len([row for row in active if row["orderType"] == "STOP_MARKET"]) == 1
    assert len([row for row in active if row["orderType"] == "TAKE_PROFIT_MARKET"]) == 2


@pytest.mark.asyncio
async def test_protection_upsert_fails_closed_when_cancellation_is_not_observed(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def immediate_sleep(seconds: float) -> None:
        del seconds

    monkeypatch.setattr("trading_system.exchange.binance.asyncio.sleep", immediate_sleep)
    deletes = 0
    posts = 0
    old_stop = {
        "algoId": 90,
        "clientAlgoId": "frc_previous_sl",
        "symbol": "BTCUSDT",
        "side": "SELL",
        "positionSide": "LONG",
        "orderType": "STOP_MARKET",
        "algoStatus": "NEW",
        "quantity": "0",
        "triggerPrice": "98",
        "closePosition": "true",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal deletes, posts
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": [old_stop]})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            deletes += 1
            # Simulate Binance acknowledging DELETE while its open-order read
            # still reports the old conditional order as active.
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            posts += 1
            return httpx.Response(500, json={"msg": "unexpected replacement"})
        raise AssertionError(request.url)

    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        with pytest.raises(ExchangeError, match="cancellation not confirmed"):
            await client.upsert_protection(intent, Decimal("1"), Decimal("100"))
    finally:
        await client.close()

    assert deletes == 1
    assert posts == 0


@pytest.mark.asyncio
async def test_tighten_stop_creates_replacement_before_canceling_old_stop(
    tmp_path: object,
) -> None:
    events: list[str] = []
    algo_orders: list[dict[str, object]] = [
        {
            "algoId": 1,
            "clientAlgoId": "frc_original_sl",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "orderType": "STOP_MARKET",
            "algoStatus": "NEW",
            "quantity": "0",
            "triggerPrice": "99",
            "closePosition": "true",
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "102"})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            events.append("post")
            row = {
                "algoId": 2,
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "LONG",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "quantity": "0",
                "triggerPrice": request.url.params["triggerPrice"],
                "closePosition": "true",
            }
            algo_orders.append(row)
            return httpx.Response(200, json=row)
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            events.append("delete")
            for row in algo_orders:
                if str(row["algoId"]) == request.url.params.get("algoId"):
                    row["algoStatus"] = "CANCELED"
            return httpx.Response(200, json={"success": True})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    current = position(
        position_id="binance-BTCUSDT-LONG",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=Decimal("100"),
        mark_price=Decimal("102"),
        stop_price=Decimal("99"),
    )
    try:
        replacement = await client.tighten_stop(current, Decimal("100.2"))
    finally:
        await client.close()

    assert events == ["post", "delete"]
    assert replacement.stop_price == Decimal("100.2")


@pytest.mark.asyncio
async def test_tighten_stop_falls_back_to_cancel_create_on_close_position_conflict(
    tmp_path: object,
) -> None:
    events: list[str] = []
    post_attempts = 0
    algo_orders: list[dict[str, object]] = [
        {
            "algoId": 1,
            "clientAlgoId": "frc_original_sl",
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "orderType": "STOP_MARKET",
            "algoStatus": "NEW",
            "quantity": "0",
            "triggerPrice": "99",
            "closePosition": "true",
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempts
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"},
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "102"})
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json={"orders": algo_orders})
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "POST":
            post_attempts += 1
            events.append("post")
            active_stop_exists = any(
                row["algoStatus"] == "NEW" and row["orderType"] == "STOP_MARKET"
                for row in algo_orders
            )
            if active_stop_exists:
                return httpx.Response(
                    400,
                    json={"code": -4130, "msg": "An open order with CLOSE_POSITION side exists."},
                )
            row = {
                "algoId": 2,
                "clientAlgoId": request.url.params["clientAlgoId"],
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "LONG",
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "quantity": "0",
                "triggerPrice": request.url.params["triggerPrice"],
                "closePosition": "true",
            }
            algo_orders.append(row)
            return httpx.Response(200, json=row)
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            events.append("delete")
            for row in algo_orders:
                if str(row["algoId"]) == request.url.params.get("algoId"):
                    row["algoStatus"] = "CANCELED"
            return httpx.Response(200, json={"success": True})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    current = position(
        position_id="binance-BTCUSDT-LONG",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        entry_price=Decimal("100"),
        mark_price=Decimal("102"),
        stop_price=Decimal("99"),
    )
    try:
        replacement = await client.tighten_stop(current, Decimal("100.2"))
        current.stop_price = Decimal("100.2")
        second_replacement = await client.tighten_stop(current, Decimal("100.4"))
    finally:
        await client.close()

    assert post_attempts == 3
    assert events == ["post", "delete", "post", "delete", "post"]
    assert replacement.stop_price == Decimal("100.2")
    assert second_replacement.stop_price == Decimal("100.4")
    assert len([row for row in algo_orders if row["algoStatus"] == "NEW"]) == 1


@pytest.mark.asyncio
async def test_get_positions_reads_algo_protection_only(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "positionAmt": "0.2",
                        "entryPrice": "100",
                        "markPrice": "102",
                        "unRealizedProfit": "0.4",
                        "isolatedWallet": "10",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "algoId": 7,
                            "clientAlgoId": "frc_abc_sl",
                            "symbol": "BTCUSDT",
                            "side": "SELL",
                            "positionSide": "LONG",
                            "orderType": "STOP_MARKET",
                            "algoStatus": "NEW",
                            "triggerPrice": "99",
                            "closePosition": True,
                        }
                        ,
                        {
                            "algoId": 8,
                            "clientAlgoId": "frc_abc_t1",
                            "symbol": "BTCUSDT",
                            "side": "SELL",
                            "positionSide": "LONG",
                            "orderType": "TAKE_PROFIT_MARKET",
                            "algoStatus": "NEW",
                            "triggerPrice": "103",
                        },
                        {
                            "algoId": 9,
                            "clientAlgoId": "frc_abc_t2",
                            "symbol": "BTCUSDT",
                            "side": "SELL",
                            "positionSide": "LONG",
                            "orderType": "TAKE_PROFIT_MARKET",
                            "algoStatus": "NEW",
                            "triggerPrice": "106",
                        }
                    ]
                },
            )
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        positions = await client.get_positions()
    finally:
        await client.close()
    assert positions[0].protected is True
    assert positions[0].stop_price == Decimal("99")
    assert positions[0].tp1_price == Decimal("103")
    assert positions[0].tp2_price == Decimal("106")


@pytest.mark.asyncio
async def test_duplicate_manual_reduce_queries_existing_order(tmp_path: object) -> None:
    posts = 0
    client_order_id = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts, client_order_id
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "ETHUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.1",
                                    "minQty": "0.1",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            posts += 1
            client_order_id = request.url.params["newClientOrderId"]
            return httpx.Response(400, json={"msg": "Duplicate order sent."})
        if request.url.path == "/fapi/v1/order" and request.method == "GET":
            assert request.url.params["origClientOrderId"] == client_order_id
            params = httpx.QueryParams(
                {
                    "newClientOrderId": client_order_id,
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "positionSide": "LONG",
                    "type": "MARKET",
                    "quantity": "0.5",
                }
            )
            return httpx.Response(200, json=order_body(params, 99))
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        order = await client.close_position_quantity_market(
            position(quantity=Decimal("1")), Decimal("0.5"), "operation-123"
        )
    finally:
        await client.close()
    assert posts == 1
    assert order.client_order_id == client_order_id
    assert order.filled_quantity == Decimal("0.5")


@pytest.mark.asyncio
async def test_pause_cancels_managed_partially_filled_entries_only(tmp_path: object) -> None:
    managed_open = True
    canceled: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal managed_open
        if request.url.path == "/fapi/v1/openOrders":
            rows = [
                {
                    "symbol": "BTCUSDT",
                    "type": "LIMIT",
                    "status": "NEW",
                    "clientOrderId": "manual-order",
                }
            ]
            if managed_open:
                rows.append(
                    {
                        "symbol": "BTCUSDT",
                        "type": "LIMIT",
                        "status": "PARTIALLY_FILLED",
                        "clientOrderId": "frc_managed_e0",
                    }
                )
            return httpx.Response(200, json=rows)
        if request.url.path == "/fapi/v1/order" and request.method == "DELETE":
            canceled.append(request.url.params["origClientOrderId"])
            managed_open = False
            params = httpx.QueryParams(
                {
                    "newClientOrderId": "frc_managed_e0",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "positionSide": "LONG",
                    "type": "LIMIT",
                    "quantity": "1",
                }
            )
            body = order_body(params, 1)
            body["status"] = "CANCELED"
            return httpx.Response(200, json=body)
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        await client.cancel_all_entry_orders()
    finally:
        await client.close()
    assert canceled == ["frc_managed_e0"]


@pytest.mark.asyncio
async def test_income_history_paginates_and_deduplicates(tmp_path: object) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/income"
        calls.append(request.url.params["startTime"])
        if request.url.params["startTime"] == "0":
            return httpx.Response(
                200,
                json=[
                    {
                        "tranId": "1",
                        "incomeType": "COMMISSION",
                        "income": "-1",
                        "time": 1,
                    },
                ],
            )
        return httpx.Response(200, json=[])

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        rows = await client.get_income_history(0, limit=1)
    finally:
        await client.close()
    assert calls == ["0", "2"]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_market_close_uses_market_lot_size_and_chunks_large_quantity(
    tmp_path: object,
) -> None:
    quantities: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "stepSize": "0.1",
                                    "minQty": "0.1",
                                    "maxQty": "100",
                                },
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "stepSize": "0.01",
                                    "minQty": "0.01",
                                    "maxQty": "1",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            quantities.append(request.url.params["quantity"])
            return httpx.Response(200, json=order_body(request.url.params, len(quantities)))
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        await client.close_position_quantity_market(
            position(symbol="BTCUSDT", quantity=Decimal("2.005")), Decimal("2.005"), "chunked-close"
        )
    finally:
        await client.close()
    assert quantities == ["1", "1"]


@pytest.mark.asyncio
async def test_orphan_algo_protection_is_canceled(tmp_path: object) -> None:
    canceled: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "algoId": 7,
                        "clientAlgoId": "frc_orphan_sl",
                        "symbol": "BTCUSDT",
                        "positionSide": "LONG",
                        "orderType": "STOP_MARKET",
                        "algoStatus": "NEW",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE":
            canceled.append(request.url.params["algoId"])
            return httpx.Response(200, json={})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    try:
        count = await client.cancel_orphan_protection_orders(set())
    finally:
        await client.close()
    assert count == 1
    assert canceled == ["7"]


@pytest.mark.asyncio
async def test_ambiguous_limit_entry_is_resolved_by_query(tmp_path: object) -> None:
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            posts += 1
            return httpx.Response(503, json={"msg": "Unknown error, please check your request"})
        if request.url.path == "/fapi/v1/order" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "clientOrderId": request.url.params["origClientOrderId"],
                    "orderId": 321,
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "positionSide": "LONG",
                    "type": "LIMIT",
                    "origQty": "0.1",
                    "executedQty": "0.1",
                    "avgPrice": "100",
                    "price": "100",
                    "status": "FILLED",
                },
            )
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        state = await client.place_limit_entry(intent, "frc_ambiguous_e0", Decimal("100"))
    finally:
        await client.close()
    assert posts == 1
    assert state.status.name == "FILLED"
    assert state.filled_quantity == Decimal("0.1")


@pytest.mark.asyncio
async def test_ambiguous_order_without_query_result_fails_closed(tmp_path: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            return httpx.Response(503, json={"msg": "Unknown error"})
        if request.url.path == "/fapi/v1/order" and request.method == "GET":
            return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist"})
        raise AssertionError(request.url)

    client = BinanceUSDMarketClient(exchange_settings(tmp_path), httpx.MockTransport(handler))
    intent = ExecutionIntent(
        signal_id="1fef93c8-f2d0-4d45-95f0-e5506fd7fa53",
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        limit_price=Decimal("100"),
        entry_min=Decimal("99.5"),
        entry_max=Decimal("100.5"),
        stop_price=Decimal("99"),
        tp1_price=Decimal("101"),
        tp2_price=Decimal("102"),
        leverage=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        with pytest.raises(ExchangeUnknownStatusError):
            await client.place_limit_entry(intent, "frc_ambiguous_missing", Decimal("100"))
    finally:
        await client.close()
