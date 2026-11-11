"""
Configuration for the BTC short-straddle assignment.

SAFETY RULES (hard):
- TestNet ONLY. There is no mainnet setting anywhere in this project.
- API keys come from environment variables ONLY. Never hardcode a key or
  secret in a file, never print one, never commit one.
- Orders are OFF unless the user passes --live-testnet AND both TestNet
  env vars are present. The default run is data-only discovery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Everything the node and strategy need, in one place."""

    # Bybit TestNet credentials — env vars only, never hardcoded.
    api_key: str | None = None
    api_secret: str | None = None

    # Reference price for the ATM strike: BTC spot on Bybit.
    reference_symbol: str = "BTCUSDT"
    venue: str = "BYBIT"

    # One contract per leg, as the assignment specifies.
    quantity: float = 1.0

    # Level 2 risk parameters (assignment values).
    stop_loss_pct: float = 0.01   # exit if buyback premium rises 1% over entry
    take_profit_pct: float = 0.02  # exit if buyback premium falls 2% under entry

    # How long the live node runs before stopping itself (seconds).
    run_seconds: int = 60

    # True only when the user explicitly asked for live TestNet orders.
    live_trading: bool = False

    @property
    def has_credentials(self) -> bool:
        """True only when BOTH TestNet key and secret are present."""
        return bool(self.api_key and self.api_secret)


def load_settings(live_trading: bool = False, run_seconds: int = 60) -> Settings:
    """
    Build settings from the environment.

    Reads BYBIT_TESTNET_API_KEY / BYBIT_TESTNET_API_SECRET only. Mainnet
    variables (BYBIT_API_KEY) are deliberately never read.
    """
    return Settings(
        api_key=os.environ.get("BYBIT_TESTNET_API_KEY") or None,
        api_secret=os.environ.get("BYBIT_TESTNET_API_SECRET") or None,
        run_seconds=run_seconds,
        live_trading=live_trading,
    )
