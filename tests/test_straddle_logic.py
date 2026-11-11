"""Tests for the pure straddle logic (straddle_logic.py).

Covers the assignment's exact numbers: ATM nearest-strike choice, combined
premium, the 1.00% / 2.00% SL/TP boundaries, the duplicate-exit guard, and
partial-fill handling.
"""

from datetime import datetime, timedelta, timezone

import pytest

from straddle_logic import (
    STOP_LOSS,
    TAKE_PROFIT,
    ExitGuard,
    FillTracker,
    OptionLeg,
    combined_premium,
    select_atm_strike,
    select_straddle_legs,
    should_exit,
)

NOW = datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
EXPIRY_NEAR = datetime(2026, 1, 30, 8, tzinfo=timezone.utc)
EXPIRY_FAR = datetime(2026, 2, 27, 8, tzinfo=timezone.utc)


def _legs():
    legs = []
    for expiry in (EXPIRY_NEAR, EXPIRY_FAR):
        for strike in (110_000, 115_000, 120_000, 125_000):
            for kind in ("CALL", "PUT"):
                legs.append(OptionLeg(
                    instrument_id=f"BTC-{strike}-{kind}-{expiry.date()}",
                    strike=float(strike), kind=kind, expiration=expiry,
                ))
    return legs


# --- ATM selection -------------------------------------------------------

def test_atm_strike_is_nearest():
    assert select_atm_strike(119_000, [110_000, 115_000, 120_000, 125_000]) == 120_000
    assert select_atm_strike(112_000, [110_000, 115_000, 120_000, 125_000]) == 110_000


def test_atm_strike_exact_match():
    assert select_atm_strike(120_000, [110_000, 120_000, 125_000]) == 120_000


def test_atm_strike_tie_breaks_to_lower_strike():
    # 100_050 is exactly 50 away from both 100_000 and 100_100.
    assert select_atm_strike(100_050, [100_000, 100_100]) == 100_000


def test_atm_strike_no_strikes_raises():
    with pytest.raises(ValueError):
        select_atm_strike(100_000, [])


def test_select_legs_nearest_expiry_and_same_strike():
    call, put = select_straddle_legs(_legs(), 119_000, now=NOW)
    assert call.strike == 120_000 and put.strike == 120_000
    assert call.kind == "CALL" and put.kind == "PUT"
    assert call.expiration == put.expiration == EXPIRY_NEAR


def test_select_legs_ignores_expired_options():
    legs = [OptionLeg("X", 120_000, "CALL", NOW - timedelta(days=1)),
            OptionLeg("Y", 120_000, "PUT", NOW - timedelta(days=1))]
    with pytest.raises(ValueError):
        select_straddle_legs(legs, 120_000, now=NOW)


def test_select_legs_missing_put_raises():
    legs = [OptionLeg("X", 120_000, "CALL", EXPIRY_NEAR)]
    with pytest.raises(ValueError):
        select_straddle_legs(legs, 120_000, now=NOW)


# --- Premium math + SL/TP -----------------------------------------------

def test_combined_premium():
    assert combined_premium(1000.0, 900.0) == 1900.0


def test_no_exit_inside_band():
    assert should_exit(1900.0, 1900.0) is None       # flat
    assert should_exit(1900.0, 1910.0) is None       # +0.53%
    assert should_exit(1900.0, 1880.0) is None       # -1.05%


def test_stop_loss_boundary_exactly_plus_1pct():
    assert should_exit(1900.0, 1919.0) == STOP_LOSS  # exactly +1.00% triggers
    assert should_exit(1900.0, 1918.99) is None      # just under does not


def test_take_profit_boundary_exactly_minus_2pct():
    assert should_exit(1900.0, 1862.0) == TAKE_PROFIT  # exactly -2.00% triggers
    assert should_exit(1900.0, 1862.01) is None        # just above does not


def test_stop_loss_takes_precedence_if_both_crossed():
    # Cannot happen with one premium, but SL is checked first by design.
    assert should_exit(100.0, 200.0) == STOP_LOSS


# --- Exit guard ----------------------------------------------------------

def test_exit_guard_first_wins():
    guard = ExitGuard()
    assert guard.try_start_exit(STOP_LOSS) is True
    assert guard.try_start_exit(STOP_LOSS) is False  # duplicate blocked
    assert guard.try_start_exit(TAKE_PROFIT) is False
    assert guard.reason == STOP_LOSS


def test_exit_guard_reset_allows_retry_after_rejection():
    guard = ExitGuard()
    assert guard.try_start_exit(STOP_LOSS) is True
    guard.reset()
    assert guard.try_start_exit(TAKE_PROFIT) is True


# --- Fill tracker / partial fills ----------------------------------------

def test_entry_premium_needs_both_legs_full():
    fills = FillTracker(target_qty=1.0)
    fills.record_fill("CALL", 1.0, 1000.0)
    assert fills.entry_premium() is None  # PUT missing — one-legged, not armed
    fills.record_fill("PUT", 1.0, 900.0)
    assert fills.entry_premium() == 1900.0


def test_partial_fills_accumulate_with_weighted_avg_price():
    fills = FillTracker(target_qty=1.0)
    fills.record_fill("CALL", 0.4, 1000.0)
    assert fills.entry_premium() is None
    assert fills.is_fully_filled("CALL") is False
    fills.record_fill("CALL", 0.6, 1002.0)
    assert fills.is_fully_filled("CALL") is True
    assert fills.fill_price["CALL"] == pytest.approx(1001.20)
    fills.record_fill("PUT", 1.0, 900.0)
    assert fills.entry_premium() == pytest.approx(1901.20)


def test_open_qty_only_closes_what_is_open():
    fills = FillTracker(target_qty=1.0)
    fills.record_fill("CALL", 1.0, 1000.0)
    fills.record_fill("PUT", 1.0, 900.0)
    assert fills.open_qty("CALL") == 1.0
    fills.record_close("CALL", 0.4)  # partial close
    assert fills.open_qty("CALL") == pytest.approx(0.6)
    assert fills.both_legs_closed() is False
    fills.record_close("CALL", 0.6)
    fills.record_close("PUT", 1.0)
    assert fills.both_legs_closed() is True
    assert fills.open_qty("CALL") == 0.0  # never negative, no double-close
