# BTC Options Straddle — NautilusTrader + Bybit TestNet
# Level 1: naked ATM short straddle (sell 1 ATM call + 1 ATM put, same strike/expiry)
# Level 2: automated risk management — 1% stop-loss / 2% take-profit on the
# combined buyback premium (ask side, both legs).
#
# SAFETY: TestNet ONLY. No mainnet, no real money. Keys come from environment
# variables only and are never printed, logged, or committed.

## Quick start

```bash
cd ~/workspace/btc-straddle-nautilus
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. Sanity check — no keys, no network, no orders
.venv/bin/python -c "import nautilus_trader; print(nautilus_trader.__version__)"

# 2. Unit tests (ATM selection, premium math, SL/TP boundaries, exit guard, partial fills)
.venv/bin/pytest -v

# 3. Simulated backtest — synthetic quotes drive the real exit logic:
#    entry fills, TP fires at -2%, SL fires at +1%, no double-exit
.venv/bin/python backtest.py

# 4. Data-only live run (default — subscribes, discovers the ATM strike and
#    prints quotes, places NO orders, needs no keys for public data)
.venv/bin/python run_live.py --seconds 60

# 5. Live TestNet run (places REAL testnet orders — testnet funds only)
export BYBIT_TESTNET_API_KEY="..."
export BYBIT_TESTNET_API_SECRET="..."
.venv/bin/python run_live.py --live-testnet --seconds 120
```

Get TestNet keys at https://testnet.bybit.com (API Management).
Bybit TestNet endpoints used by the adapter: `api-testnet.bybit.com`,
`wss://stream-testnet.bybit.com/v5/public/option`.

## Files

| File | What it is |
|---|---|
| `config.py` | TestNet-only settings; reads keys from env vars only |
| `straddle_logic.py` | Pure, unit-testable logic: ATM selection, premium math, SL/TP check, exit guard, fill tracker |
| `strategy.py` | `BtcShortStraddle` — the Nautilus `Strategy` (Level 1 + Level 2) |
| `run_live.py` | `TradingNode` + Bybit data/exec clients; data-only by default |
| `backtest.py` | `BacktestEngine` run on synthetic quotes + a scripted quote-feed harness |
| `tests/test_straddle_logic.py` | pytest suite for the pure logic |
| `WALKTHROUGH.md` | The 10 review questions, answered in execution-flow order |
| `.gitignore` | Keeps `.venv/`, logs and any key material out of git |

## How the strategy works (one paragraph)

On start the strategy subscribes to BTC option instruments and to the
`BTCUSDT` spot reference price. From the instrument cache it keeps only
unexpired BTC options, takes the nearest expiry, and picks the strike closest
to the BTC reference price — that is the ATM strike. It subscribes to quote
ticks for the matching call and put, then sells 1 contract of each at market.
Every fill is recorded; only once **both** legs are fully filled is the entry
combined premium (`call fill + put fill`) armed. On every later quote the
strategy values the buyback as `call ask + put ask`: if it is >= entry x 1.01
the stop-loss fires, if it is <= entry x 0.98 the take-profit fires. Either
exit buys back only the still-open quantity of each leg, and an `exiting`
flag plus an open-position check make a duplicate exit impossible. If only
one leg fills, the strategy logs the one-legged state and waits — it never
arms the exit on a single leg.

## Verified results (2026-11-06, nautilus_trader 1.231.0, Python 3.12)

- `python -c "import nautilus_trader"` → **1.231.0** OK. Bybit adapter
  imports OK (`BybitDataClientConfig`, `BybitExecClientConfig`,
  `BybitEnvironment.TESTNET`, `BybitProductType.OPTION`).
- **pytest: 17/17 passed** (`tests/test_straddle_logic.py`) — ATM selection
  incl. tie-break, SL/TP exact boundaries, exit guard, partial fills.
- **backtest.py: ALL PASSED** — real `BacktestEngine`: TP scenario (entry
  1900 → buyback 1860, −2.1% → `TAKE_PROFIT`, `CLOSED`, exactly 2 exit
  orders), SL scenario (buyback 1920, +1.05% → `STOP_LOSS`, `CLOSED`,
  exactly 2 exit orders), plus the scripted harness (partial fills
  0.4 + 0.6, no arming on one leg, duplicate exit refused).
