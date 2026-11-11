"""
Live runner: TradingNode + Bybit (TestNet only).

DEFAULT MODE IS DATA-ONLY: it subscribes to Bybit TestNet instruments and
BTC spot quotes, discovers the ATM strike, prints the chosen legs and live
quotes — and places NO orders. No API keys are needed for public data.

LIVE TESTNET MODE (places real testnet orders — testnet funds only):
    export BYBIT_TESTNET_API_KEY="..."
    export BYBIT_TESTNET_API_SECRET="..."
    python run_live.py --live-testnet --seconds 120

The node always stops itself after --seconds (default 60).
"""

from __future__ import annotations

import argparse
import threading
import time

from nautilus_trader.adapters.bybit import (
    BybitDataClientConfig,
    BybitEnvironment,
    BybitExecClientConfig,
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
    BybitProductType,
)
from nautilus_trader.config import LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import InstrumentId  # noqa: F401  (kept for clarity)
from nautilus_trader.trading.config import ImportableStrategyConfig

from config import load_settings
from strategy import BtcShortStraddle  # noqa: F401  (imported so the path resolves)


def build_node(settings) -> TradingNode:
    """Assemble the TradingNode: Bybit data client (+ exec client if live)."""
    # TradingNode takes strategies as importable configs (path + dict).
    strategy_config = ImportableStrategyConfig(
        strategy_path="strategy:BtcShortStraddle",
        config_path="strategy:BtcShortStraddleConfig",
        config={
            "reference_instrument_id": "BTCUSDT.BYBIT",
            "quantity": settings.quantity,
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
            "dry_run": not settings.live_trading,  # orders only in live-testnet mode
        },
    )

    data_client = BybitDataClientConfig(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        product_types=(BybitProductType.OPTION, BybitProductType.SPOT),
        environment=BybitEnvironment.TESTNET,  # TESTNET — never mainnet
    )

    node_config = TradingNodeConfig(
        trader_id="STRADDLE-001",
        logging=LoggingConfig(log_level="INFO"),
        strategies=[strategy_config],
        data_clients={"BYBIT": data_client},
    )

    if settings.live_trading:
        # Execution client only exists in live-testnet mode, with TestNet keys.
        node_config.exec_clients = {
            "BYBIT": BybitExecClientConfig(
                api_key=settings.api_key,
                api_secret=settings.api_secret,
                product_types=(BybitProductType.OPTION,),
                environment=BybitEnvironment.TESTNET,
            ),
        }

    return TradingNode(config=node_config)


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC straddle — Bybit TestNet (default: data-only)")
    parser.add_argument("--live-testnet", action="store_true",
                        help="Place REAL testnet orders (needs BYBIT_TESTNET_* env vars)")
    parser.add_argument("--seconds", type=int, default=60,
                        help="How long the node runs before stopping itself")
    args = parser.parse_args()

    settings = load_settings(live_trading=args.live_testnet, run_seconds=args.seconds)

    if args.live_testnet and not settings.has_credentials:
        raise SystemExit(
            "ERROR: --live-testnet needs BYBIT_TESTNET_API_KEY and "
            "BYBIT_TESTNET_API_SECRET in the environment. Refusing to run live."
        )
    if args.live_testnet:
        print("LIVE TESTNET MODE: real testnet orders will be placed. "
              "TestNet funds only — no mainnet, no real money.")
    else:
        print("DATA-ONLY MODE: subscribing + discovering the ATM strike. No orders.")

    node = build_node(settings)
    # Register the Bybit live factories — without these the node cannot
    # build a data/exec client for BYBIT at all.
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    if settings.live_trading:
        node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)
    node.build()

    # Run the node in its own thread so we can stop it after N seconds.
    thread = threading.Thread(target=node.run, daemon=True)
    thread.start()
    time.sleep(settings.run_seconds)
    print(f"\n{settings.run_seconds}s elapsed — stopping node...")
    node.stop()
    thread.join(timeout=30)
    node.dispose()
    print("Node stopped.")


if __name__ == "__main__":
    main()
