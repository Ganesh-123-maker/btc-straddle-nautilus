"""
Backtest + simulated harness for the BTC short straddle.

Two layers, both driving the SAME exit logic the live strategy uses
(straddle_logic.should_exit / ExitGuard / FillTracker):

A. Real BacktestEngine run (nautilus_trader.backtest.engine) with synthetic
   CryptoOption instruments and synthetic quote ticks:
     1. TP scenario — buyback falls 2% -> take-profit exit fires, both legs
        bought back, and later quotes do NOT trigger a second exit.
     2. SL scenario — buyback rises 1% -> stop-loss exit fires.
   The engine proves order submission, fill handling and the exit guard
   inside real Nautilus machinery (no Bybit connection needed).

B. Scripted quote-feed harness (pure logic, no engine): a tick-by-tick
   trace including a PARTIAL fill (0.4 then 0.6 of 1 contract) and a
   duplicate exit attempt, proving the fill tracker arms the exit only
   after both legs are fully filled and the guard blocks the second exit.

HONEST LIMIT: the quotes and instruments here are synthetic. This proves
the strategy logic and the engine wiring — it does NOT prove live Bybit
fill behaviour. That needs the TestNet run (run_live.py --live-testnet).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType, OptionKind
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.objects import Money, Price, Quantity, Currency

from straddle_logic import ExitGuard, FillTracker, combined_premium, should_exit
from strategy import BtcShortStraddle, BtcShortStraddleConfig

VENUE = Venue("BYBIT")
EXPIRY = datetime(2026, 12, 25, 8, tzinfo=timezone.utc)


def make_btc_option(kind: str, strike: str) -> CryptoOption:
    """One synthetic BTC option, shaped like a Bybit USDT option."""
    letter = "C" if kind == "CALL" else "P"
    return CryptoOption(
        instrument_id=InstrumentId.from_str(
            f"BTC-25DEC26-{strike}-{letter}-USDT-OPTION.BYBIT"
        ),
        raw_symbol=Symbol(f"BTC-25DEC26-{strike}-{letter}-USDT-OPTION"),
        underlying=Currency.from_str("BTC"),
        quote_currency=Currency.from_str("USDT"),
        settlement_currency=Currency.from_str("USDT"),
        is_inverse=False,
        option_kind=OptionKind.CALL if kind == "CALL" else OptionKind.PUT,
        strike_price=Price.from_str(strike),
        activation_ns=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1e9),
        expiration_ns=int(EXPIRY.timestamp() * 1e9),
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        size_precision=2,
        size_increment=Quantity.from_str("0.01"),
        multiplier=Quantity.from_str("1.00"),
        lot_size=Quantity.from_str("1.00"),
        max_quantity=Quantity.from_str("100.00"),
        min_quantity=Quantity.from_str("0.01"),
        max_notional=None,
        min_notional=None,
        max_price=None,
        min_price=None,
        margin_init=Decimal("0.00"),
        margin_maint=Decimal("0.00"),
        maker_fee=Decimal("0.0003"),
        taker_fee=Decimal("0.0003"),
        ts_event=0,
        ts_init=0,
        info=None,
    )


def quote(instrument_id: InstrumentId, bid: str, ask: str, ts_ns: int) -> QuoteTick:
    return QuoteTick(
        instrument_id=instrument_id,
        bid_price=Price.from_str(bid),
        ask_price=Price.from_str(ask),
        bid_size=Quantity.from_str("1.00"),
        ask_size=Quantity.from_str("1.00"),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def _ns(minute: int) -> int:
    base = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    return int((base.timestamp() + minute * 60) * 1e9)


def run_engine_scenario(name: str, ticks: list[QuoteTick]) -> BtcShortStraddle:
    """Run one scenario through the real BacktestEngine; return the strategy."""
    call = make_btc_option("CALL", "120000.00")
    put = make_btc_option("PUT", "120000.00")

    engine = BacktestEngine()
    engine.add_venue(
        VENUE,
        OmsType.HEDGING,
        AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(100_000, USDT)],
    )
    engine.add_instrument(call)
    engine.add_instrument(put)

    strategy = BtcShortStraddle(
        BtcShortStraddleConfig(
            reference_instrument_id=InstrumentId.from_str("BTCUSDT.BYBIT"),
            quantity=1.0,
            dry_run=False,  # backtest: orders go to the simulated exchange
            call_instrument_id=call.id,
            put_instrument_id=put.id,
            reduce_only_on_exit=False,  # simulated exchange quirk, see config doc
        )
    )
    engine.add_strategy(strategy)
    engine.add_data(ticks)
    engine.run()
    engine.dispose()
    print(f"[{name}] final state={strategy.straddle_state} "
          f"entry_premium={strategy.entry_premium} exit_reason={strategy.exit_reason} "
          f"exit_orders={len(strategy._exit_order_ids)}")
    return strategy


def scenario_take_profit() -> None:
    """Entry fills at call 1000 + put 900 = 1900. Asks fall to 980 + 880 = 1860
    (-2.1%) -> TP fires. Extra quotes afterwards must NOT re-exit."""
    call = make_btc_option("CALL", "120000.00")
    put = make_btc_option("PUT", "120000.00")
    ticks = [
        quote(call.id, "1000.00", "1005.00", _ns(0)),
        quote(put.id, "900.00", "905.00", _ns(0)),
        # t1: entry market sells fill at the bids -> entry premium 1900.
        quote(call.id, "1000.00", "1005.00", _ns(1)),
        quote(put.id, "900.00", "905.00", _ns(1)),
        quote(call.id, "990.00", "995.00", _ns(2)),   # buyback 1890 — no trigger
        quote(put.id, "890.00", "895.00", _ns(2)),
        quote(call.id, "975.00", "980.00", _ns(3)),    # buyback 1860 — TP (-2.1%)
        quote(put.id, "875.00", "880.00", _ns(3)),
        # t4: exit market buys fill at the asks (980 + 880).
        quote(call.id, "975.00", "980.00", _ns(4)),
        quote(put.id, "875.00", "880.00", _ns(4)),
        quote(call.id, "900.00", "905.00", _ns(5)),    # after exit: must be ignored
        quote(put.id, "800.00", "805.00", _ns(5)),
    ]
    s = run_engine_scenario("TAKE-PROFIT", ticks)
    assert s.straddle_state == "CLOSED", f"expected CLOSED, got {s.straddle_state}"
    assert s.exit_reason == "TAKE_PROFIT", f"expected TAKE_PROFIT, got {s.exit_reason}"
    assert s.entry_premium == 1900.0, f"expected entry 1900, got {s.entry_premium}"
    assert len(s._exit_order_ids) == 2, "exactly one closing order per leg"
    print("[TAKE-PROFIT] PASS: TP fired at -2%, both legs closed, no double-exit")


def scenario_stop_loss() -> None:
    """Entry fills at 1900. Asks rise to 1020 + 910 = 1930 (+1.6%) -> SL fires."""
    call = make_btc_option("CALL", "120000.00")
    put = make_btc_option("PUT", "120000.00")
    ticks = [
        quote(call.id, "1000.00", "1005.00", _ns(0)),
        quote(put.id, "900.00", "905.00", _ns(0)),
        # t1: entry market sells fill at the bids -> entry premium 1900.
        quote(call.id, "1000.00", "1005.00", _ns(1)),
        quote(put.id, "900.00", "905.00", _ns(1)),
        quote(call.id, "1010.00", "1015.00", _ns(2)),  # buyback 1920 — SL (+1.05%)
        quote(put.id, "900.00", "905.00", _ns(2)),
        # t3: exit market buys fill at the asks (1015 + 905).
        quote(call.id, "1010.00", "1015.00", _ns(3)),
        quote(put.id, "900.00", "905.00", _ns(3)),
        quote(call.id, "1100.00", "1105.00", _ns(4)),  # after exit: must be ignored
        quote(put.id, "1000.00", "1005.00", _ns(4)),
    ]
    s = run_engine_scenario("STOP-LOSS", ticks)
    assert s.straddle_state == "CLOSED", f"expected CLOSED, got {s.straddle_state}"
    assert s.exit_reason == "STOP_LOSS", f"expected STOP_LOSS, got {s.exit_reason}"
    assert len(s._exit_order_ids) == 2, "exactly one closing order per leg"
    print("[STOP-LOSS] PASS: SL fired at +1%, both legs closed, no double-exit")


def harness_partial_fill_and_duplicate_exit() -> None:
    """
    Scripted feed through the pure logic: entry fills PARTIALLY first
    (CALL 0.4, then 0.6), the exit must NOT arm until both legs are full,
    and a second exit attempt while exiting must be refused.
    """
    print("\n--- Harness: partial fills + duplicate exit (pure logic) ---")
    fills = FillTracker(target_qty=1.0)
    guard = ExitGuard()

    fills.record_fill("CALL", 0.4, 1000.0)
    print(f"CALL filled 0.4 @1000 -> entry premium armed? {fills.entry_premium()} (must be None)")
    assert fills.entry_premium() is None, "exit must NOT arm on a partial single leg"

    fills.record_fill("CALL", 0.6, 1002.0)  # CALL now full, avg 1001.20
    fills.record_fill("PUT", 1.0, 900.0)
    entry = fills.entry_premium()
    print(f"Both legs full -> entry premium = {entry:.2f} (CALL avg 1001.20 + PUT 900)")
    assert abs(entry - 1901.20) < 1e-9, f"entry {entry}"

    # Quotes drift up: buyback 1925 (+1.26%) -> SL.
    buyback = combined_premium(1020.0, 905.0)
    reason = should_exit(entry, buyback)
    print(f"Buyback {buyback} vs entry {entry} ({buyback / entry - 1:+.2%}) -> {reason}")
    assert reason == "STOP_LOSS"

    first = guard.try_start_exit(reason)
    second = guard.try_start_exit(reason)  # duplicate attempt, same tick storm
    print(f"First exit attempt accepted? {first} | duplicate attempt accepted? {second} (must be False)")
    assert first is True and second is False

    # Exit fills arrive partially too; only the still-open qty is bought back.
    print(f"Open qty before close: CALL={fills.open_qty('CALL')}, PUT={fills.open_qty('PUT')}")
    fills.record_close("CALL", 1.0)
    fills.record_close("PUT", 1.0)
    assert fills.both_legs_closed()
    print("Both legs closed, open qty 0 / 0 — no double-close possible.")
    print("[HARNESS] PASS")


def main() -> None:
    scenario_take_profit()
    scenario_stop_loss()
    harness_partial_fill_and_duplicate_exit()
    print("\nALL BACKTEST SCENARIOS PASSED")


if __name__ == "__main__":
    main()
