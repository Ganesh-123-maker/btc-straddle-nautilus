"""
Real-data end of the raw-data -> Nautilus conversion: TrueFX AUD/USD.

The docs' "Loading External Data" how-to converts raw CSV ticks into
Nautilus ``QuoteTick`` objects, writes them to a ``ParquetDataCatalog``,
and backtests from the catalog. Its primary source is a histdata.com
download (blocked here: anonymous download does not work, and no
account is created), and its fallback is the TrueFX AUD/USD sample —
``truefx/audusd-ticks.csv`` — which IS real raw data, and which this
script runs on for real:

  * source: the NautilusTrader repo, tag ``v1.231.0`` (same version this
    project pins) — see ``data/raw-real/README.md`` for the URL, md5 and
    the verified stats (100,000 rows, 2020-01-30 -> 2020-01-31,
    bid <= ask on every row).
  * FX spot ticks are NOT a straddle: a single instrument cannot drive
    the BTC call+put strategy, so this script does NOT force them
    through ``BtcShortStraddle``. It proves the pipeline instead:
    parse -> catalog -> query back -> real ``BacktestEngine`` run with
    a tick-counting strategy, asserting every tick fed to the engine
    is seen exactly once. The docs' how-to runs 20,000 ticks; this
    script does the same by default (``--max-ticks 20000``) so the run
    stays short, while the catalog holds the full file.

Real BTC options raw data could not be obtained from this machine:
Bybit public REST (mainnet AND testnet) returns HTTP 403 CloudFront
country block, and no VPN / other-region account is used. The BTC
option CSVs in ``data/raw/`` therefore remain labelled samples.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.strategy import Strategy

from convert_raw_data import load_catalog_ticks, parse_raw_quotes

VENUE = Venue("SIM")
DEFAULT_CSV = "data/raw-real/truefx-audusd-ticks.csv"
DEFAULT_CATALOG = "catalog-fx"
DEFAULT_MAX_TICKS = 20_000  # the docs' how-to sample size


class TickCounterConfig(StrategyConfig, frozen=True):
    """Config for ``TickCounter``: the instrument whose quotes to count."""

    instrument_id: InstrumentId


class TickCounter(Strategy):
    """Minimal strategy: subscribe to quotes and count them.

    Proves real catalog ticks flow through the real ``BacktestEngine``
    (subscription, dispatch, ``on_quote_tick``) without placing orders.
    """

    def __init__(self, config: TickCounterConfig) -> None:
        super().__init__(config)
        self.seen = 0
        self.first_ts_ns: int | None = None
        self.last_ts_ns: int | None = None

    def on_start(self) -> None:
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        self.seen += 1
        if self.first_ts_ns is None:
            self.first_ts_ns = tick.ts_event
        self.last_ts_ns = tick.ts_event


def build_fx_catalog(
    csv_path: str | Path,
    catalog_path: str | Path,
    max_rows: int | None = None,
) -> tuple[ParquetDataCatalog, object, list[QuoteTick]]:
    """Parse the real TrueFX CSV and write it to a fresh catalog."""
    instrument = TestInstrumentProvider.default_fx_ccy("AUD/USD")
    # AUD/USD size_precision is 0, so sizes must be "1", not "1.00" —
    # the simulated exchange rejects ticks whose size precision
    # does not match the instrument.
    ticks = parse_raw_quotes(csv_path, instrument, default_size="1")
    if max_rows is not None:
        ticks = ticks[:max_rows]
    catalog_dir = Path(catalog_path)
    if catalog_dir.exists():
        shutil.rmtree(catalog_dir)
    catalog_dir.mkdir(parents=True)
    catalog = ParquetDataCatalog(str(catalog_dir))
    catalog.write_data([instrument])
    catalog.write_data(ticks)
    print(f"  {Path(csv_path).name}: {len(ticks)} ticks, "
          f"{datetime.fromtimestamp(ticks[0].ts_event / 1e9, tz=timezone.utc).isoformat()} "
          f"-> {datetime.fromtimestamp(ticks[-1].ts_event / 1e9, tz=timezone.utc).isoformat()}")
    print(f"Catalog written: {catalog_dir} (1 instrument, {len(ticks)} quote ticks)")
    return catalog, instrument, ticks


def run_fx_engine(catalog_path: str | Path, instrument, max_ticks: int) -> TickCounter:
    """Load ticks back from the catalog and run them through the engine."""
    ticks = load_catalog_ticks(catalog_path, instrument.id)[:max_ticks]
    print(f"Loaded from catalog: AUD/USD {len(ticks)} ticks")
    engine = BacktestEngine()
    engine.add_venue(
        VENUE,
        OmsType.HEDGING,
        AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(1_000_000, USD)],
    )
    engine.add_instrument(instrument)
    counter = TickCounter(TickCounterConfig(instrument_id=instrument.id))
    engine.add_strategy(counter)
    engine.add_data(ticks)
    engine.run()
    engine.dispose()
    print(f"[FX-CATALOG] ticks fed={len(ticks)} ticks seen by strategy={counter.seen}")
    return counter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert the real TrueFX AUD/USD raw CSV to a Nautilus "
                    "catalog and run it through the BacktestEngine")
    parser.add_argument("--csv", default=DEFAULT_CSV,
                        help="Real raw TrueFX CSV file")
    parser.add_argument("--catalog", default=DEFAULT_CATALOG,
                        help="Catalog directory to (re)create")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="Ticks to write to the catalog (default: whole file)")
    parser.add_argument("--max-ticks", type=int, default=DEFAULT_MAX_TICKS,
                        help="Ticks to feed the engine (default: 20000, the docs' sample size)")
    args = parser.parse_args()
    print(f"Converting real raw data {args.csv} -> catalog {args.catalog}")
    _, instrument, _ = build_fx_catalog(args.csv, args.catalog, args.max_rows)
    counter = run_fx_engine(args.catalog, instrument, args.max_ticks)
    assert counter.seen == args.max_ticks, (
        f"engine saw {counter.seen} ticks, expected {args.max_ticks}")
    print(f"[FX-CATALOG] PASS: {counter.seen} real ticks parsed -> catalog "
          f"-> engine, no ticks lost, no orders placed")


if __name__ == "__main__":
    main()
