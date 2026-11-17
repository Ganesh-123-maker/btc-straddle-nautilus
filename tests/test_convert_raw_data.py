"""Tests for the raw-data -> Nautilus converter (convert_raw_data.py).

Covers both documented header variants (bid_price/ask_price and
bid/ask), both timestamp formats (ISO and histdata-style), sorting,
rejection of bad rows, and the catalog round-trip (write then query).
"""

from pathlib import Path

import pytest

from backtest import make_btc_option
from convert_raw_data import build_catalog, load_catalog_ticks, parse_raw_quotes

CALL = make_btc_option("CALL", "120000.00")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_parser_iso_timestamps_and_bid_price_headers(tmp_path):
    path = _write(tmp_path, "call.csv",
                  "timestamp,bid_price,ask_price\n"
                  "2026-01-05T09:00:00Z,1000.00,1005.00\n"
                  "2026-01-05T09:01:00Z,990.00,995.00\n")
    ticks = parse_raw_quotes(path, CALL)
    assert len(ticks) == 2
    assert str(ticks[0].bid_price) == "1000.00"
    assert str(ticks[0].ask_price) == "1005.00"
    assert ticks[0].instrument_id == CALL.id
    assert ticks[0].ts_event < ticks[1].ts_event


def test_parser_histdata_timestamps_and_short_headers(tmp_path):
    path = _write(tmp_path, "put.csv",
                  "timestamp,bid,ask\n"
                  "20260105 090000000,900.00,905.00\n"
                  "20260105 090100000,890.00,895.00\n")
    ticks = parse_raw_quotes(path, CALL)
    assert len(ticks) == 2
    assert str(ticks[1].bid_price) == "890.00"
    # Histdata 09:00:00.000 == ISO 09:00:00Z in nanoseconds.
    iso = _write(tmp_path, "iso.csv",
                 "timestamp,bid,ask\n2026-01-05T09:00:00Z,900.00,905.00\n")
    assert parse_raw_quotes(iso, CALL)[0].ts_event == ticks[0].ts_event


def test_parser_sorts_out_of_order_rows(tmp_path):
    path = _write(tmp_path, "o.csv",
                  "timestamp,bid,ask\n"
                  "2026-01-05T09:02:00Z,990.00,995.00\n"
                  "2026-01-05T09:00:00Z,1000.00,1005.00\n"
                  "2026-01-05T09:01:00Z,1000.00,1005.00\n")
    ticks = parse_raw_quotes(path, CALL)
    assert [str(t.bid_price) for t in ticks] == ["1000.00", "1000.00", "990.00"]
    assert ticks[0].ts_event < ticks[1].ts_event < ticks[2].ts_event


def test_parser_skips_comment_lines(tmp_path):
    path = _write(tmp_path, "c.csv",
                  "# SAMPLE DATA — not real history\n"
                  "timestamp,bid,ask\n"
                  "2026-01-05T09:00:00Z,1000.00,1005.00\n")
    assert len(parse_raw_quotes(path, CALL)) == 1


def test_parser_rejects_bid_greater_than_ask(tmp_path):
    path = _write(tmp_path, "bad.csv",
                  "timestamp,bid,ask\n2026-01-05T09:00:00Z,1010.00,1005.00\n")
    with pytest.raises(ValueError, match="bid.*> ask"):
        parse_raw_quotes(path, CALL)


def test_parser_rejects_non_positive_price(tmp_path):
    path = _write(tmp_path, "bad.csv",
                  "timestamp,bid,ask\n2026-01-05T09:00:00Z,0.00,1005.00\n")
    with pytest.raises(ValueError, match="must be positive"):
        parse_raw_quotes(path, CALL)


def test_parser_rejects_bad_timestamp(tmp_path):
    path = _write(tmp_path, "bad.csv",
                  "timestamp,bid,ask\nnot-a-date,1000.00,1005.00\n")
    with pytest.raises(ValueError, match="unparseable timestamp"):
        parse_raw_quotes(path, CALL)


def test_parser_rejects_empty_file(tmp_path):
    path = _write(tmp_path, "empty.csv", "timestamp,bid,ask\n")
    with pytest.raises(ValueError, match="empty file"):
        parse_raw_quotes(path, CALL)


def test_parser_rejects_missing_column(tmp_path):
    path = _write(tmp_path, "bad.csv",
                  "timestamp,bid\n2026-01-05T09:00:00Z,1000.00\n")
    with pytest.raises(ValueError, match="missing required column"):
        parse_raw_quotes(path, CALL)


