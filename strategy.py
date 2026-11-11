"""
Level 1 + Level 2 BTC short straddle strategy (NautilusTrader + Bybit).

Short straddle = SELL 1 ATM call + SELL 1 ATM put, same strike, same expiry.
Level 2 adds: stop-loss if the combined buyback premium rises 1% over the
entry premium; take-profit if it falls 2% under it. Buyback is valued on
ASK prices (what it costs to buy the options back).

Execution flow (see WALKTHROUGH.md for the full story):
  on_start -> subscribe instruments + BTCUSDT reference quotes
  on_instrument / on_quote_tick (reference) -> discover options, pick ATM legs
  _enter_straddle -> SELL market 1 contract of each leg
  on_order_filled -> track fills; both legs full -> arm entry premium
  on_quote_tick (legs) -> buyback = call ask + put ask -> SL / TP check
  _exit_straddle -> BUY market reduce-only for the still-open qty of each leg
"""

from __future__ import annotations

from datetime import datetime, timezone

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderCanceled, OrderFilled, OrderRejected
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.instruments import OptionContract
from nautilus_trader.trading.strategy import Strategy

from straddle_logic import (
    STOP_LOSS,
    TAKE_PROFIT,
    ExitGuard,
    FillTracker,
    OptionLeg,
    combined_premium,
    select_straddle_legs,
    should_exit,
)

BYBIT = Venue("BYBIT")


class BtcShortStraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for ``BtcShortStraddle``.

    Parameters
    ----------
    reference_instrument_id : InstrumentId
        Spot instrument whose price is the ATM reference (BTCUSDT on Bybit).
    quantity : float
        Contracts per leg (assignment: 1).
    stop_loss_pct : float
        Exit if buyback premium rises this far over entry (assignment: 0.01).
    take_profit_pct : float
        Exit if buyback premium falls this far under entry (assignment: 0.02).
    dry_run : bool
        If True (default), log what WOULD be ordered but submit nothing.
        Data-only discovery mode. Set False only for live TestNet / backtest.
    call_instrument_id / put_instrument_id : InstrumentId, optional
        Pre-selected legs. If given, discovery is skipped — used by the
        backtest (and handy for a demo). If omitted, legs are discovered
        from the live instrument cache.
    reduce_only_on_exit : bool
        Send closing orders as reduce-only (default True — live safety).
        The backtest sets False: the simulated exchange rejects reduce-only
        market buys on these synthetic option positions, while plain market
        buys close them correctly.
    """

    reference_instrument_id: InstrumentId
    quantity: float = 1.0
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.02
    dry_run: bool = True
    call_instrument_id: InstrumentId | None = None
    put_instrument_id: InstrumentId | None = None
    reduce_only_on_exit: bool = True


class BtcShortStraddle(Strategy):
    """Sell 1 ATM BTC call + 1 ATM BTC put; exit on +1% SL / -2% TP."""

    def __init__(self, config: BtcShortStraddleConfig) -> None:
        super().__init__(config)
        self.straddle_state: str = "DISCOVERING"  # DISCOVERING -> SELECTED -> ENTERING
        # ENTERING -> ENTERED -> EXITING -> CLOSED
        self.reference_price: float | None = None
        self.call_instrument: OptionContract | None = None
        self.put_instrument: OptionContract | None = None
        self.best_ask: dict[str, float] = {}  # "CALL"/"PUT" -> best ask
        self.best_bid: dict[str, float] = {}
        self.entry_premium: float | None = None
        self.exit_reason: str | None = None
        self.fills = FillTracker(target_qty=config.quantity)
        self.exit_guard = ExitGuard()
        self._options_seen: dict[InstrumentId, OptionContract] = {}
        self._entry_order_ids: set = set()
        self._exit_order_ids: set = set()
        self._logged_discovery_wait: bool = False

    # ------------------------------------------------------------------
    # Start-up
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        self.log.info(f"STRADDLE START dry_run={self.config.dry_run} "
                      f"qty={self.config.quantity} "
                      f"SL={self.config.stop_loss_pct:.0%} "
                      f"TP={self.config.take_profit_pct:.0%}")
        if self.config.call_instrument_id and self.config.put_instrument_id:
            # Pre-selected legs (backtest / demo): skip discovery.
            self.call_instrument = self.cache.instrument(self.config.call_instrument_id)
            self.put_instrument = self.cache.instrument(self.config.put_instrument_id)
            if self.call_instrument is None or self.put_instrument is None:
                self.log.error("Pre-selected leg instrument(s) not in cache — cannot start")
                self.stop()
                return
            self._select_legs(self.call_instrument, self.put_instrument)
            return

        # Live discovery: ask Bybit for every instrument, and watch BTC spot
        # for the reference price the ATM strike is measured against.
        self.subscribe_instruments(BYBIT)
        self.subscribe_quote_ticks(self.config.reference_instrument_id)
        self.log.info(f"Subscribed to Bybit instruments + reference "
                      f"{self.config.reference_instrument_id}")
        self._try_select_from_cache()

    # ------------------------------------------------------------------
    # Discovery — Level 1
    # ------------------------------------------------------------------
    def on_instrument(self, instrument) -> None:
        """Every instrument Bybit publishes arrives here, one at a time."""
        if isinstance(instrument, OptionContract):
            self._options_seen[instrument.id] = instrument
            self._try_select_from_cache()

    def _try_select_from_cache(self) -> None:
        """Pick the ATM legs as soon as we have a reference price + options."""
        if self.straddle_state != "DISCOVERING" or self.reference_price is None:
            return
        # Fallback: Bybit has no instruments channel — instruments arrive
        # via polling, and on_instrument may not fire for every one.
        # The cache is seeded with them (live run: "Seeded instrument
        # status cache with 1467 entries"), so pull option contracts
        # from the cache too, not only from on_instrument events.
        try:
            for inst in self.cache.instruments(venue=BYBIT):
                if isinstance(inst, OptionContract):
                    self._options_seen.setdefault(inst.id, inst)
        except Exception:  # noqa: BLE001 — cache read must never break selection
            pass
        if not self._options_seen:
            # One-time diagnostic: if this ever logs live, instruments
            # are not being loaded at all (see load_all in run_live.py).
            if not self._logged_discovery_wait:
                self._logged_discovery_wait = True
                self.log.info("Discovery waiting: reference price set, "
                              "but 0 option instruments seen/loaded yet")
            return
        legs = [
            OptionLeg(
                instrument_id=str(o.id),
                strike=o.strike_price.as_double(),
                kind="CALL" if o.option_kind == OptionKind.CALL else "PUT",
                expiration=o.expiration_utc,
                underlying=o.underlying.code,
            )
            for o in self._options_seen.values()
        ]
        try:
            call_leg, put_leg = select_straddle_legs(legs, self.reference_price)
        except ValueError as exc:
            self.log.info(f"ATM selection not possible yet: {exc}")
            return
        call = self._options_seen[InstrumentId.from_str(call_leg.instrument_id)]
        put = self._options_seen[InstrumentId.from_str(put_leg.instrument_id)]
        self._select_legs(call, put)

    def _select_legs(self, call: OptionContract, put: OptionContract) -> None:
        """Record the chosen legs, subscribe to their quotes, and enter."""
        self.call_instrument = call
        self.put_instrument = put
        self.straddle_state = "SELECTED"
        self.log.info(
            f"ATM SELECTED strike={call.strike_price} expiry={call.expiration_utc} "
            f"CALL={call.id} PUT={put.id} (ref BTC={self.reference_price})"
        )
        self.subscribe_quote_ticks(call.id)
        self.subscribe_quote_ticks(put.id)
        # Entry waits for the first quote of EACH leg (see _maybe_enter):
        # a market order needs a market to trade against, and the first
        # quotes also confirm both legs are actually trading.
        self.log.info("Waiting for first quotes on both legs before entering")

    # ------------------------------------------------------------------
    # Entry — Level 1
    # ------------------------------------------------------------------
    def _enter_straddle(self) -> None:
        """SELL 1 contract of each leg at market (the short straddle)."""
        if self.straddle_state != "SELECTED":
            return
        self.straddle_state = "ENTERING"
        for leg, instrument in (("CALL", self.call_instrument), ("PUT", self.put_instrument)):
            order = self.order_factory.market(
                instrument_id=instrument.id,
                order_side=OrderSide.SELL,  # short: we sell the option
                quantity=instrument.make_qty(self.config.quantity),
                time_in_force=TimeInForce.GTC,
            )
            self._entry_order_ids.add(order.client_order_id)
            self._submit(order, f"ENTRY SELL {leg} {instrument.id}")
        self.log.info("Entry orders submitted for both legs — waiting for fills")

    def _submit(self, order, description: str) -> None:
        """Single choke point for order submission (dry-run lives here)."""
        if self.config.dry_run:
            self.log.info(f"DRY RUN — would submit: {description} "
                          f"qty={order.quantity}")
            return
        self.submit_order(order)
        self.log.info(f"Submitted: {description} client_order_id={order.client_order_id}")

    # ------------------------------------------------------------------
    # Market data + Level 2 exit check
    # ------------------------------------------------------------------
    def on_quote_tick(self, tick: QuoteTick) -> None:
        # 1. Reference price (BTC spot) — only used before selection.
        if tick.instrument_id == self.config.reference_instrument_id:
            mid = (tick.bid_price.as_double() + tick.ask_price.as_double()) / 2.0
            if self.reference_price is None:
                self.log.info(f"Reference BTC price: {mid:,.2f} USDT")
            self.reference_price = mid
            self._try_select_from_cache()
            return

        # 2. Leg quotes — track best bid/ask per leg.
        leg = None
        if self.call_instrument and tick.instrument_id == self.call_instrument.id:
            leg = "CALL"
        elif self.put_instrument and tick.instrument_id == self.put_instrument.id:
            leg = "PUT"
        if leg is None:
            return
        self.best_bid[leg] = tick.bid_price.as_double()
        self.best_ask[leg] = tick.ask_price.as_double()
        self._maybe_enter()
        self._check_exit()

    def _maybe_enter(self) -> None:
        """Enter once both legs have shown a first quote (a market exists)."""
        if self.straddle_state != "SELECTED":
            return
        if "CALL" in self.best_bid and "PUT" in self.best_bid:
            self._enter_straddle()

    def _check_exit(self) -> None:
        """Level 2: on every leg quote, value the buyback and maybe exit."""
        if self.straddle_state != "ENTERED" or self.entry_premium is None:
            return  # not armed yet — entry incomplete, or already exiting/closed
        if "CALL" not in self.best_ask or "PUT" not in self.best_ask:
            return  # need both asks to value the combined buyback
        buyback = combined_premium(self.best_ask["CALL"], self.best_ask["PUT"])
        reason = should_exit(
            self.entry_premium, buyback,
            self.config.stop_loss_pct, self.config.take_profit_pct,
        )
        self.log.info(f"Buyback check: entry={self.entry_premium:.2f} "
                      f"buyback={buyback:.2f} ({buyback / self.entry_premium - 1:+.2%})")
        if reason is not None:
            self._exit_straddle(reason, buyback)

    def _exit_straddle(self, reason: str, buyback: float) -> None:
        """
        Buy back both legs. Guarded twice: the ExitGuard (first attempt
        wins) and the state check (only from ENTERED). A second trigger —
        e.g. another quote arriving while the first exit is in flight —
        is logged and ignored.
        """
        if not self.exit_guard.try_start_exit(reason):
            self.log.info(f"Exit already in progress ({self.exit_guard.reason}) — ignoring duplicate")
            return
        if self.straddle_state != "ENTERED":
            self.log.info(f"Exit {reason} ignored: state is {self.straddle_state}, not ENTERED")
            return
        self.straddle_state = "EXITING"
        self.exit_reason = reason
        label = "STOP-LOSS (+1%)" if reason == STOP_LOSS else "TAKE-PROFIT (-2%)"
        self.log.info(f"{label} TRIGGERED: buyback={buyback:.2f} vs entry={self.entry_premium:.2f} — closing both legs")
        for leg, instrument in (("CALL", self.call_instrument), ("PUT", self.put_instrument)):
            qty = self.fills.open_qty(leg)
            if qty <= 0:
                self.log.info(f"{leg} already flat — no closing order needed")
                continue
            order = self.order_factory.market(
                instrument_id=instrument.id,
                order_side=OrderSide.BUY,  # buy back the short leg
                quantity=instrument.make_qty(qty),
                time_in_force=TimeInForce.GTC,
                reduce_only=self.config.reduce_only_on_exit,
            )
            self._exit_order_ids.add(order.client_order_id)
            self._submit(order, f"EXIT BUY {leg} {instrument.id} qty={qty}")
        if self.fills.both_legs_closed():
            self.straddle_state = "CLOSED"

    # ------------------------------------------------------------------
    # Order + position tracking
    # ------------------------------------------------------------------
    def on_order_filled(self, event: OrderFilled) -> None:
        leg = self._leg_of(event.instrument_id)
        if leg is None:
            return
        qty = event.last_qty.as_double()
        price = event.last_px.as_double()
        if event.client_order_id in self._entry_order_ids:
            self.fills.record_fill(leg, qty, price)
            self.log.info(f"FILL entry {leg}: qty={qty} price={price:.2f} "
                          f"(total filled {self.fills.filled[leg]}/{self.config.quantity})")
            if self.fills.both_legs_filled() and self.straddle_state == "ENTERING":
                self.entry_premium = self.fills.entry_premium()
                self.straddle_state = "ENTERED"
                self.log.info(f"STRADDLE ENTERED: combined entry premium = {self.entry_premium:.2f} "
                              f"(CALL {self.fills.fill_price['CALL']:.2f} + "
                              f"PUT {self.fills.fill_price['PUT']:.2f}) — SL/TP now armed")
        elif event.client_order_id in self._exit_order_ids:
            self.fills.record_close(leg, qty)
            self.log.info(f"FILL exit {leg}: bought back qty={qty} at {price:.2f}")
            if self.fills.both_legs_closed() and self.straddle_state == "EXITING":
                self.straddle_state = "CLOSED"
                self.log.info(f"STRADDLE CLOSED via {self.exit_reason}: "
                              f"entry={self.entry_premium:.2f}")
                self._log_positions()

    def on_order_rejected(self, event: OrderRejected) -> None:
        self.log.error(f"ORDER REJECTED {event.instrument_id}: {event}")
        self._handle_failed_exit(event)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        self.log.warning(f"ORDER CANCELED {event.instrument_id}: {event}")
        self._handle_failed_exit(event)

    def _handle_failed_exit(self, event) -> None:
        """
        If an EXIT order fails, the position is still open, so the guard
        must be released — the next quote may then retry the exit.
        If an ENTRY order fails, the straddle is one-legged: log it loudly
        and do NOT arm the exit (a single leg has no combined premium).
        """
        if event.client_order_id in self._exit_order_ids:
            self.exit_guard.reset()
            self.straddle_state = "ENTERED"  # still open — exit may be retried
            self.log.warning("Exit order failed — position still open, exit guard released")
        elif event.client_order_id in self._entry_order_ids:
            self.log.error("ENTRY leg rejected — straddle is INCOMPLETE (one-legged). "
                           "No exit is armed. Manual review required.")

    def on_position_opened(self, event) -> None:
        self.log.info(f"POSITION OPENED: {event.instrument_id} "
                      f"position_id={event.position_id} side={event.side} qty={event.quantity}")

    def on_position_closed(self, event) -> None:
        self.log.info(f"POSITION CLOSED: {event.instrument_id} "
                      f"position_id={event.position_id} realized_pnl={event.realized_pnl}")

    def _log_positions(self) -> None:
        """Positions as Nautilus sees them (the source of truth)."""
        try:
            open_positions = self.cache.positions_open()
        except Exception as exc:  # cache shape differs across versions
            self.log.warning(f"Could not read open positions: {exc}")
            return
        self.log.info(f"Open positions: {len(open_positions)}")
        for position in open_positions:
            self.log.info(f"  {position.instrument_id} qty={position.quantity} "
                          f"side={position.side}")

    def _leg_of(self, instrument_id: InstrumentId) -> str | None:
        if self.call_instrument and instrument_id == self.call_instrument.id:
            return "CALL"
        if self.put_instrument and instrument_id == self.put_instrument.id:
            return "PUT"
        return None

    def on_stop(self) -> None:
        self.log.info(f"STRADDLE STOP state={self.straddle_state} entry_premium={self.entry_premium} "
                      f"exit_reason={self.exit_reason}")
        self._log_positions()
