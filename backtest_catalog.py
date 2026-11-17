"""
Backtest the straddle on CONVERTED raw data (catalog path).

Same strategy, same engine, same assertions as the take-profit scenario
in backtest.py — but the ticks come from the ParquetDataCatalog built by
``convert_raw_data.py`` (raw CSV -> QuoteTick -> catalog), not from
synthetic ticks built inline. That is the whole point of the conversion
work: the backtest input is now a file on disk in a documented raw
format, converted through the catalog, instead of hard-coded quotes.

If the catalog does not exist yet it is built first from ``data/raw``.
Run:  python backtest_catalog.py [--catalog catalog]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from backtest import make_btc_option, run_engine_scenario
from convert_raw_data import build_catalog, load_catalog_ticks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest the straddle on ticks loaded from the catalog")
    parser.add_argument("--catalog", default="catalog",
                        help="Catalog directory (built from data/raw if missing)")
    args = parser.parse_args()

    catalog_path = Path(args.catalog)
    if not catalog_path.is_dir() or not any(catalog_path.iterdir()):
        print(f"Catalog {catalog_path} missing/empty — building from data/raw first")
        build_catalog("data/raw", catalog_path)

    call = make_btc_option("CALL", "120000.00")
    put = make_btc_option("PUT", "120000.00")
    call_ticks = load_catalog_ticks(catalog_path, call.id)
    put_ticks = load_catalog_ticks(catalog_path, put.id)
    print(f"Loaded from catalog: CALL {len(call_ticks)} ticks, PUT {len(put_ticks)} ticks")
    # Merge legs in timestamp order (stable sort keeps CALL before PUT
    # within the same minute, matching the synthetic TP scenario).
    ticks = sorted(call_ticks + put_ticks, key=lambda t: t.ts_event)

    s = run_engine_scenario("CATALOG-TAKE-PROFIT", ticks)
    assert s.straddle_state == "CLOSED", f"expected CLOSED, got {s.straddle_state}"
    assert s.exit_reason == "TAKE_PROFIT", f"expected TAKE_PROFIT, got {s.exit_reason}"
    assert s.entry_premium == 1900.0, f"expected entry 1900, got {s.entry_premium}"
    assert len(s._exit_order_ids) == 2, "exactly one closing order per leg"
    print("[CATALOG-TAKE-PROFIT] PASS: converted-data run closed on take-profit, "
          f"entry premium {s.entry_premium}, 2 exit orders, no double-exit")


if __name__ == "__main__":
    main()
