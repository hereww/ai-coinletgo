from __future__ import annotations

from decimal import Decimal

from trading_system.domain.models import MarketSnapshot


class MarketScreener:
    def __init__(
        self,
        *,
        max_spread_pct: Decimal = Decimal("0.0015"),
        max_abs_funding_rate: Decimal = Decimal("0.001"),
        max_abs_basis_pct: Decimal = Decimal("0.01"),
        min_book_depth_usdt: Decimal = Decimal("50000"),
        min_listing_days: int = 90,
        max_volatility_percentile: Decimal = Decimal("0.99"),
        entry_trigger: str = "breakout_or_pullback",
        trend_adx_min: Decimal = Decimal("20"),
        volatility_soft_limit_percentile: Decimal = Decimal("0.75"),
        volatility_hard_limit_percentile: Decimal = Decimal("0.90"),
    ) -> None:
        self.max_spread_pct = max_spread_pct
        self.max_abs_funding_rate = max_abs_funding_rate
        self.max_abs_basis_pct = max_abs_basis_pct
        self.min_book_depth_usdt = min_book_depth_usdt
        self.min_listing_days = min_listing_days
        self.max_volatility_percentile = max_volatility_percentile
        self.entry_trigger = entry_trigger
        self.trend_adx_min = trend_adx_min
        self.volatility_soft_limit_percentile = volatility_soft_limit_percentile
        self.volatility_hard_limit_percentile = volatility_hard_limit_percentile

    def eligible(self, snapshot: MarketSnapshot) -> tuple[bool, list[str]]:
        reasons = self._market_reasons(snapshot) + self._setup_reasons(snapshot)
        return not reasons, reasons

    def portfolio_eligible(self, snapshot: MarketSnapshot) -> bool:
        """Return whether a snapshot is safe enough to show to Portfolio-v1.

        Portfolio-v1 deliberately lets the model rank the complete safe
        watchlist.  Trend/trigger checks remain visible in the snapshot and
        are still enforced by the model contract and local risk compiler, but
        they no longer prevent the model from seeing every otherwise-tradable
        market.  Legacy signal-v1 continues to use ``eligible`` unchanged.
        """
        return not self._market_reasons(snapshot)

    def rank_portfolio(
        self, snapshots: list[MarketSnapshot], limit: int | None = None
    ) -> list[MarketSnapshot]:
        candidates: list[MarketSnapshot] = []
        for snapshot in snapshots:
            if not self.portfolio_eligible(snapshot):
                continue
            snapshot.score = self.score(snapshot)
            candidates.append(snapshot)
        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
        return ranked if limit is None else ranked[:limit]

    def _market_reasons(self, snapshot: MarketSnapshot) -> list[str]:
        reasons: list[str] = []
        if snapshot.status != "TRADING":
            reasons.append("symbol_not_trading")
        if snapshot.listing_days < self.min_listing_days:
            reasons.append("listing_too_recent")
        if snapshot.spread_pct > self.max_spread_pct:
            reasons.append("spread_too_wide")
        if abs(snapshot.funding_rate) > self.max_abs_funding_rate:
            reasons.append("funding_rate_abnormal")
        if abs(snapshot.basis_pct) > self.max_abs_basis_pct:
            reasons.append("basis_abnormal")
        if snapshot.book_depth_usdt < self.min_book_depth_usdt:
            reasons.append("insufficient_book_depth")
        if snapshot.volatility_percentile > self.max_volatility_percentile:
            reasons.append("extreme_volatility")
        if snapshot.market_regime == "VOLATILE":
            reasons.append("volatile_regime")
        elif snapshot.market_regime == "UNCERTAIN":
            reasons.append("uncertain_regime")
        return reasons

    def _setup_reasons(self, snapshot: MarketSnapshot) -> list[str]:
        reasons: list[str] = []
        if snapshot.trend_1h == 0 or snapshot.trend_1h != snapshot.trend_4h:
            reasons.append("trend_not_aligned")
        if snapshot.adx_1h < self.trend_adx_min:
            reasons.append("trend_strength_below_minimum")
        if snapshot.market_regime != "TRENDING":
            reasons.append("market_regime_not_trending")
        triggers = {
            "breakout_only": snapshot.breakout_15m,
            "pullback_only": snapshot.pullback_15m,
            "breakout_or_pullback": snapshot.breakout_15m
            if snapshot.breakout_15m == snapshot.trend_1h
            else snapshot.pullback_15m,
        }
        trigger = triggers[self.entry_trigger]
        if trigger == 0 or trigger != snapshot.trend_1h:
            reasons.append("no_aligned_entry_trigger")
        return reasons

    def score(self, snapshot: MarketSnapshot) -> Decimal:
        trend_score = Decimal("1") if snapshot.trend_1h == snapshot.trend_4h != 0 else 0
        trigger_values = {
            "breakout_only": snapshot.breakout_15m,
            "pullback_only": snapshot.pullback_15m,
            "breakout_or_pullback": snapshot.breakout_15m
            if snapshot.breakout_15m == snapshot.trend_1h
            else snapshot.pullback_15m,
        }
        trigger_score = (
            Decimal("1")
            if trigger_values[self.entry_trigger] == snapshot.trend_1h
            else Decimal("0")
        )
        adx_score = min(snapshot.adx_1h / Decimal("40"), Decimal("1"))
        volume_score = max(
            Decimal("0"), min((snapshot.volume_zscore + Decimal("1")) / Decimal("3"), Decimal("1"))
        )
        oi_score = max(
            Decimal("0"),
            min(abs(snapshot.open_interest_change_pct) / Decimal("0.05"), Decimal("1")),
        )
        cost_score = max(Decimal("0"), Decimal("1") - snapshot.spread_pct / self.max_spread_pct)
        volatility_score = Decimal("1") - abs(snapshot.volatility_percentile - Decimal("0.6"))
        return (
            trend_score * Decimal("0.25")
            + trigger_score * Decimal("0.20")
            + adx_score * Decimal("0.15")
            + volume_score * Decimal("0.15")
            + oi_score * Decimal("0.10")
            + cost_score * Decimal("0.10")
            + volatility_score * Decimal("0.05")
        )

    def rank(self, snapshots: list[MarketSnapshot], limit: int = 5) -> list[MarketSnapshot]:
        candidates: list[MarketSnapshot] = []
        for snapshot in snapshots:
            eligible, _ = self.eligible(snapshot)
            if not eligible:
                continue
            snapshot.score = self.score(snapshot)
            candidates.append(snapshot)
        return sorted(candidates, key=lambda item: item.score, reverse=True)[:limit]
