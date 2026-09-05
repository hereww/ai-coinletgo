from __future__ import annotations

import time
from decimal import Decimal

from trading_system.hft.models import HftRiskDecision, HftSignal


class HftRiskManager:
    def __init__(
        self,
        *,
        max_inventory_usdt: Decimal,
        cooldown_seconds: float,
        market_stale_seconds: float,
        max_consecutive_losses: int,
    ) -> None:
        self.max_inventory_usdt = max_inventory_usdt
        self.cooldown_seconds = cooldown_seconds
        self.market_stale_seconds = market_stale_seconds
        self.max_consecutive_losses = max_consecutive_losses
        self.last_trade_monotonic: float | None = None
        self.consecutive_losses = 0
        self.halted = False

    def update_limits(
        self,
        *,
        max_inventory_usdt: Decimal,
        cooldown_seconds: float,
        market_stale_seconds: float,
        max_consecutive_losses: int,
    ) -> None:
        self.max_inventory_usdt = max_inventory_usdt
        self.cooldown_seconds = cooldown_seconds
        self.market_stale_seconds = market_stale_seconds
        self.max_consecutive_losses = max_consecutive_losses
        if self.consecutive_losses < max_consecutive_losses:
            self.halted = False

    def check(
        self,
        signal: HftSignal,
        *,
        inventory_qty: Decimal,
        desired_notional_usdt: Decimal,
        now_monotonic: float | None = None,
    ) -> HftRiskDecision:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        if signal.action == "HOLD":
            return HftRiskDecision(allowed=False, reason="no_actionable_signal")
        if self.halted:
            return HftRiskDecision(allowed=False, reason="consecutive_loss_circuit_breaker")
        if (
            signal.book.received_monotonic > 0
            and now - signal.book.received_monotonic > self.market_stale_seconds
        ):
            return HftRiskDecision(allowed=False, reason="market_data_stale")
        if (
            self.last_trade_monotonic is not None
            and now - self.last_trade_monotonic < self.cooldown_seconds
        ):
            return HftRiskDecision(allowed=False, reason="cooldown_active")
        if desired_notional_usdt <= 0:
            return HftRiskDecision(allowed=False, reason="invalid_order_notional")

        price = signal.book.best_ask if signal.action == "BUY" else signal.book.best_bid
        desired_quantity = desired_notional_usdt / price
        signed_quantity = desired_quantity if signal.action == "BUY" else -desired_quantity
        projected_inventory = inventory_qty + signed_quantity
        projected_notional = abs(projected_inventory * price)
        if projected_notional <= self.max_inventory_usdt:
            return HftRiskDecision(allowed=True, quantity=desired_quantity, reason="risk_passed")

        current_notional = abs(inventory_qty * price)
        same_direction = inventory_qty != 0 and inventory_qty * signed_quantity > 0
        if same_direction:
            allowable_notional = self.max_inventory_usdt - current_notional
        else:
            # Closing an existing inventory is risk-reducing. If the order
            # crosses through flat, only the residual side counts toward the
            # inventory cap.
            allowable_notional = self.max_inventory_usdt + current_notional
        if allowable_notional <= 0:
            return HftRiskDecision(allowed=False, reason="max_inventory_reached")
        reduced_quantity = min(desired_quantity, allowable_notional / price)
        if reduced_quantity <= 0:
            return HftRiskDecision(allowed=False, reason="max_inventory_reached")
        return HftRiskDecision(
            allowed=True,
            quantity=reduced_quantity,
            reason="risk_passed_with_inventory_cap",
        )

    def record_fill(self, *, closed_quantity: Decimal, realized_pnl_usdt: Decimal) -> None:
        if closed_quantity <= 0:
            return
        self.last_trade_monotonic = time.monotonic()
        if realized_pnl_usdt <= 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.halted = True
        else:
            self.consecutive_losses = 0

    def record_opening_fill(self) -> None:
        self.last_trade_monotonic = time.monotonic()
