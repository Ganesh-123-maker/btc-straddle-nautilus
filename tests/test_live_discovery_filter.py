"""
Regression test for the third live-run bug (11 Nov 2026).

Bybit options arrive as CryptoOption, not OptionContract — the two are
sibling Instrument subclasses. The strategy filtered on OptionContract
only, so a live run loaded 1,439 instruments, got a real BTC price
(73,776.10 USDT), and still reported "0 option instruments seen".
The backtest never caught it: it uses pre-selected legs and never
goes through on_instrument / the cache fallback.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import make_btc_option  # noqa: E402
from strategy import is_option_instrument  # noqa: E402


def test_crypto_option_is_accepted_as_an_option():
    call = make_btc_option("CALL", "120000.00")
    put = make_btc_option("PUT", "120000.00")
    assert is_option_instrument(call) is True
    assert is_option_instrument(put) is True


def test_non_option_is_rejected():
    assert is_option_instrument("BTCUSDT-SPOT.BYBIT") is False
    assert is_option_instrument(None) is False
