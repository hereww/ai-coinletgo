from __future__ import annotations

from decimal import Decimal

from trading_system.domain.enums import PositionSide

TP1_FRACTION = Decimal("0.4")
TP2_FRACTION = Decimal("0.4")
TRAILING_FRACTION = Decimal("0.2")


def manual_atr_exit_prices(
    *,
    side: PositionSide,
    entry_min: Decimal,
    entry_max: Decimal,
    atr: Decimal,
    stop_atr: Decimal,
    target_atr: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return a stop outside the entry band and the configured final target."""

    structural_buffer = atr * Decimal("0.01")
    stop_distance = atr * stop_atr
    target_distance = atr * target_atr
    if side == PositionSide.LONG:
        return (
            min(entry_min - structural_buffer, entry_max - stop_distance),
            entry_max + target_distance,
        )
    return (
        max(entry_max + structural_buffer, entry_min + stop_distance),
        entry_min - target_distance,
    )


def first_take_profit(
    *,
    entry: Decimal,
    stop_price: Decimal,
    final_target: Decimal,
    side: PositionSide,
) -> Decimal:
    """Place TP1 at 1R unless it would collide with the final target."""

    risk = abs(entry - stop_price)
    if risk <= 0:
        raise ValueError("entry stop distance is invalid")
    if side == PositionSide.LONG:
        if final_target <= entry:
            raise ValueError("long final target must be above entry")
        mechanical = entry + risk
        return mechanical if mechanical < final_target else entry + (final_target - entry) / 2
    if final_target >= entry:
        raise ValueError("short final target must be below entry")
    mechanical = entry - risk
    return mechanical if mechanical > final_target else entry + (final_target - entry) / 2


def breakeven_stop(
    entry: Decimal, side: PositionSide, round_trip_fee_rate: Decimal
) -> Decimal:
    fee_buffer = entry * round_trip_fee_rate
    return entry + fee_buffer if side == PositionSide.LONG else entry - fee_buffer


def trailing_stop(
    *,
    current_stop: Decimal,
    side: PositionSide,
    highest: Decimal,
    lowest: Decimal,
    atr: Decimal,
    atr_multiple: Decimal,
) -> Decimal:
    proposed = (
        highest - atr * atr_multiple
        if side == PositionSide.LONG
        else lowest + atr * atr_multiple
    )
    return max(current_stop, proposed) if side == PositionSide.LONG else min(
        current_stop, proposed
    )
