"""
Convert raw market data into NautilusTrader objects.

The interviewer flagged this: the NautilusTrader documentation
("Loading External Data", nautilustrader.io/docs/latest/how_to/
loading_external_data/) ships raw data, and the work is to convert it
into Nautilus form before any backtest can use it. The conversion is
three steps:

  1. PARSE  — each raw CSV row becomes a Nautilus ``QuoteTick``
     (``parse_raw_quotes``). Nautilus never reads a raw CSV directly;
     the strategy only ever sees typed ``QuoteTick`` objects.
  2. CATALOG — the ticks (plus their instrument definitions) are written
     to a ``ParquetDataCatalog`` (``build_catalog``). Parquet is the
     catalog's storage format: compressed, fast to query, and the
     backtest reads from it instead of from loose CSV files.
  3. BACKTEST — ``backtest_catalog.py`` loads the ticks back out of the
     catalog (``load_catalog_ticks``) and feeds them to the SAME
     ``BtcShortStraddle`` through the real ``BacktestEngine``.

Version note (important): the docs page above targets NautilusTrader
2.x (Python 3.13+, ``--pre``). This project pins
``nautilus_trader==1.231.0`` (Python 3.12), and the 2.x helpers do NOT
exist there — verified against the installed package:

  * ``nautilus_trader.persistence.external.readers.CSVReader`` — MISSING
    (no ``nautilus_trader.persistence.external`` module at all).
  * ``nautilus_trader.testkit.providers.TestDataProvider`` — MISSING
    (no ``nautilus_trader.testkit`` module).
  * ``ParquetDataCatalog.write_instruments / write_quote_ticks /
    query_quote_ticks`` — MISSING in 1.231.0.

What 1.231.0 *does* have, and what this module uses:

  * ``ParquetDataCatalog.write_data(data)`` — writes any list of
    Nautilus objects (instruments and ticks alike), categorised by
    class. This replaces both ``write_instruments`` and
    ``write_quote_ticks``.
  * ``ParquetDataCatalog.query(data_cls, identifiers=[...])`` — the
    generic read; ``catalog.query(QuoteTick, identifiers=[id])``
    replaces ``query_quote_ticks``. (There is also a
    ``catalog.quote_ticks(instrument_ids=[...])`` convenience wrapper.)

So the parser here is a small stdlib ``csv`` parser we control, instead
of a version-fragile helper copied from the 2.x docs. The raw format
accepted is the documented one:

  * columns: ``timestamp,bid,ask`` or ``timestamp,bid_price,ask_price``
    (optional ``bid_size`` / ``ask_size``, default 1.00)
  * timestamps: ISO ``2026-01-05T09:00:00Z`` or histdata-style
    ``20260105 090000000`` (``YYYYMMDD HHMMSSmmm``)
  * lines starting with ``#`` are comments and are skipped, so sample
    files can carry a header note saying what they are.

Nothing is ever invented: a bad row (non-positive price, bid > ask,
unparseable timestamp) or an empty file raises ``ValueError`` with the
file name and row number, instead of silently guessing a price.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog import ParquetDataCatalog

# Imported, not copied: the instrument definitions must stay identical to
# the ones the synthetic backtest uses, so the catalog ticks feed the
# same strategy through the same engine.
from backtest import make_btc_option

_HISTDATA_RE = re.compile(r"^(\d{8})\s+(\d{6})(\d{3})?$")


def _parse_timestamp(raw: str, source: str, row_no: int) -> int:
    """Parse an ISO or histdata-style timestamp to nanoseconds since epoch."""
    text = raw.strip()
    # Histdata: "20260105 090000000" (YYYYMMDD HHMMSSmmm) or "20260105 090000".
    match = _HISTDATA_RE.match(text)
    if match:
        date_part, time_part, millis = match.groups()
        dt = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
        if millis:
            dt = dt.replace(microsecond=int(millis) * 1000)
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1_000_000_000)
    # ISO: "2026-01-05T09:00:00Z", "... +00:00", or naive (assumed UTC).
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{source} row {row_no}: unparseable timestamp {raw!r} "
            "(expected ISO '2026-01-05T09:00:00Z' or histdata '20260105 090000000')"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _parse_price(raw: str, field: str, source: str, row_no: int) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(
            f"{source} row {row_no}: {field} is not a number: {raw!r}"
        ) from None
    if value <= 0:
        raise ValueError(
            f"{source} row {row_no}: {field} must be positive, got {raw!r}"
        )
    return value


def parse_raw_quotes(csv_path: str | Path, instrument) -> list[QuoteTick]:
    """Parse one raw CSV file into sorted Nautilus ``QuoteTick`` objects.

    ``instrument`` is the Nautilus instrument the ticks belong to (its
    ``instrument_id`` is stamped on every tick, so the catalog and the
    backtest can link ticks to the instrument definition).
    """
    path = Path(csv_path)
    if not path.is_file():
        raise ValueError(f"raw data file not found: {path}")
    # Skip '#' comment lines (sample files carry a header note) and keep
    # the rest for DictReader. Blank lines are skipped the same way.
    with path.open(newline="") as f:
        lines = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ValueError(f"{path}: empty file — no header and no data rows")
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise ValueError(f"{path}: empty file — no header row")
    # Normalise headers: "Bid_Price " -> "bid_price".
    field_map = {name.strip().lower(): name for name in reader.fieldnames if name}
    ts_key = next((k for k in ("timestamp", "time", "datetime", "ts") if k in field_map), None)
    bid_key = next((k for k in ("bid_price", "bid") if k in field_map), None)
    ask_key = next((k for k in ("ask_price", "ask") if k in field_map), None)
    missing = [name for name, key in
               (("timestamp", ts_key), ("bid/bid_price", bid_key), ("ask/ask_price", ask_key))
               if key is None]
    if missing:
        raise ValueError(
            f"{path}: missing required column(s): {', '.join(missing)} "
            f"(got header: {', '.join(reader.fieldnames)})"
        )
    bid_size_key = next((k for k in ("bid_size", "bid_qty") if k in field_map), None)
    ask_size_key = next((k for k in ("ask_size", "ask_qty") if k in field_map), None)

    ticks: list[QuoteTick] = []
    for row_no, row in enumerate(reader, start=1):
        bid_raw = row[field_map[bid_key]]
        ask_raw = row[field_map[ask_key]]
        if bid_raw is None or ask_raw is None or not str(bid_raw).strip() or not str(ask_raw).strip():
            raise ValueError(f"{path} row {row_no}: missing bid or ask price")
        bid = _parse_price(bid_raw, "bid", str(path), row_no)
        ask = _parse_price(ask_raw, "ask", str(path), row_no)
        if bid > ask:
            raise ValueError(
                f"{path} row {row_no}: bid ({bid}) > ask ({ask}) — crossed market, "
                "refusing to convert"
            )
        ts_ns = _parse_timestamp(row[field_map[ts_key]], str(path), row_no)
        bid_size = (row[field_map[bid_size_key]] or "1.00") if bid_size_key else "1.00"
        ask_size = (row[field_map[ask_size_key]] or "1.00") if ask_size_key else "1.00"
        ticks.append(QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price.from_str(str(bid)),
            ask_price=Price.from_str(str(ask)),
            bid_size=Quantity.from_str(str(bid_size).strip()),
            ask_size=Quantity.from_str(str(ask_size).strip()),
            ts_event=ts_ns,
            ts_init=ts_ns,
        ))
    if not ticks:
        raise ValueError(f"{path}: empty file — header present but no data rows")
    # The catalog requires ascending timestamps; raw vendor exports are
    # not always monotonic, so sort (stable — equal timestamps keep file order).
    ticks.sort(key=lambda t: t.ts_event)
    return ticks


def _leg_from_filename(path: Path) -> tuple[str, str]:
    """Return (kind, strike) parsed from a leg filename.

    Expected: ``BTC-25DEC26-120000-C-USDT-OPTION.csv`` -> ("CALL", "120000").
    """
    parts = path.stem.split("-")
    if len(parts) < 4 or parts[3] not in ("C", "P"):
        raise ValueError(
            f"{path}: filename must look like "
            "BTC-25DEC26-120000-C-USDT-OPTION.csv (strike + C/P in the name)"
        )
    kind = "CALL" if parts[3] == "C" else "PUT"
    return kind, parts[2]


def build_catalog(raw_dir: str | Path, catalog_path: str | Path) -> ParquetDataCatalog:
    """Build a fresh ParquetDataCatalog from the two leg CSVs in ``raw_dir``.

    Finds the call CSV (filename containing ``-C-``) and the put CSV
    (``-P-``), reuses ``backtest.make_btc_option`` for both instruments
    (so the catalog ticks carry the exact instrument IDs the backtest
    engine expects), parses both files, and writes instruments + ticks
    with the 1.231.0 ``catalog.write_data`` API. The catalog directory
    is wiped and recreated on every run, like the docs guide.
    """
    raw = Path(raw_dir)
    if not raw.is_dir():
        raise ValueError(f"raw data directory not found: {raw}")
    call_files = sorted(p for p in raw.glob("*.csv") if "-C-" in p.name)
    put_files = sorted(p for p in raw.glob("*.csv") if "-P-" in p.name)
    if len(call_files) != 1 or len(put_files) != 1:
        raise ValueError(
            f"{raw}: expected exactly one call CSV ('-C-') and one put CSV ('-P-'), "
            f"found {len(call_files)} call and {len(put_files)} put: "
            f"{[p.name for p in sorted(raw.glob('*.csv'))]}"
        )
    catalog_dir = Path(catalog_path)
    if catalog_dir.exists():
        shutil.rmtree(catalog_dir)
    catalog_dir.mkdir(parents=True)

    catalog = ParquetDataCatalog(str(catalog_dir))
    all_ticks: list[QuoteTick] = []
    for csv_file, expected_kind in ((call_files[0], "CALL"), (put_files[0], "PUT")):
        kind, strike = _leg_from_filename(csv_file)
        if kind != expected_kind:
            raise ValueError(f"{csv_file}: filename says {kind}, expected {expected_kind}")
        instrument = make_btc_option(kind, f"{float(strike):.2f}")
        ticks = parse_raw_quotes(csv_file, instrument)
        print(f"  {csv_file.name}: {len(ticks)} ticks, "
              f"{datetime.fromtimestamp(ticks[0].ts_event / 1e9, tz=timezone.utc).isoformat()} "
              f"-> {datetime.fromtimestamp(ticks[-1].ts_event / 1e9, tz=timezone.utc).isoformat()}")
        all_ticks.extend(ticks)
    # Instruments first, then ticks — write_data categorises by class.
    call_kind, call_strike = _leg_from_filename(call_files[0])
    put_kind, put_strike = _leg_from_filename(put_files[0])
    instruments = [
        make_btc_option(call_kind, f"{float(call_strike):.2f}"),
        make_btc_option(put_kind, f"{float(put_strike):.2f}"),
    ]
    catalog.write_data(instruments)
    catalog.write_data(all_ticks)
    print(f"Catalog written: {catalog_dir} "
          f"({len(instruments)} instruments, {len(all_ticks)} quote ticks)")
    return catalog


def load_catalog_ticks(catalog_path: str | Path, instrument_id) -> list[QuoteTick]:
    """Load a leg's ticks back out of the catalog, in timestamp order."""
    identifier = instrument_id.value if isinstance(instrument_id, InstrumentId) else str(instrument_id)
    catalog = ParquetDataCatalog(str(catalog_path))
    ticks = catalog.query(QuoteTick, identifiers=[identifier])
    return sorted(ticks, key=lambda t: t.ts_event)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert raw CSV option quotes into a Nautilus ParquetDataCatalog")
    parser.add_argument("--raw-dir", default="data/raw",
                        help="Directory with the call ('-C-') and put ('-P-') raw CSVs")
    parser.add_argument("--catalog", default="catalog",
                        help="Catalog directory to (re)create")
    args = parser.parse_args()
    print(f"Converting raw data in {args.raw_dir} -> catalog {args.catalog}")
    build_catalog(args.raw_dir, args.catalog)


if __name__ == "__main__":
    main()