def test_catalog_round_trip(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "BTC-25DEC26-120000-C-USDT-OPTION.csv").write_text(
        "timestamp,bid_price,ask_price\n"
        "2026-01-05T09:00:00Z,1000.00,1005.00\n"
        "2026-01-05T09:01:00Z,975.00,980.00\n")
    (raw_dir / "BTC-25DEC26-120000-P-USDT-OPTION.csv").write_text(
        "timestamp,bid,ask\n"
        "20260105 090000000,900.00,905.00\n"
        "20260105 090100000,875.00,880.00\n")
    catalog_path = tmp_path / "catalog"
    build_catalog(raw_dir, catalog_path)

    call = make_btc_option("CALL", "120000.00")
    put = make_btc_option("PUT", "120000.00")
    call_ticks = load_catalog_ticks(catalog_path, call.id)
    put_ticks = load_catalog_ticks(catalog_path, put.id)
    assert len(call_ticks) == 2 and len(put_ticks) == 2
    assert str(call_ticks[0].bid_price) == "1000.00"
    assert str(call_ticks[-1].ask_price) == "980.00"
    assert str(put_ticks[0].bid_price) == "900.00"
    assert str(put_ticks[-1].ask_price) == "880.00"
    # Instruments were written too, so the catalog can link ticks to them.
    from nautilus_trader.persistence.catalog import ParquetDataCatalog
    ids = [i.id.value for i in ParquetDataCatalog(str(catalog_path)).instruments()]
    assert call.id.value in ids and put.id.value in ids


def test_build_catalog_requires_both_legs(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "BTC-25DEC26-120000-C-USDT-OPTION.csv").write_text(
        "timestamp,bid,ask\n2026-01-05T09:00:00Z,1000.00,1005.00\n")
    with pytest.raises(ValueError, match="exactly one call.*one put"):
        build_catalog(raw_dir, tmp_path / "catalog")


# --- Headerless histdata files (real histdata ASCII tick files have NO
# header line: rows are "20260105 090000000,bid,ask"). Fixtures below
# are small synthetic rows in that exact documented format. ---

def test_parser_headerless_histdata_rows(tmp_path):
    path = _write(tmp_path, "hist.csv",
                  "20260105 090000000,1000.00,1005.00\n"
                  "20260105 090100000,990.00,995.00\n")
    ticks = parse_raw_quotes(path, CALL)
    assert len(ticks) == 2
    assert str(ticks[0].bid_price) == "1000.00"
    assert str(ticks[1].ask_price) == "995.00"
    # Same instant as the ISO form.
    iso = _write(tmp_path, "iso.csv",
                 "timestamp,bid,ask\n2026-01-05T09:00:00Z,1000.00,1005.00\n")
    assert parse_raw_quotes(iso, CALL)[0].ts_event == ticks[0].ts_event


def test_parser_headerless_histdata_without_millis(tmp_path):
    path = _write(tmp_path, "hist.csv",
                  "20260105 090000,1000.00,1005.00\n")
    ticks = parse_raw_quotes(path, CALL)
    assert len(ticks) == 1
    assert str(ticks[0].bid_price) == "1000.00"


def test_parser_headerless_histdata_after_comment(tmp_path):
    path = _write(tmp_path, "hist.csv",
                  "# histdata ASCII tick file, no header\n"
                  "20260105 090000000,1000.00,1005.00\n")
    assert len(parse_raw_quotes(path, CALL)) == 1


def test_parser_headerless_histdata_rejects_bad_row(tmp_path):
    path = _write(tmp_path, "hist.csv",
                  "20260105 090000000,1010.00,1005.00\n")
    with pytest.raises(ValueError, match="bid.*> ask"):
        parse_raw_quotes(path, CALL)


def test_parser_truefx_iso_space_timestamp(tmp_path):
    """The real TrueFX file's format: header + space-separated ISO
    timestamp with microseconds and a +00:00 offset."""
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    audusd = TestInstrumentProvider.default_fx_ccy("AUD/USD")
    path = _write(tmp_path, "truefx.csv",
                  "timestamp,bid,ask\n"
                  "2020-01-30 15:28:09.820000+00:00,0.67067,0.67070\n"
                  "2020-01-30 15:28:09.823000+00:00,0.67067,0.67069\n")
    ticks = parse_raw_quotes(path, audusd)
    assert len(ticks) == 2
    assert str(ticks[0].bid_price) == "0.67067"
    assert str(ticks[0].ask_price) == "0.67070"
    assert ticks[0].ts_event < ticks[1].ts_event
