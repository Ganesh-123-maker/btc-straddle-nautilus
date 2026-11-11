# Walkthrough — BTC Short Straddle on NautilusTrader + Bybit TestNet

This answers the assignment's 10 review questions, in the order things
actually happen when you run the strategy. Read it with `strategy.py`,
`run_live.py` and `straddle_logic.py` open — every answer names the file
and function where the work happens.

## 0. The strategy in one sentence

Sell 1 ATM BTC call + 1 ATM BTC put (same strike, same expiry). The
combined premium you collect is the most you can make; you profit if BTC
stays near the strike. Level 2 closes both legs automatically if buying
them back gets 1% more expensive (stop-loss) or 2% cheaper (take-profit).

---

## 1. How does NautilusTrader start and initialize?

`run_live.py` → `main()` → `build_node()` → `TradingNode`.

1. `load_settings()` (`config.py`) reads only `BYBIT_TESTNET_API_KEY` /
   `BYBIT_TESTNET_API_SECRET` from the environment. No keys, no live mode.
2. `build_node()` builds a `TradingNodeConfig`: a `trader_id`, a
   `LoggingConfig`, the strategy as an `ImportableStrategyConfig`
   (`strategy:BtcShortStraddle` + a config dict), and the Bybit data
   client config. The execution client is added **only** in
   `--live-testnet` mode.
3. `main()` registers the Bybit live factories on the node
   (`node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)` —
   without this the node logs `No LiveDataClientFactory registered for
   BYBIT` and builds no client at all; this was the first real bug found
   while building this project), then `node.build()`, then `node.run()`
   in a thread. The node starts its engines (Data, Risk, Exec, Cache,
   MessageBus), connects the data client, and calls the strategy's
   `on_start()`. After `--seconds` it calls `node.stop()` and
   `node.dispose()` — the run is always bounded.

Inside the strategy, `on_start()` (`strategy.py`) is where our own
initialization lives: log the mode, then either use pre-selected legs
(backtest) or subscribe to instruments + reference quotes (live).

## 2. How have you configured the Bybit integration?

In `run_live.py` → `build_node()`:

```python
BybitDataClientConfig(
    api_key=settings.api_key, api_secret=settings.api_secret,
    product_types=(BybitProductType.OPTION, BybitProductType.SPOT),
    environment=BybitEnvironment.TESTNET,   # TESTNET — never mainnet
)
BybitExecClientConfig(
    api_key=..., api_secret=...,
    product_types=(BybitProductType.OPTION,),
    environment=BybitEnvironment.TESTNET,
)
```

Key points for the review:

- **TestNet only.** `BybitEnvironment.TESTNET` points the adapter at
  `api-testnet.bybit.com` and `wss://stream-testnet.bybit.com/v5/public/option`.
  There is no mainnet value anywhere in this project.
- **Keys from env only** (`config.py`). Nothing is hardcoded, printed, or
  committed. `--live-testnet` without both env vars exits with an error.
- **Product types are explicit.** `OPTION` for the legs, `SPOT` for the
  `BTCUSDT` reference price. Bybit options are European, USDT-settled,
  market/limit orders only — no bars, no conditional/trailing stops.
- **Default is data-only.** `dry_run=True` unless `--live-testnet` is
  passed, so a plain `python run_live.py` can never place an order.

## 3. How does NautilusTrader receive market data?

Bybit pushes data over WebSocket; Nautilus turns each message into a
typed event and routes it to the strategy:

```
Bybit WS (testnet) → BybitDataClient → DataEngine → MessageBus
    → Strategy.on_quote_tick(tick) / on_instrument(instrument)
```

- Instruments arrive as `OptionContract` objects via `on_instrument`.
- Quotes arrive as `QuoteTick` objects (bid/ask price + size, timestamp)
  via `on_quote_tick`.
