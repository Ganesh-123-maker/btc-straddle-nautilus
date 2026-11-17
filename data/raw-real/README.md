# Real raw data — obtained 17 Nov 2026

This folder holds REAL raw data (not the hand-made samples in
`../data/raw/`). Every file here was downloaded from a public source;
nothing was hand-written.

## `truefx-audusd-ticks.csv` — the docs' own fallback raw data

- **Source:** the NautilusTrader repository, tag `v1.231.0` (the same
  version this project pins):
  `https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v1.231.0/tests/test_data/truefx/audusd-ticks.csv`
- **Downloaded:** 17 Nov 2026, direct `curl` from `raw.githubusercontent.com`
  (HTTP 200, 4,899,290 bytes, md5 `874a8e5ad4e7ea666a9035ff008bd17d`).
- **What it is:** the 20,000-tick AUD/USD TrueFX sample the
  "Loading External Data" how-to falls back to when no histdata
  download is present — in the `v1.231.0` tag the file actually holds
  **100,000 ticks** (the how-to takes the first 20,000). It is FX spot
  data, NOT BTC options data: there is no BTC options raw data in the
  NautilusTrader docs.
- **Format:** header `timestamp,bid,ask`; rows like
  `2020-01-30 15:28:09.820000+00:00,0.67067,0.67070`
  (ISO timestamp with microseconds and `+00:00` offset).
- **Range:** 2020-01-30 15:28:09.820 UTC → 2020-01-31 20:59:54.501 UTC
  (100,000 rows; every row 3 columns; bid ≤ ask on every row —
  verified at download time).
- **Used by:** `backtest_fx_catalog.py` — parses it with
  `convert_raw_data.parse_raw_quotes`, writes it to a
  `ParquetDataCatalog`, reads it back, and runs it through the real
  `BacktestEngine` with a tick-counting strategy. The docs' how-to
  runs 20,000 ticks; this script does the same (first 20,000 from the
  catalog) so the run stays short.

## What is NOT here (blocked, recorded honestly)

- **histdata.com EUR/USD ticks:** the docs' primary raw-data source.
  Anonymous download does NOT work — the download page
  (`/download-free-forex-data/?/ascii/tick-data-quotes/EURUSD/2025/10`)
  only offers a POST form to `/get.php`; an anonymous POST with the
  page's own token returned an empty 200 first, then timed out
  (60 s, 0 bytes) on retry, and `getStatus.php` timed out the same
  way. Histdata free downloads need a registered account; no account
  was created (standing rule: no accounts, no credentials).
- **Bybit public REST (BTC options tickers/klines):** both
  `https://api.bybit.com/v5/market/tickers?category=option` and
  `https://api-testnet.bybit.com/v5/market/tickers?category=option`
  return **HTTP 403** — "The Amazon CloudFront distribution is
  configured to block access from your country" — the same geo-block
  as the TestNet websocket. No VPN or other-region account was used
  (standing rule). So no real BTC options raw data could be obtained
  from Bybit from this machine; the `data/raw/` BTC option CSVs remain
  labelled samples.
