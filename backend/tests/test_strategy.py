from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.factories import position, snapshot
from trading_system.domain.enums import ReviewAction
from trading_system.domain.models import ExchangeFilters, PositionReview
from trading_system.exchange.base import ExchangeError
from trading_system.orchestration.cycle import CycleResult, TradingCycle
from trading_system.strategy.indicators import (
    atr,
    ema,
    pearson_correlation,
    trend_direction,
)
from trading_system.strategy.screener import MarketScreener


def test_indicator_basics_and_conservative_short_correlation() -> None:
    assert ema([Decimal("1"), Decimal("2"), Decimal("3")], 2)[-1] > Decimal("2")
    assert atr([]) == 0
    assert trend_direction([]) == 0
    assert pearson_correlation([Decimal("1")] * 10, [Decimal("1")] * 10) == 1


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"listing_days": 89}, "listing_too_recent"),
        ({"spread_pct": Decimal("0.0016")}, "spread_too_wide"),
        ({"funding_rate": Decimal("0.0011")}, "funding_rate_abnormal"),
        ({"basis_pct": Decimal("0.011")}, "basis_abnormal"),
        ({"book_depth_usdt": Decimal("49999")}, "insufficient_book_depth"),
        ({"volatility_percentile": Decimal("1")}, "extreme_volatility"),
        ({"trend_4h": -1}, "trend_not_aligned"),
        ({"breakout_15m": 0, "pullback_15m": 0}, "no_aligned_entry_trigger"),
    ],
)
def test_screener_excludes_abnormal_market_conditions(
    updates: dict[str, object], reason: str
) -> None:
    eligible, reasons = MarketScreener().eligible(snapshot(**updates))
    assert eligible is False
    assert reason in reasons


def test_screener_ranks_only_eligible_top_five() -> None:
    rows = [snapshot(symbol=f"COIN{index}USDT", volume_zscore=Decimal(index)) for index in range(8)]
    rows.append(snapshot(symbol="BADUSDT", funding_rate=Decimal("0.01")))
    ranked = MarketScreener().rank(rows, 5)
    assert len(ranked) == 5
    assert all(item.symbol != "BADUSDT" for item in ranked)
    assert ranked[0].score >= ranked[-1].score


def test_screener_honors_configured_entry_trigger() -> None:
    pullback = snapshot(breakout_15m=0, pullback_15m=1)
    assert MarketScreener(entry_trigger="pullback_only").eligible(pullback)[0] is True
    eligible, reasons = MarketScreener(entry_trigger="breakout_only").eligible(pullback)
    assert eligible is False
    assert "no_aligned_entry_trigger" in reasons


@pytest.mark.asyncio
async def test_cycle_status_marks_partial_execution_failure_before_executed() -> None:
    class Redis:
        def __init__(self) -> None:
            self.payload: dict[str, object] | None = None

        async def set(self, key: str, value: str, *, ex: int) -> None:
            assert key == "trading-cycle:last-status"
            assert ex == 86_400
            import json

            self.payload = json.loads(value)

    cycle = TradingCycle.__new__(TradingCycle)
    cycle.redis = Redis()
    result = CycleResult(
        executed=1,
        failed=True,
        detail="组合动作执行失败: HEMIUSDT",
    )

    await cycle._finish_cycle(result, datetime.now(UTC))

    assert cycle.redis.payload is not None
    assert cycle.redis.payload["state"] == "FAILED"
    assert cycle.redis.payload["failed"] is True


@pytest.mark.asyncio
async def test_cycle_status_marks_valid_no_action_as_completed() -> None:
    class Redis:
        def __init__(self) -> None:
            self.payload: dict[str, object] | None = None

        async def set(self, key: str, value: str, *, ex: int) -> None:
            assert key == "trading-cycle:last-status"
            assert ex == 86_400
            import json

            self.payload = json.loads(value)

    cycle = TradingCycle.__new__(TradingCycle)
    cycle.redis = Redis()
    result = CycleResult(
        candidates=17,
        signals=3,
        approved=0,
        no_action=True,
        detail="组合决策完成：RANGING，无调仓动作，风险预算 0 USDT",
    )

    await cycle._finish_cycle(result, datetime.now(UTC))

    assert cycle.redis.payload is not None
    assert cycle.redis.payload["state"] == "COMPLETED"
    assert cycle.redis.payload["no_action"] is True
    assert cycle.redis.payload["risk_rejected"] is False


def test_portfolio_screener_keeps_safe_watchlist_without_setup_trigger() -> None:
    waiting = snapshot(breakout_15m=0, pullback_15m=0)
    strict, _ = MarketScreener().eligible(waiting)
    assert strict is False
    assert MarketScreener().portfolio_eligible(waiting) is True
    ranked = MarketScreener().rank_portfolio([waiting])
    assert [item.symbol for item in ranked] == [waiting.symbol]