- The strategy never touches sockets or HTTP itself — it only declares
  subscriptions (`subscribe_instruments`, `subscribe_quote_ticks`) and
  handles the typed events. The `DataEngine` does the routing by
  instrument ID.

## 4. How are BTC option instruments identified and subscribed to?

Identification — a Bybit option instrument ID looks like:

```
BTC-25DEC26-120000-C-USDT-OPTION.BYBIT
 └┬┘ └─────┘ └──┘ └┘ └─┬┘ └──┘ └──┘
  │   expiry  strike C/P │    │    venue
  │                      │    product suffix
  └─ underlying          └─ settlement currency
```

Subscription — in `on_start()` the strategy calls:

```python
self.subscribe_instruments(Venue("BYBIT"))          # every instrument, once
self.subscribe_quote_ticks(InstrumentId("BTCUSDT.BYBIT"))  # reference price
```

Every option Bybit publishes then arrives in `on_instrument()`, where
`isinstance(instrument, OptionContract)` filters for options and they are
kept in `self._options_seen`. Once both legs are chosen, the strategy
subscribes to their quotes individually
(`subscribe_quote_ticks(call.id)` / `subscribe_quote_ticks(put.id)`).

## 5. How does the strategy receive market data events?

Two callbacks in `strategy.py`:

- `on_instrument(instrument)` — one call per instrument; stores BTC
  options, then calls `_try_select_from_cache()`.
- `on_quote_tick(tick)` — one call per quote, and it does three jobs:
  1. If the tick is the `BTCUSDT` reference, update
     `self.reference_price` (mid-price) and retry ATM selection.
  2. If the tick is a leg, store `best_bid` / `best_ask` for that leg,
     then `_maybe_enter()` (enter once both legs have quoted) and
     `_check_exit()` (Level 2, every tick).

Nautilus guarantees these run on the strategy's thread in event order,
so the state machine (`DISCOVERING → SELECTED → ENTERING → ENTERED →
EXITING → CLOSED`, in `self.straddle_state`) never races with itself.

## 6. How are orders submitted to Bybit?

Through the standard Nautilus path — the strategy never calls Bybit
directly:

```python
order = self.order_factory.market(
    instrument_id=instrument.id,
    order_side=OrderSide.SELL,          # short leg
    quantity=instrument.make_qty(1.0),
    time_in_force=TimeInForce.GTC,
)
self.submit_order(order)
```

`submit_order()` hands the order to the `ExecEngine`, which routes it to
the `BybitExecutionClient`, which sends it to Bybit's REST order endpoint
(the demo uses HTTP REST for orders; the WS Trade API is unsupported).
Exit orders add `reduce_only=True` so a closing buy can never open a new
long position by accident. All submission goes through one choke point,
`_submit()`, which is also where `dry_run` blocks orders in data-only mode.

## 7. How does the system know whether an order was accepted or executed?

Bybit answers asynchronously, and Nautilus delivers each answer as an
order event to the strategy:

| Event | Meaning | Handler |
|---|---|---|
| `OrderSubmitted` | Nautilus accepted it locally | (logged by the framework) |
| `OrderAccepted` | Bybit accepted it | `on_order_accepted` (inherited logging) |
| `OrderFilled` | (Partially) executed — carries `last_qty`, `last_px` | `on_order_filled` |
| `OrderRejected` | Bybit refused it — carries `reason` | `on_order_rejected` |
| `OrderCanceled` | Cancelled | `on_order_canceled` |

`on_order_filled` is where the strategy learns the truth: it records
each fill (leg, qty, price) in `FillTracker`. Only when **both** legs are
fully filled does it compute `entry_premium = call fill + put fill` and
move to `ENTERED`, arming the SL/TP. A fill is never assumed from a
submission — submission ≠ execution, and the code treats them as
different facts.

## 8. How are positions tracked?

Two layers, and they agree by construction:

