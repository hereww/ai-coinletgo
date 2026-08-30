from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from trading_system.domain.enums import (
    PortfolioPlanActionType,
    PortfolioPlanStatus,
    PortfolioTargetSide,
    PositionSide,
    SystemMode,
)
from trading_system.domain.models import (
    AccountState,
    ExchangeFilters,
    MarketSnapshot,
    PortfolioAllocation,
    PortfolioDecision,
    PortfolioPlan,
    PortfolioPlanAction,
    PositionState,
    RiskLimits,
)


class PortfolioCompiler:
    """Compile a bounded portfolio intent into deterministic target-position actions.

    The compiler never trusts a requested quantity or leverage from the model.  It
    works only with normalized risk shares and emits actions that are safe to pass
    to the execution layer.  Invalid allocations are retained as REJECTED actions
    so the console can explain the difference between model intent and execution.
    """

    def compile(
        self,
        decision: PortfolioDecision,
        *,
        snapshots: dict[str, MarketSnapshot],
        account: AccountState,
        positions: list[PositionState],
        filters: dict[str, ExchangeFilters],
        limits: RiskLimits,
        mode: SystemMode,
        correlations: dict[tuple[str, str], Decimal] | None = None,
        last_rebalance_at: datetime | None = None,
        cooldown_minutes: int = 0,
        now: datetime | None = None,
    ) -> PortfolioPlan:
        now = now or datetime.now(UTC)
        capital_base = min(account.equity, limits.capital_limit_usdt)
        risk_cap = (
            capital_base
            * limits.portfolio_risk_pct
            * decision.portfolio_risk_budget_fraction
        )
        plan = PortfolioPlan(
            decision_id=decision.decision_id,
            status=PortfolioPlanStatus.REJECTED,
            capital_base=capital_base,
            risk_cap_usdt=risk_cap,
        )
        if decision.expires_at <= now:
            plan.reasons.append("portfolio_decision_expired")
            return plan
        # A paused or risk-halted testnet may still need to reduce exposure or
        # tighten protection.  Only the reconciliation boundary and the live
        # lock are fail-closed for the whole plan.  Entries/adds are filtered
        # below when the mode is PAUSED or RISK_HALTED.
        if mode in {SystemMode.LIVE_LOCKED, SystemMode.RECONCILIATION_REQUIRED}:
            plan.reasons.append("system_mode_disallows_entries")
            return plan
        risk_increase_blocked = mode in {SystemMode.PAUSED, SystemMode.RISK_HALTED}

        current_by_symbol = {item.symbol: item for item in positions}
        allocations = {item.symbol: item for item in decision.allocations}
        missing_positions = sorted(set(current_by_symbol) - set(allocations))
        if missing_positions:
            plan.reasons.extend(
                [f"missing_existing_position_allocation:{symbol}" for symbol in missing_positions]
            )
            return plan

        missing_filters = sorted(
            symbol
            for symbol, allocation in allocations.items()
            if allocation.target_side != PortfolioTargetSide.FLAT and symbol not in filters
        )
        if missing_filters:
            plan.reasons.extend(
                f"exchange_filters_missing:{symbol}" for symbol in missing_filters
            )
            return plan

        plan.requested_risk_usdt = sum(
            (
                risk_cap * allocation.allocation_fraction
                for allocation in decision.allocations
                if allocation.target_side != PortfolioTargetSide.FLAT
            ),
            Decimal("0"),
        )
        projected = dict(current_by_symbol)
        reductions: list[PortfolioPlanAction] = []
        increases: list[tuple[PortfolioAllocation, PortfolioPlanAction]] = []

        # Existing positions must be dealt with first.  This releases risk/margin
        # before any new exposure is considered and prevents a same-cycle reversal.
        for symbol, position in current_by_symbol.items():
            allocation = allocations[symbol]
            snapshot = snapshots.get(symbol)
            action = self._existing_action(
                allocation,
                position,
                snapshot,
                filters[symbol],
                risk_cap,
                limits,
            )
            if action.action in {
                PortfolioPlanActionType.CLOSE,
                PortfolioPlanActionType.REDUCE,
                PortfolioPlanActionType.TIGHTEN_STOP,
                PortfolioPlanActionType.REJECTED,
                PortfolioPlanActionType.HOLD,
            }:
                reductions.append(action)
                if action.action == PortfolioPlanActionType.CLOSE:
                    projected.pop(symbol, None)
                elif action.action == PortfolioPlanActionType.REDUCE:
                    projected[symbol] = position.model_copy(
                        update={
                            "quantity": action.target_quantity,
                            "initial_risk_usdt": action.target_risk_usdt,
                        }
                    )
            elif action.action == PortfolioPlanActionType.ADD:
                increases.append((allocation, action))

        # New candidates do not need to be listed.  A non-flat allocation for a
        # symbol with no open position is an OPEN candidate.
        for allocation in decision.allocations:
            if allocation.symbol in current_by_symbol:
                continue
            if allocation.target_side == PortfolioTargetSide.FLAT:
                continue
            snapshot = snapshots.get(allocation.symbol)
            if snapshot is None:
                reductions.append(self._rejected(allocation, "allocation_symbol_not_in_candidates"))
                continue
            action = self._new_action(
                allocation, snapshot, filters[allocation.symbol], risk_cap, limits
            )
            if action.action == PortfolioPlanActionType.OPEN:
                increases.append((allocation, action))
            else:
                reductions.append(action)

        correlations = correlations or {}
        cooldown_active = bool(
            last_rebalance_at is not None
            and cooldown_minutes > 0
            and now < last_rebalance_at
            + timedelta(minutes=cooldown_minutes)
        )
        approved_risk = sum(
            item.target_risk_usdt
            for item in reductions
            if item.action
            in {
                PortfolioPlanActionType.HOLD,
                PortfolioPlanActionType.ADD,
                PortfolioPlanActionType.REDUCE,
                PortfolioPlanActionType.TIGHTEN_STOP,
            }
        )

        # A stable deterministic priority order keeps compiles reproducible.
        for allocation, action in sorted(
            increases, key=lambda item: (item[0].priority, -item[0].confidence, item[0].symbol)
        ):
            validation = (
                "system_mode_disallows_risk_increase"
                if risk_increase_blocked
                else self._validate_increase(
                    action,
                    allocation,
                    projected,
                    snapshots,
                    limits,
                    account,
                    correlations,
                )
            )
            if validation is None and approved_risk + action.target_risk_usdt > risk_cap:
                validation = "portfolio_risk_capacity_exhausted"
            if validation is None and cooldown_active:
                validation = "rebalance_cooldown_active"
            if validation is not None:
                reductions.append(self._rejected(allocation, validation, source=action))
                continue
            if action.action == PortfolioPlanActionType.OPEN:
                if action.side is None:
                    reductions.append(
                        self._rejected(
                            allocation,
                            "invalid_portfolio_action",
                            source=action,
                        )
                    )
                    continue
                projected[action.symbol] = PositionState(
                    position_id=f"planned-{action.symbol}-{action.side.value}",
                    symbol=action.symbol,
                    side=action.side,
                    quantity=action.target_quantity,
                    entry_price=self._reference_price(action, snapshots[action.symbol]),
                    mark_price=self._reference_price(action, snapshots[action.symbol]),
                    stop_price=action.stop_price or Decimal("0.00000001"),
                    initial_risk_usdt=action.target_risk_usdt,
                )
            else:
                previous = projected[action.symbol]
                projected[action.symbol] = previous.model_copy(
                    update={
                        "quantity": action.target_quantity,
                        "initial_risk_usdt": action.target_risk_usdt,
                    }
                )
            approved_risk += action.target_risk_usdt
            reductions.append(action)

        plan.actions = [
            item.model_copy(update={"action_sequence": sequence})
            for sequence, item in enumerate(self._ordered(reductions), start=1)
        ]
        plan.approved_risk_usdt = min(
            risk_cap,
            sum(
                (item.initial_risk_usdt for item in projected.values()),
                Decimal("0"),
            ),
        )
        rejected = any(item.action == PortfolioPlanActionType.REJECTED for item in plan.actions)
        approved = any(item.action != PortfolioPlanActionType.REJECTED for item in plan.actions)
        plan.status = (
            PortfolioPlanStatus.PARTIALLY_APPROVED
            if approved and rejected
            else PortfolioPlanStatus.APPROVED
            if approved
            else PortfolioPlanStatus.NO_ACTION
            if not plan.actions and not plan.reasons
            else PortfolioPlanStatus.REJECTED
        )
        if rejected:
            plan.reasons.append("one_or_more_allocations_rejected")
        return plan

    def _existing_action(
        self,
        allocation: PortfolioAllocation,
        position: PositionState,
        snapshot: MarketSnapshot | None,
        exchange_filters: ExchangeFilters,
        risk_cap: Decimal,
        limits: RiskLimits,
    ) -> PortfolioPlanAction:
        current_risk = position.initial_risk_usdt
        if allocation.target_side == PortfolioTargetSide.FLAT:
            return self._action(
                allocation,
                PortfolioPlanActionType.CLOSE,
                position,
                target_quantity=Decimal("0"),
                target_risk=Decimal("0"),
                reasons=["model_target_is_flat"],
            )
        desired_side = PositionSide(allocation.target_side.value)
        if desired_side != position.side:
            return self._action(
                allocation,
                PortfolioPlanActionType.CLOSE,
                position,
                target_quantity=Decimal("0"),
                target_risk=Decimal("0"),
                reasons=["same_cycle_reversal_deferred"],
            )
        if snapshot is None:
            return self._rejected(allocation, "market_snapshot_missing", position)
        stop = self._effective_stop(allocation, position, snapshot)
        if stop is None:
            return self._rejected(allocation, "stop_widening_or_invalid", position)
        price = snapshot.mid_price
        distance = abs(price - stop)
        if distance <= 0:
            return self._rejected(allocation, "rounded_stop_invalid", position)
        target_risk = risk_cap * allocation.allocation_fraction
        target_quantity = self._round_down(target_risk / distance, exchange_filters.step_size)
        if exchange_filters.max_quantity is not None:
            target_quantity = min(target_quantity, exchange_filters.max_quantity)
        if target_quantity <= 0:
            return self._action(
                allocation,
                PortfolioPlanActionType.CLOSE,
                position,
                target_quantity=Decimal("0"),
                target_risk=Decimal("0"),
                stop=stop,
                reasons=["target_quantity_below_minimum"],
            )
        delta_risk = abs(target_risk - current_risk)
        deadband = risk_cap * limits.portfolio_rebalance_deadband_fraction
        tighten = stop != position.stop_price
        # A portfolio decision may keep quantity unchanged while moving the
        # final take-profit target.  Treat that as a protection update so the
        # execution layer re-compiles both TP tranches on the exchange.
        target_changed = (
            allocation.target_price is not None
            and (
                position.tp2_price is None
                or allocation.target_price != position.tp2_price
            )
        )
        if (
            target_quantity == position.quantity
            or (
                target_risk != 0
                and current_risk <= risk_cap
                and delta_risk < deadband
            )
        ):
            if tighten or target_changed:
                reasons = []
                if tighten:
                    reasons.append("hard_stop_tightened")
                if target_changed:
                    reasons.append("take_profit_updated")
                return self._action(
                    allocation,
                    PortfolioPlanActionType.TIGHTEN_STOP,
                    position,
                    target_quantity=position.quantity,
                    target_risk=current_risk,
                    stop=stop,
                    reasons=reasons,
                )
            return self._action(
                allocation,
                PortfolioPlanActionType.HOLD,
                position,
                target_quantity=position.quantity,
                target_risk=current_risk,
                stop=stop,
                reasons=["target_inside_rebalance_deadband"],
            )
        if target_quantity < position.quantity:
            return self._action(
                allocation,
                PortfolioPlanActionType.REDUCE,
                position,
                target_quantity=target_quantity,
                target_risk=target_risk,
                stop=stop,
                reasons=["target_risk_reduced"],
            )
        return self._action(
            allocation,
            PortfolioPlanActionType.ADD,
            position,
            target_quantity=target_quantity,
            target_risk=target_risk,
            stop=stop,
            reasons=["target_risk_increased"],
        )

    def _new_action(
        self,
        allocation: PortfolioAllocation,
        snapshot: MarketSnapshot,
        exchange_filters: ExchangeFilters,
        risk_cap: Decimal,
        limits: RiskLimits,
    ) -> PortfolioPlanAction:
        if allocation.confidence < limits.min_confidence:
            return self._rejected(allocation, "confidence_below_minimum")
        if not self._direction_allowed(allocation.target_side, limits):
            return self._rejected(allocation, "entry_direction_not_allowed")
        if not self._valid_geometry(allocation, snapshot):
            return self._rejected(allocation, "invalid_price_geometry")
        entry = self._allocation_entry(allocation, snapshot)
        assert allocation.stop_price is not None
        assert allocation.target_price is not None
        distance = abs(entry - allocation.stop_price)
        if exchange_filters.max_quantity is not None:
            quantity_limit = exchange_filters.max_quantity
        else:
            quantity_limit = None
        stop_atr = distance / snapshot.atr_15m
        if stop_atr < limits.min_stop_atr:
            return self._rejected(allocation, "stop_too_close")
        if stop_atr > limits.max_stop_atr:
            return self._rejected(allocation, "stop_too_far")
        net_rr = self._net_reward_risk(entry, allocation, snapshot)
        if net_rr < limits.min_net_reward_risk:
            return self._rejected(allocation, "net_reward_risk_below_minimum")
        target_risk = risk_cap * allocation.allocation_fraction
        quantity = self._round_down(target_risk / distance, exchange_filters.step_size)
        if quantity_limit is not None:
            quantity = min(quantity, self._round_down(quantity_limit, exchange_filters.step_size))
        if quantity < exchange_filters.min_quantity:
            return self._rejected(allocation, "quantity_below_exchange_minimum")
        if quantity * entry < exchange_filters.min_notional:
            return self._rejected(allocation, "notional_below_exchange_minimum")
        return PortfolioPlanAction(
            action_id=allocation.allocation_id,
            allocation_id=allocation.allocation_id,
            symbol=allocation.symbol,
            action=PortfolioPlanActionType.OPEN,
            side=PositionSide(allocation.target_side.value),
            target_quantity=quantity,
            quantity_delta=quantity,
            target_risk_usdt=quantity * distance,
            entry_min=allocation.entry_min,
            entry_max=allocation.entry_max,
            stop_price=allocation.stop_price,
            target_price=allocation.target_price,
            confidence=allocation.confidence,
            priority=allocation.priority,
            reasons=["portfolio_target_approved"],
        )

    def _validate_increase(
        self,
        action: PortfolioPlanAction,
        allocation: PortfolioAllocation,
        projected: dict[str, PositionState],
        snapshots: dict[str, MarketSnapshot],
        limits: RiskLimits,
        account: AccountState,
        correlations: dict[tuple[str, str], Decimal],
    ) -> str | None:
        if action.side is None or action.stop_price is None:
            return "invalid_portfolio_action"
        if (
            action.action == PortfolioPlanActionType.ADD
            and allocation.confidence < limits.min_confidence
        ):
            return "confidence_below_minimum"
        if action.action == PortfolioPlanActionType.ADD and not self._valid_geometry(
            allocation,
            snapshots[action.symbol],
            allow_existing=True,
        ):
            return "invalid_price_geometry"
        if action.action == PortfolioPlanActionType.ADD:
            snapshot = snapshots[action.symbol]
            reference = self._reference_price(action, snapshot)
            stop_distance = abs(reference - action.stop_price)
            stop_atr = stop_distance / snapshot.atr_15m
            if stop_atr < limits.min_stop_atr:
                return "stop_too_close"
            if stop_atr > limits.max_stop_atr:
                return "stop_too_far"
            if allocation.target_price is None:
                return "invalid_price_geometry"
            reward = abs(allocation.target_price - reference)
            costs = reference * Decimal("0.0015") + reference * abs(snapshot.funding_rate)
            net_rr = max(Decimal("0"), reward - costs) / (stop_distance + costs)
            if net_rr < limits.min_net_reward_risk:
                return "net_reward_risk_below_minimum"
        position_count = len(projected) + (
            1 if action.action == PortfolioPlanActionType.OPEN else 0
        )
        if position_count > limits.max_positions:
            return "position_count_limit_reached"
        same_direction = sum(item.side == action.side for item in projected.values())
        if (
            action.action == PortfolioPlanActionType.OPEN
            and same_direction >= limits.max_same_direction
        ):
            return "same_direction_limit_reached"
        for symbol in projected:
            if symbol == action.symbol:
                continue
            correlation = abs(self._correlation(action.symbol, symbol, correlations))
            if correlation > limits.correlation_limit:
                return f"correlated_with_{symbol}"
        projected_margin = sum(
            (
                item.quantity * item.mark_price / Decimal(limits.max_leverage)
                for item in projected.values()
            ),
            Decimal("0"),
        )
        if action.action == PortfolioPlanActionType.OPEN:
            projected_margin += (
                self._reference_price(action, snapshots[action.symbol])
                * action.target_quantity
                / Decimal(limits.max_leverage)
            )
        else:
            existing = projected[action.symbol]
            projected_margin += (
                self._reference_price(action, snapshots[action.symbol])
                * (action.target_quantity - existing.quantity)
                / Decimal(limits.max_leverage)
            )
        capital_base = min(account.equity, limits.capital_limit_usdt)
        if projected_margin > capital_base * limits.max_margin_pct:
            return "margin_limit_reached"
        # Check free balance independently from the configured margin ratio.
        # This lets reductions earlier in the same plan release capacity before
        # a later increase and prevents avoidable exchange-side rejections.
        incremental_margin = max(
            Decimal("0"), projected_margin - account.total_margin_used
        )
        if incremental_margin > account.available_balance:
            return "available_balance_insufficient"
        return None

    @staticmethod
    def _ordered(actions: list[PortfolioPlanAction]) -> list[PortfolioPlanAction]:
        order = {
            PortfolioPlanActionType.CLOSE: 0,
            PortfolioPlanActionType.REDUCE: 1,
            PortfolioPlanActionType.TIGHTEN_STOP: 2,
            PortfolioPlanActionType.HOLD: 3,
            PortfolioPlanActionType.ADD: 4,
            PortfolioPlanActionType.OPEN: 5,
            PortfolioPlanActionType.REJECTED: 6,
        }
        return sorted(actions, key=lambda item: (order[item.action], item.priority, item.symbol))

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _direction_allowed(side: PortfolioTargetSide, limits: RiskLimits) -> bool:
        return not (
            (limits.entry_direction == "long_only" and side == PortfolioTargetSide.SHORT)
            or (limits.entry_direction == "short_only" and side == PortfolioTargetSide.LONG)
        )

    @staticmethod
    def _correlation(
        first: str, second: str, correlations: dict[tuple[str, str], Decimal]
    ) -> Decimal:
        return correlations.get((first, second), correlations.get((second, first), Decimal("0")))

    @staticmethod
    def _allocation_entry(allocation: PortfolioAllocation, snapshot: MarketSnapshot) -> Decimal:
        if allocation.entry_min is None or allocation.entry_max is None:
            return snapshot.mid_price
        midpoint = (allocation.entry_min + allocation.entry_max) / Decimal("2")
        if allocation.entry_min <= snapshot.mid_price <= allocation.entry_max:
            return snapshot.mid_price
        return midpoint

    def _valid_geometry(
        self,
        allocation: PortfolioAllocation,
        snapshot: MarketSnapshot,
        *,
        allow_existing: bool = False,
    ) -> bool:
        if allocation.stop_price is None:
            return False
        if allocation.entry_min is None or allocation.entry_max is None:
            return allow_existing
        if allocation.target_price is None:
            return False
        entry = self._allocation_entry(allocation, snapshot)
        if allocation.target_side == PortfolioTargetSide.LONG:
            return allocation.stop_price < entry < allocation.target_price
        if allocation.target_side == PortfolioTargetSide.SHORT:
            return allocation.target_price < entry < allocation.stop_price
        return False

    @staticmethod
    def _effective_stop(
        allocation: PortfolioAllocation,
        position: PositionState,
        snapshot: MarketSnapshot,
    ) -> Decimal | None:
        proposed = allocation.stop_price
        if proposed is None:
            return position.stop_price
        if position.side == PositionSide.LONG:
            if proposed < position.stop_price or proposed >= snapshot.mid_price:
                return None
        elif proposed > position.stop_price or proposed <= snapshot.mid_price:
            return None
        return proposed

    def _net_reward_risk(
        self, entry: Decimal, allocation: PortfolioAllocation, snapshot: MarketSnapshot
    ) -> Decimal:
        assert allocation.stop_price is not None
        assert allocation.target_price is not None
        distance = abs(entry - allocation.stop_price)
        reward = abs(allocation.target_price - entry)
        costs = entry * (Decimal("0.0015"))
        funding = entry * abs(snapshot.funding_rate)
        return max(Decimal("0"), reward - costs - funding) / (distance + costs + funding)

    @staticmethod
    def _reference_price(action: PortfolioPlanAction, snapshot: MarketSnapshot) -> Decimal:
        if action.entry_min is not None and action.entry_max is not None:
            return min(max(snapshot.mid_price, action.entry_min), action.entry_max)
        return snapshot.mid_price

    def _action(
        self,
        allocation: PortfolioAllocation,
        action_type: PortfolioPlanActionType,
        position: PositionState,
        *,
        target_quantity: Decimal,
        target_risk: Decimal,
        stop: Decimal | None = None,
        reasons: list[str],
    ) -> PortfolioPlanAction:
        return PortfolioPlanAction(
            action_id=allocation.allocation_id,
            allocation_id=allocation.allocation_id,
            symbol=position.symbol,
            action=action_type,
            side=position.side,
            current_quantity=position.quantity,
            target_quantity=target_quantity,
            quantity_delta=abs(target_quantity - position.quantity),
            target_risk_usdt=target_risk,
            entry_min=allocation.entry_min,
            entry_max=allocation.entry_max,
            stop_price=stop or position.stop_price,
            target_price=allocation.target_price,
            confidence=allocation.confidence,
            priority=allocation.priority,
            reasons=reasons,
        )

    def _rejected(
        self,
        allocation: PortfolioAllocation,
        reason: str,
        position: PositionState | None = None,
        source: PortfolioPlanAction | None = None,
    ) -> PortfolioPlanAction:
        return PortfolioPlanAction(
            action_id=allocation.allocation_id,
            allocation_id=allocation.allocation_id,
            symbol=allocation.symbol,
            action=PortfolioPlanActionType.REJECTED,
            side=(source.side if source is not None else position.side if position else None),
            current_quantity=(
                source.current_quantity
                if source is not None
                else position.quantity
                if position
                else Decimal("0")
            ),
            target_quantity=(source.target_quantity if source is not None else Decimal("0")),
            quantity_delta=(source.quantity_delta if source is not None else Decimal("0")),
            target_risk_usdt=(source.target_risk_usdt if source is not None else Decimal("0")),
            entry_min=allocation.entry_min,
            entry_max=allocation.entry_max,
            stop_price=allocation.stop_price,
            target_price=allocation.target_price,
            confidence=allocation.confidence,
            priority=allocation.priority,
            reasons=[reason],
        )