@pytest.mark.asyncio
async def test_model_reviews_execute_close_partial_then_tighten_in_priority_order() -> None:
    first = position(
        position_id="position-close", symbol="BTCUSDT", quantity=Decimal("1")
    )
    second = position(
        position_id="position-partial", symbol="ETHUSDT", quantity=Decimal("1")
    )
    third = position(
        position_id="position-tighten", symbol="SOLUSDT", quantity=Decimal("1")
    )
    events: list[tuple[str, str, Decimal | None]] = []

    class Exchange:
        async def best_entry_price(self, symbol: str, side: str) -> Decimal:
            del side
            events.append(("price", symbol, None))
            return Decimal("100")

        async def get_filters(self, symbol: str) -> ExchangeFilters:
            del symbol
            return ExchangeFilters(
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.1"),
                min_quantity=Decimal("0.1"),
                min_notional=Decimal("5"),
                market_step_size=Decimal("0.1"),
                market_min_quantity=Decimal("0.1"),
            )

        async def tighten_stop(self, position: object, new_stop: Decimal) -> str:
            events.append(("tighten", position.symbol, new_stop))
            return "tighten-order"

        async def get_positions(self) -> list[object]:
            return []

    class Repository:
        async def save_orders(self, orders: list[object]) -> None:
            events.append(("save", str(len(orders)), None))

        async def hydrate_positions(self, positions: list[object]) -> list[object]:
            return positions

        async def sync_positions(self, positions: list[object]) -> None:
            del positions

    class Exits:
        async def execute(
            self, position: object, quantity: Decimal, operation_id: str, price: Decimal
        ) -> list[str]:
            del price
            events.append(("exit", operation_id.split("-")[2], quantity))
            return [f"{position.symbol}-exit"]

    cycle = TradingCycle.__new__(TradingCycle)
    cycle.exchange = Exchange()
    cycle.repository = Repository()
    cycle.exits = Exits()
    async def no_halt(symbol: str, error: Exception) -> None:
        del symbol, error

    cycle._halt_execution = no_halt

    reviews = [
        PositionReview(
            position_id=third.position_id,
            symbol=third.symbol,
            side=third.side,
            action=ReviewAction.TIGHTEN_STOP,
            confidence=Decimal("0.9"),
            tightened_stop=Decimal("50.5"),
            rationale="lock profit",
        ),
        PositionReview(
            position_id=second.position_id,
            symbol=second.symbol,
            side=second.side,
            action=ReviewAction.PARTIAL_CLOSE,
            confidence=Decimal("0.9"),
            close_fraction=Decimal("0.25"),
            rationale="momentum extended",
        ),
        PositionReview(
            position_id=first.position_id,
            symbol=first.symbol,
            side=first.side,
            action=ReviewAction.CLOSE,
            confidence=Decimal("0.9"),
            rationale="thesis invalid",
        ),
    ]

    assert await cycle._process_reviews(reviews, [first, second, third]) is True
    assert [event[0] for event in events if event[0] in {"exit", "tighten"}] == [
        "exit",
        "exit",
        "tighten",
    ]
    action_events = [event for event in events if event[0] in {"exit", "tighten"}]
    assert action_events[0][1] == "close"
    assert action_events[1][1] == "partial_close"
    assert action_events[1][2] == Decimal("0.2")


@pytest.mark.asyncio
async def test_model_review_stop_failure_keeps_entries_enabled_when_hard_stop_exists() -> None:
    current = position(
        position_id="position-tighten",
        symbol="SOLUSDT",
        stop_price=Decimal("49"),
        mark_price=Decimal("51"),
        protected=True,
    )
    halted: list[str] = []

    class Exchange:
        async def tighten_stop(self, current_position, new_stop):
            del current_position, new_stop
            raise ExchangeError(
                "400 [-2021]: Order would immediately trigger.",
                code=-2021,
                http_status=400,
            )

    class Repository:
        async def save_orders(self, orders):
            raise AssertionError(f"failed adjustment must not save orders: {orders}")

    cycle = TradingCycle.__new__(TradingCycle)
    cycle.exchange = Exchange()
    cycle.repository = Repository()

    async def halt(symbol: str, error: Exception) -> None:
        del error
        halted.append(symbol)

    cycle._halt_execution = halt
    review = PositionReview(
        position_id=current.position_id,
        symbol=current.symbol,
        side=current.side,
        action=ReviewAction.TIGHTEN_STOP,
        confidence=Decimal("0.9"),
        tightened_stop=Decimal("50"),
        rationale="lock profit",
    )

    assert await cycle._process_reviews([review], [current]) is True
    assert halted == []
