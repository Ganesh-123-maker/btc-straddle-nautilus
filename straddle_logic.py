"""
Pure, unit-testable logic for the BTC short straddle.

No Nautilus imports here on purpose: every function works on plain numbers
and small stub objects, so pytest can check it without a trading node, and
the walkthrough can explain it without framework knowledge.

The strategy (strategy.py) calls these same functions — the backtest and the
live run therefore exercise the exact logic the tests cover.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# Exit reasons returned by should_exit().
STOP_LOSS = "STOP_LOSS"
TAKE_PROFIT = "TAKE_PROFIT"


# ---------------------------------------------------------------------------
# Leg selection (Level 1)
# ---------------------------------------------------------------------------

def select_atm_strike(reference_price: float, strikes: list[float]) -> float:
    """
    The ATM strike is the available strike closest to the BTC reference price.

    Tie-break: if two strikes are equally close, take the LOWER one, so the
    choice is deterministic (e.g. ref 100_050 between 100_000 and 100_100
    is a tie at 50 away -> 100_000).
    """
    if not strikes:
        raise ValueError("no strikes available to choose from")
    return min(strikes, key=lambda s: (abs(s - reference_price), s))


def nearest_expiry(expirations: list[datetime]) -> datetime:
    """The nearest expiry among the given (already unexpired) expiries."""
    if not expirations:
        raise ValueError("no expirations available to choose from")
    return min(expirations)


@dataclass(frozen=True)
class OptionLeg:
    """Minimal view of an option contract — just what selection needs."""

    instrument_id: str
    strike: float
    kind: str  # "CALL" or "PUT"
    expiration: datetime
    underlying: str = "BTC"


def select_straddle_legs(
    options: list[OptionLeg],
    reference_price: float,
    now: datetime | None = None,
) -> tuple[OptionLeg, OptionLeg]:
    """
    Pick the call + put for the short straddle.

    Rules (assignment):
    1. Only BTC options, and only ones that have not expired yet.
    2. Nearest expiry among those.
    3. ATM strike = strike closest to the BTC reference price.
    4. One CALL and one PUT with that same strike and same expiry.

    Returns (call_leg, put_leg). Raises ValueError with a plain message if
    any step cannot be satisfied — the strategy logs that and waits instead
    of guessing.
    """
    now = now or datetime.now(timezone.utc)
    live = [o for o in options if o.underlying == "BTC" and o.expiration > now]
    if not live:
        raise ValueError("no unexpired BTC options available")

    expiry = nearest_expiry([o.expiration for o in live])
    at_expiry = [o for o in live if o.expiration == expiry]

    atm = select_atm_strike(reference_price, [o.strike for o in at_expiry])

    calls = [o for o in at_expiry if o.kind == "CALL" and o.strike == atm]
    puts = [o for o in at_expiry if o.kind == "PUT" and o.strike == atm]
    if not calls:
        raise ValueError(f"no CALL at ATM strike {atm} for expiry {expiry}")
    if not puts:
        raise ValueError(f"no PUT at ATM strike {atm} for expiry {expiry}")
    return calls[0], puts[0]


# ---------------------------------------------------------------------------
# Premium math + exit decision (Level 2)
# ---------------------------------------------------------------------------

def combined_premium(call_price: float, put_price: float) -> float:
    """Combined straddle premium = call + put (same quantity on both legs)."""
    return call_price + put_price


def should_exit(
    entry_premium: float,
    current_buyback: float,
    stop_loss_pct: float = 0.01,
    take_profit_pct: float = 0.02,
) -> str | None:
    """
    Level 2 exit check, evaluated on every quote once both legs are filled.

    - Buyback uses ASK prices: that is what it costs us to buy the options back.
    - Stop-loss: buyback >= entry * (1 + 1%)  -> "STOP_LOSS"
    - Take-profit: buyback <= entry * (1 - 2%) -> "TAKE_PROFIT"
    - Boundaries are inclusive: exactly +1.00% triggers the stop, exactly
      -2.00% triggers the take-profit (the assignment says "increases by 1%"
      / "decreases by 2%").
    - Otherwise None (hold).
    """
    if entry_premium <= 0:
        return None
    if current_buyback >= entry_premium * (1.0 + stop_loss_pct):
        return STOP_LOSS
    if current_buyback <= entry_premium * (1.0 - take_profit_pct):
        return TAKE_PROFIT
    return None


# ---------------------------------------------------------------------------
# Exit guard — duplicate exits must be impossible
# ---------------------------------------------------------------------------

@dataclass
class ExitGuard:
    """
    One boolean + one reason, guarding the exit path.

    The strategy checks try_start_exit() before submitting ANY closing order.
    The first call wins; every later call returns False until reset() is
    called (which only happens if the exit order is rejected/cancelled,
    i.e. the position is provably still open).
    """

    exiting: bool = False
    reason: str | None = None

    def try_start_exit(self, reason: str) -> bool:
        """Return True only for the first exit attempt; False after that."""
        if self.exiting:
            return False
        self.exiting = True
        self.reason = reason
        return True

    def reset(self) -> None:
        """Allow a fresh exit attempt (only after a rejected/cancelled exit)."""
        self.exiting = False
        self.reason = None


# ---------------------------------------------------------------------------
# Fill tracking — partial fills
# ---------------------------------------------------------------------------

@dataclass
class FillTracker:
    """
    Accumulates fills per leg so partial fills are handled correctly.

    A market order can fill in pieces (e.g. 0.4 then 0.6 of 1 contract).
    The entry premium is only armed when BOTH legs are FULLY filled, and an
    exit only buys back the quantity still open (filled - already closed).
    """

    target_qty: float = 1.0
    filled: dict[str, float] = field(default_factory=dict)   # leg -> qty filled
    fill_price: dict[str, float] = field(default_factory=dict)  # leg -> avg fill price
    closed: dict[str, float] = field(default_factory=dict)  # leg -> qty closed

    def record_fill(self, leg: str, qty: float, price: float) -> None:
        """Add one fill (possibly partial). Keeps a qty-weighted avg price."""
        prev_qty = self.filled.get(leg, 0.0)
        prev_price = self.fill_price.get(leg, 0.0)
        new_qty = prev_qty + qty
        if new_qty > 0:
            self.fill_price[leg] = (prev_price * prev_qty + price * qty) / new_qty
        self.filled[leg] = new_qty

    def record_close(self, leg: str, qty: float) -> None:
        """Add one closing fill (buy-back of a short leg)."""
        self.closed[leg] = self.closed.get(leg, 0.0) + qty

    def is_fully_filled(self, leg: str) -> bool:
        """True when this leg's fills add up to the target quantity."""
        return self.filled.get(leg, 0.0) >= self.target_qty - 1e-9

    def both_legs_filled(self) -> bool:
        """True only when BOTH call and put are fully filled."""
        return self.is_fully_filled("CALL") and self.is_fully_filled("PUT")

    def entry_premium(self) -> float | None:
        """
        Combined entry premium, or None if either leg is not fully filled.

        Never arm the exit on a single leg: a one-legged straddle has no
        meaningful combined premium.
        """
        if not self.both_legs_filled():
            return None
        return combined_premium(self.fill_price["CALL"], self.fill_price["PUT"])

    def open_qty(self, leg: str) -> float:
        """Quantity still open on a leg = filled - already closed (never < 0)."""
        return max(0.0, self.filled.get(leg, 0.0) - self.closed.get(leg, 0.0))

    def both_legs_closed(self) -> bool:
        """True when every filled contract on both legs has been bought back."""
        return self.open_qty("CALL") <= 1e-9 and self.open_qty("PUT") <= 1e-9
