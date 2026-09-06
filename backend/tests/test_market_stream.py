from __future__ import annotations

import pytest

from trading_system.exchange.market_stream import BinancePublicMarketCache


@pytest.mark.asyncio
async def test_public_market_cache_tracks_ticker_book_and_mark_freshness() -> None:
    cache = BinancePublicMarketCache("wss://stream.example")

    cache._apply_payload(
        {
            "stream": "!markPrice@arr@1s",
            "data": {
                "e": "markPriceUpdate",
                "s": "BTCUSDT",
                "p": "100",
                "i": "100",
                "r": "0.0001",
            },
        }
    )
    assert cache.mark_snapshot("BTCUSDT") == {"mark_price": "100"}
    assert cache.book_snapshot("BTCUSDT") is None

    cache._apply_payload(
        {
            "stream": "!bookTicker",
            "data": {
                "e": "bookTicker",
                "s": "BTCUSDT",
                "b": "99",
                "a": "101",
            },
        }
    )
    cache._apply_payload(
        {
            "stream": "!ticker@arr",
            "data": [{"e": "24hrTicker", "s": "BTCUSDT", "q": "2500000"}],
        }
    )

    assert cache.book_snapshot("BTCUSDT") == {"best_bid": "99", "best_ask": "101"}
    assert cache.snapshot("BTCUSDT") == {
        "mark_price": "100",
        "index_price": "100",
        "funding_rate": "0.0001",
        "best_bid": "99",
        "best_ask": "101",
        "quote_volume_24h": "2500000",
        "last_event": "24hrTicker",
        "ticker_updated_at": cache._rows["BTCUSDT"]["ticker_updated_at"],
        "book_updated_at": cache._rows["BTCUSDT"]["book_updated_at"],
        "mark_updated_at": cache._rows["BTCUSDT"]["mark_updated_at"],
    }