1. **The strategy's own `FillTracker`** (`straddle_logic.py`): filled qty
   and volume-weighted average fill price per leg, minus closed qty =
   open qty per leg. This is what the exit uses to size closing orders
   ("buy back only what is still open").
2. **Nautilus' cache / portfolio** (the source of truth): `on_position_opened`
   / `on_position_closed` log every position event, and `_log_positions()`
   reads `self.cache.positions_open()` and logs each open position
   (instrument, qty, side). On stop, the strategy logs its final state
   plus all open positions.

## 9. How have you implemented the Stop-Loss and Take-Profit?

All in `straddle_logic.py` (pure functions) + `_check_exit()` /
`_exit_straddle()` in `strategy.py`:

```python
buyback = call_ask + put_ask            # what it costs to buy both back
if buyback >= entry * 1.01:  exit("STOP_LOSS")
if buyback <= entry * 0.98:  exit("TAKE_PROFIT")
```

- **Valuation is ask-side**, exactly as the assignment specifies — the ask
  is what you pay to buy back a short option.
- **Arming:** the check runs on every leg quote, but only in state
  `ENTERED` with a known `entry_premium` — i.e. only after both legs are
  fully filled. A one-legged straddle never arms the exit.
- **Boundaries are inclusive:** exactly +1.00% triggers the stop, exactly
  −2.00% triggers the take-profit (unit-tested at the boundary).
- **Exit:** `_exit_straddle()` buys back the still-open quantity of each
  leg with market `reduce_only` orders.
- **Duplicate exits are impossible twice over:** `ExitGuard` (first
  `try_start_exit()` wins; later calls return `False`) plus the state
  check (exits only from `ENTERED`). If an exit order is rejected, the
  guard is released and the state returns to `ENTERED`, so the *next*
  quote can retry — a failed exit is retried, a successful one is never
  repeated.

One real finding from the backtest: the entry fills at the **bid** (market
sell) while the buyback is valued at the **ask**, so the bid–ask spread
alone puts the immediate buyback above the entry premium. With a 10-wide
spread on a 1900 premium that is +1.05% — enough to trip the 1% stop-loss
instantly. The backtest therefore uses a 5-wide spread, and live, a wide
spread at entry is a genuine risk to watch (see README "Known limits").

## 10. What happens when one leg gets executed but the other leg does not?

This is the partial-fill / one-legged case, and the strategy handles it
in three places:

1. **Entry, partial fill:** `FillTracker.record_fill()` accumulates fills
   per leg (e.g. 0.4 then 0.6 of 1 contract, keeping a qty-weighted
   average price). `entry_premium` stays `None` until **both** legs are
   fully filled — the strategy logs the one-legged state and waits. It
   never arms SL/TP on a single leg, because a single leg has no combined
   premium.
2. **Entry, leg rejected:** `on_order_rejected` logs loudly that the
   straddle is INCOMPLETE and needs manual review. It does **not**
   auto-retry and does **not** arm the exit. (Seen for real in the
   backtest while debugging: a market order submitted before any quote
   existed for that leg was rejected with "no market" — which is also why
   entry now waits for a first quote on both legs.)
3. **Exit, partial close:** `_exit_straddle()` sizes each closing order
   from `fills.open_qty(leg)` (filled − already closed), so if only 0.4
   of a leg was bought back, the next attempt closes only the remaining
   0.6. `open_qty` can never go below 0, so a double-close is impossible.

The scripted harness in `backtest.py` demonstrates all of this end to
end: partial fills (0.4 + 0.6), no arming before both legs are full, SL
trigger, and a duplicate exit attempt being refused.

---

## Demo commands

```bash
cd ~/workspace/btc-straddle-nautilus
.venv/bin/python -m pytest -v        # 17 unit tests
.venv/bin/python backtest.py          # TP + SL engine scenarios + harness
.venv/bin/python run_live.py --seconds 60                    # data-only
.venv/bin/python run_live.py --live-testnet --seconds 120    # TestNet orders
```