- **Live data-only run: NOT completed from this VM — Bybit TestNet is
  geo-blocked here.** The node builds correctly (factories registered,
  data client created) and attempts the right endpoint, but the
  connection fails: `Failed to connect to
  wss://stream-testnet.bybit.com/v5/public/option ... InvalidContentType`,
  and REST returns HTTP 403 `The Amazon CloudFront distribution is
  configured to block access from your country.` Full log:
  `live-run-1.log`. **Run `run_live.py` from the user's own machine
  (or any network not blocked by CloudFront) to see live discovery.**
  No orders were placed anywhere; `--live-testnet` without keys exits
  with an error (verified).

## Findings worth knowing before the demo

- **Factory registration is mandatory.** `TradingNode` does not pick up
  the Bybit adapter by import alone — you must call
  `node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)`
  (and the exec equivalent) before `node.build()`, or no client is built.
- **Entry waits for first quotes.** A market order submitted before any
  quote exists for that leg is rejected by the matching engine with
  "no market" (seen in the backtest). The strategy now enters only after
  both legs have quoted.
- **The bid–ask spread eats the 1% stop.** Entry fills at the bid, the
  buyback is valued at the ask, so the spread alone can exceed +1% (a
  10-wide spread on a 1900 premium is +1.05% and trips the stop at once).
  The backtest uses a 5-wide spread; live, check the spread before entry.
- **Reduce-only exit vs the simulator.** The simulated exchange rejects
  `reduce_only` market buys on the synthetic option positions
  ("would have increased position"), while plain market buys close them
  correctly. Live keeps `reduce_only=True` (default); the backtest sets
  `reduce_only_on_exit=False` — see the config docstring. This is a
  simulator quirk, not a strategy bug; verify reduce-only behaviour on
  TestNet before relying on it.

## Known limits (honest)

- Bybit options are European, USDT/USDC-settled; only market/limit orders,
  no bars/kline, no conditional/trailing stops, UTA account required.
- The backtest uses synthetic `CryptoOption` instruments and synthetic quote
  ticks (real Bybit option history is not bundled). It proves the entry,
  SL/TP and no-double-exit logic inside the real `BacktestEngine`; it does
  **not** prove live Bybit fill behaviour — that needs the TestNet run.
- Partial fills are covered by the `FillTracker` unit tests and the scripted
  harness; the backtest engine fills market orders in full, so a live
  partial fill can only be observed on TestNet.
- Data-only mode places no orders, so SL/TP never arms there — by design.

## Live-run bug found and fixed (11 Nov 2026, on Windows)

The first live data-only run connected to both Bybit websockets and
seeded 1,467 instruments — then discovery never ran, because the
reference symbol was wrong: `BTCUSDT.BYBIT` is rejected by Nautilus
1.231.0 (`ValueError: invalid Bybit symbol 'BTCUSDT': expected suffix
in ["-SPOT", "-LINEAR", "-INVERSE", "-OPTION"]`). Fixed to
`BTCUSDT-SPOT.BYBIT` in `run_live.py`, and `_try_select_from_cache`
now also pulls option contracts from the instrument cache, since
Bybit has no instruments channel and `on_instrument` alone may not
deliver them all. Re-test after pulling: `pytest` + `backtest.py`,
then `run_live.py --seconds 60` — it should now print
`ATM SELECTED strike=...` and `Reference BTC price: ...`.

Second live bug (11 Nov 2026, second run): after the symbol fix the
node subscribed to BTCUSDT-SPOT quotes, but still sat in DISCOVERING
for 60s — no reference tick, no option ever arrived. Cause: the
Bybit instrument provider loads nothing by default
(`load_all=False`, node warns "No loading configured"). Fixed with
`instrument_provider=InstrumentProviderConfig(load_all=True)` on the
data client in `run_live.py`, plus a one-time "Discovery waiting"
diagnostic in `strategy.py` if 0 options are ever seen after the
reference price arrives.

Third live bug (11 Nov 2026, third run): instruments loaded (1,439)
and a real BTC price arrived (73,776.10 USDT), yet the strategy
still saw "0 option instruments". Cause: Bybit options are
`CryptoOption`, not `OptionContract` — sibling Instrument classes,
so `isinstance(x, OptionContract)` is False for every real Bybit
option. Fixed in `strategy.py` with `is_option_instrument()`
accepting both types (used in `on_instrument` and the cache
fallback), plus a regression test
`tests/test_live_discovery_filter.py` (19 tests now). The backtest
never caught this because it uses pre-selected legs and never goes
through discovery.
