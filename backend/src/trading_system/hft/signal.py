from __future__ import annotations

from decimal import Decimal

from trading_system.hft.models import HftSignal
from trading_system.hft.order_book import DiffOrderBook


class HftSignalEngine:
    def __init__(
        self,
        *,
        imbalance_threshold: Decimal,
        max_spread_pct: Decimal,
        min_depth_usdt: Decimal,
    ) -> None:
        self.imbalance_threshold = imbalance_threshold
        self.max_spread_pct = max_spread_pct
        self.min_depth_usdt = min_depth_usdt

    def update_limits(
        self,
        *,
        imbalance_threshold: Decimal,
        max_spread_pct: Decimal,
        min_depth_usdt: Decimal,
    ) -> None:
        self.imbalance_threshold = imbalance_threshold
        self.max_spread_pct = max_spread_pct
        self.min_depth_usdt = min_depth_usdt

    def evaluate(self, book: DiffOrderBook) -> HftSignal:
        metrics = book.metrics()
        if metrics.spread_pct > self.max_spread_pct:
            return HftSignal(
                symbol=metrics.symbol,
                action="HOLD",
                score=Decimal("0"),
                reason="spread_too_wide",
                book=metrics,
            )
        if metrics.min_depth_usdt < self.min_depth_usdt:
            return HftSignal(
                symbol=metrics.symbol,
                action="HOLD",
                score=Decimal("0"),
                reason="insufficient_two_sided_depth",
                book=metrics,
            )
        if (
            metrics.imbalance >= self.imbalance_threshold
            and metrics.microprice > metrics.mid_price
        ):
            return HftSignal(
                symbol=metrics.symbol,
                action="BUY",
                score=min(Decimal("1"), abs(metrics.imbalance)),
                reason="bid_imbalance_microprice_up",
                book=metrics,
            )
        if (
            metrics.imbalance <= -self.imbalance_threshold
            and metrics.microprice < metrics.mid_price
        ):
            return HftSignal(
                symbol=metrics.symbol,
                action="SELL",
                score=min(Decimal("1"), abs(metrics.imbalance)),
                reason="ask_imbalance_microprice_down",
                book=metrics,
            )
        return HftSignal(
            symbol=metrics.symbol,
            action="HOLD",
            score=abs(metrics.imbalance),
            reason="imbalance_below_threshold",
            book=metrics,
        )
