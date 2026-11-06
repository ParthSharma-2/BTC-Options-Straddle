#!/usr/bin/env python3
"""
verify_connection.py — Bybit Testnet Connection & Option Instrument Diagnostic
================================================================================
Connects to Bybit Testnet via NautilusTrader, loads all BTC option + linear
instruments, and prints a structured summary of the discovered option chain.

Usage:
    1. Copy .env.example → .env and fill in your testnet API keys.
    2. pip install -r requirements.txt
    3. python verify_connection.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# ── NautilusTrader core ──────────────────────────────────────────────────────
from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit.common.enums import BybitProductType
from nautilus_trader.adapters.bybit.config import BybitDataClientConfig, BybitExecClientConfig
from nautilus_trader.adapters.bybit.factories import (
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)
from nautilus_trader.config import InstrumentProviderConfig, LoggingConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.common.component import LiveClock
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.config import StrategyConfig


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight strategy that runs diagnostics in on_start() then stops the node
# ─────────────────────────────────────────────────────────────────────────────
class DiagnosticConfig(StrategyConfig, frozen=True):
    """Minimal config for the diagnostic strategy."""
    pass


class DiagnosticStrategy(Strategy):
    """
    Connects, enumerates BTC option instruments from the cache, prints a
    summary table, and then stops the trading node.
    """

    def __init__(self, config: DiagnosticConfig) -> None:
        super().__init__(config)
        self._printed = False

    # ── Lifecycle ────────────────────────────────────────────────────────
    def on_start(self) -> None:
        self.log.info("=" * 70)
        self.log.info("BYBIT TESTNET — CONNECTION & INSTRUMENT DIAGNOSTIC")
        self.log.info("=" * 70)

        venue = Venue("BYBIT")
        all_instruments = self.cache.instruments(venue=venue)

        self.log.info(f"Total instruments loaded for BYBIT: {len(all_instruments)}")

        # ── Filter BTC options ───────────────────────────────────────────
        btc_options: list[CryptoOption] = []
        linear_count = 0

        for inst in all_instruments:
            if isinstance(inst, CryptoOption) and "BTC" in str(inst.id):
                btc_options.append(inst)
            elif "BTCUSDT-LINEAR" in str(inst.id):
                linear_count += 1

        self.log.info(f"BTC Option contracts found: {len(btc_options)}")
        self.log.info(f"BTCUSDT-LINEAR instruments found: {linear_count}")

        if not btc_options:
            self.log.warning(
                "⚠  No BTC option instruments found! "
                "Ensure your Bybit Testnet account has options trading enabled "
                "and that BTC options are available on the testnet."
            )
            self._stop_node()
            return

        # ── Group by expiry ──────────────────────────────────────────────
        now_ns = time.time_ns()
        expiry_map: dict[int, list[CryptoOption]] = {}

        for opt in btc_options:
            exp_ns = opt.expiration_ns
            expiry_map.setdefault(exp_ns, []).append(opt)

        self.log.info("-" * 70)
        self.log.info(f"{'Expiry Date':<22} {'Calls':>6} {'Puts':>6} {'Strikes':>8}  Status")
        self.log.info("-" * 70)

        for exp_ns in sorted(expiry_map.keys()):
            opts = expiry_map[exp_ns]
            calls = [o for o in opts if str(o.option_kind) in ("CALL", "OptionKind.CALL", "Call")]
            puts = [o for o in opts if str(o.option_kind) in ("PUT", "OptionKind.PUT", "Put")]
            unique_strikes = len(set(float(o.strike_price) for o in opts))
            exp_dt = datetime.fromtimestamp(exp_ns / 1e9, tz=timezone.utc)
            status = "ACTIVE" if exp_ns > now_ns else "EXPIRED"

            self.log.info(
                f"{exp_dt.strftime('%Y-%m-%d %H:%M UTC'):<22} "
                f"{len(calls):>6} {len(puts):>6} {unique_strikes:>8}  {status}"
            )

        # ── Print sample instruments ─────────────────────────────────────
        self.log.info("-" * 70)
        self.log.info("Sample BTC option instruments (first 10):")
        self.log.info("-" * 70)

        for opt in sorted(btc_options, key=lambda o: (o.expiration_ns, float(o.strike_price)))[:10]:
            exp_dt = datetime.fromtimestamp(opt.expiration_ns / 1e9, tz=timezone.utc)
            self.log.info(
                f"  {str(opt.id):<55} | "
                f"Strike={float(opt.strike_price):>10,.0f} | "
                f"Kind={str(opt.option_kind):<6} | "
                f"Expiry={exp_dt.strftime('%Y-%m-%d')}"
            )

        self.log.info("=" * 70)
        self.log.info("✅  Diagnostic complete. Connection to Bybit Testnet verified.")
        self.log.info("=" * 70)

        self._stop_node()

    def _stop_node(self) -> None:
        """Schedule node stop after a short delay to allow log flush."""
        self.clock.set_timer(
            name="stop_timer",
            interval=timedelta(seconds=3),
            callback=self._on_stop_timer,
        )

    def _on_stop_timer(self, event) -> None:
        self.log.info("Stopping trading node...")
        self.stop()

    def on_stop(self) -> None:
        self.log.info("Diagnostic strategy stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    load_dotenv()

    api_key = os.getenv("BYBIT_TESTNET_API_KEY") or os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_TESTNET_API_SECRET") or os.getenv("BYBIT_API_SECRET")

    is_demo = (
        os.getenv("BYBIT_DEMO", "false").lower() in ("true", "1", "yes")
        or os.getenv("BYBIT_ENV", "").lower() == "demo"
    )
    is_testnet = not is_demo
    env_label = "Bybit Demo Trading" if is_demo else "Bybit Testnet"

    if not api_key or api_key in ("your_testnet_api_key_here", "your_api_key_here"):
        print(
            f"\n[!] ERROR: {env_label} API keys not configured.\n"
            "   1. Copy .env.example -> .env\n"
            "   2. Add your Bybit API key and secret.\n"
            "   3. Re-run this script.\n"
        )
        sys.exit(1)

    print(f"\n[+] Connecting to {env_label}...")
    print(f"   API Key: {api_key[:8]}...{api_key[-4:]}\n")

    # ── Node configuration ───────────────────────────────────────────────
    config_node = TradingNodeConfig(
        trader_id=TraderId("DIAG-001"),
        logging=LoggingConfig(log_level="INFO"),
        data_clients={
            BYBIT: BybitDataClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                demo=is_demo,
                testnet=is_testnet,
                product_types=[BybitProductType.OPTION, BybitProductType.LINEAR],
                instrument_provider=InstrumentProviderConfig(load_all=True),
            ),
        },
        exec_clients={
            BYBIT: BybitExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                demo=is_demo,
                testnet=is_testnet,
                product_types=[BybitProductType.OPTION, BybitProductType.LINEAR],
                instrument_provider=InstrumentProviderConfig(load_all=True),
            ),
        },
    )

    # ── Build and run ────────────────────────────────────────────────────
    node = TradingNode(config=config_node)
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)
    node.build()

    strategy = DiagnosticStrategy(config=DiagnosticConfig())
    node.trader.add_strategy(strategy)

    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
