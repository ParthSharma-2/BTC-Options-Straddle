#!/usr/bin/env python3
"""
level1_naked_straddle.py — BTC ATM Short Straddle (Level 1)
=============================================================
A complete, self-contained NautilusTrader strategy that:

  1. Connects to Bybit Testnet (OPTION + LINEAR product types).
  2. Discovers available BTC option contracts from the instrument cache.
  3. Subscribes to the BTCUSDT-LINEAR reference price.
  4. Dynamically selects the nearest valid expiry and ATM strike where
     both a Call and Put exist.
  5. Subscribes to live QuoteTick data for both ATM legs.
  6. Executes a Short Straddle: SELL 1 ATM Call + SELL 1 ATM Put.
  7. Tracks the full order/position lifecycle and logs live mark-to-market
     combined straddle buyback quotes as the market moves.

Level 1 Boundaries:
  ✅  Open the straddle and observe.
  ❌  No Stop-Loss, Take-Profit, or automated exits.

Usage:
    1. Copy .env.example → .env and fill in your Bybit Testnet API keys.
    2. pip install -r requirements.txt
    3. python level1_naked_straddle.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# ── NautilusTrader core imports ──────────────────────────────────────────────
from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.adapters.bybit.common.enums import BybitProductType
from nautilus_trader.adapters.bybit.config import BybitDataClientConfig, BybitExecClientConfig
from nautilus_trader.adapters.bybit.factories import (
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    StrategyConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.enums import OptionKind, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


# ═════════════════════════════════════════════════════════════════════════════
# Strategy Configuration
# ═════════════════════════════════════════════════════════════════════════════

class ShortStraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for the Level 1 Short Straddle strategy.

    Parameters
    ----------
    trade_quantity : str
        Contract quantity per leg (e.g. "0.01" for 0.01 BTC).
    underlying_symbol : str
        Instrument ID of the BTC reference price source.
    """
    trade_quantity: str = "0.01"
    underlying_symbol: str = "BTCUSDT-LINEAR.BYBIT"


# ═════════════════════════════════════════════════════════════════════════════
# Strategy Implementation
# ═════════════════════════════════════════════════════════════════════════════

class ShortStraddleStrategy(Strategy):
    """
    Level 1 — BTC ATM Short Straddle.

    Lifecycle:
        on_start   → discover instruments → select ATM → subscribe → execute
        on_quote_tick → log live MtM
        on_order_*  → log order events
        on_position_* → log position events
        on_stop    → log final summary
    """

    def __init__(self, config: ShortStraddleConfig) -> None:
        super().__init__(config)

        # ── State ────────────────────────────────────────────────────────
        self._underlying_id: InstrumentId | None = None
        self._call_id: InstrumentId | None = None
        self._put_id: InstrumentId | None = None
        self._atm_strike: float | None = None
        self._expiry_ns: int | None = None

        self._last_underlying_mid: float | None = None
        self._option_quotes: dict[str, dict] = {}  # instrument_id_str → {bid, ask, mid}
        self._straddle_executed: bool = False

        # Track entry premiums for PnL logging
        self._call_fill_price: float | None = None
        self._put_fill_price: float | None = None

    # ═════════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═════════════════════════════════════════════════════════════════════

    def on_start(self) -> None:
        """Strategy startup: discover options, select ATM, subscribe, execute."""
        self.log.info("=" * 70)
        self.log.info("LEVEL 1 — BTC ATM SHORT STRADDLE — STARTING")
        self.log.info("=" * 70)

        # ── Step 1: Parse underlying instrument ID ───────────────────────
        self._underlying_id = InstrumentId.from_str(self.config.underlying_symbol)
        self.log.info(f"Underlying reference: {self._underlying_id}")

        # ── Step 2: Subscribe to underlying for reference price ──────────
        self.subscribe_quote_ticks(self._underlying_id)
        self.log.info(f"Subscribed to underlying quotes: {self._underlying_id}")

        # ── Step 3: Discover BTC option instruments ──────────────────────
        cached_quote = self.cache.quote_tick(self._underlying_id)
        ref_price = None
        if cached_quote is not None:
            ref_price = float(cached_quote.bid_price + cached_quote.ask_price) / 2.0
            self._last_underlying_mid = ref_price
            self.log.info(f"Cached reference price for {self._underlying_id}: {ref_price:,.2f}")

        if ref_price is not None:
            result = self._discover_and_select_atm(reference_price=ref_price)
            if result is not None:
                call_inst, put_inst, atm_strike, expiry_ns = result
                self._setup_straddle(call_inst, put_inst, atm_strike, expiry_ns)
        else:
            self.log.info(
                "Awaiting first underlying quote tick to dynamically select ATM strike..."
            )

    def _setup_straddle(
        self,
        call_inst: CryptoOption,
        put_inst: CryptoOption,
        atm_strike: float,
        expiry_ns: int,
    ) -> None:
        """Configure and execute the straddle after ATM selection."""
        self._call_id = call_inst.id
        self._put_id = put_inst.id
        self._atm_strike = atm_strike
        self._expiry_ns = expiry_ns

        expiry_dt = datetime.fromtimestamp(expiry_ns / 1e9, tz=timezone.utc)

        self.log.info("-" * 70)
        self.log.info("ATM STRIKE SELECTED")
        self.log.info(f"  Strike Price : {atm_strike:,.0f}")
        self.log.info(f"  Expiry       : {expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}")
        self.log.info(f"  Call Contract: {self._call_id}")
        self.log.info(f"  Put Contract : {self._put_id}")
        self.log.info(f"  Quantity/leg : {self.config.trade_quantity}")
        self.log.info("-" * 70)

        # ── Step 4: Subscribe to option quotes ───────────────────────────
        self.subscribe_quote_ticks(self._call_id)
        self.subscribe_quote_ticks(self._put_id)
        self.log.info(f"Subscribed to Call quotes: {self._call_id}")
        self.log.info(f"Subscribed to Put quotes:  {self._put_id}")

        # ── Step 5: Execute the short straddle ───────────────────────────
        self._execute_straddle()

    def on_stop(self) -> None:
        """Log final state summary when the strategy stops."""
        self.log.info("=" * 70)
        self.log.info("LEVEL 1 — SHORT STRADDLE — STOPPING")
        self.log.info("-" * 70)

        if self._atm_strike:
            self.log.info(f"  ATM Strike    : {self._atm_strike:,.0f}")
            self.log.info(f"  Call Contract : {self._call_id}")
            self.log.info(f"  Put Contract  : {self._put_id}")
            self.log.info(f"  Call Fill     : {self._call_fill_price}")
            self.log.info(f"  Put Fill      : {self._put_fill_price}")

        if self._last_underlying_mid:
            self.log.info(f"  Last BTC Mid  : {self._last_underlying_mid:,.2f}")

        # Log final option quotes if available
        if self._call_id and self._put_id:
            call_q = self._option_quotes.get(str(self._call_id))
            put_q = self._option_quotes.get(str(self._put_id))
            if call_q and put_q:
                combined = call_q["ask"] + put_q["ask"]
                self.log.info(f"  Final Buyback : {combined:.4f}")

        self.log.info("=" * 70)

    # ═════════════════════════════════════════════════════════════════════
    # ATM DISCOVERY & SELECTION
    # ═════════════════════════════════════════════════════════════════════

    def _discover_and_select_atm(
        self, reference_price: float | None = None,
    ) -> tuple[CryptoOption, CryptoOption, float, int] | None:
        """
        Discover BTC options from the cache and select the nearest ATM strike.

        Algorithm:
            1. Get all instruments from cache for venue BYBIT.
            2. Filter to CryptoOption instances with "BTC" in the symbol.
            3. Exclude expired contracts (expiration_ns ≤ now).
            4. Group by expiration → pick the NEAREST future expiry.
            5. Within that expiry, find all unique strikes.
            6. For each strike, check that BOTH a Call and Put exist.
            7. Among valid strikes, pick the one closest to reference_price.
            8. Return (call_instrument, put_instrument, strike, expiry_ns).

        Parameters
        ----------
        reference_price : float, optional
            The current BTC price. If None, the mid-point of the first
            available strike range is used as a rough estimate.

        Returns
        -------
        tuple or None
            (call_inst, put_inst, atm_strike, expiry_ns) or None if no
            valid option chain is found.
        """
        venue = Venue("BYBIT")
        now_ns = time.time_ns()

        # ── Step 1–2: Fetch all instruments, filter to BTC CryptoOptions ─
        all_instruments = self.cache.instruments(venue=venue)
        options: list[CryptoOption] = [
            inst for inst in all_instruments
            if isinstance(inst, CryptoOption)
            and "BTC" in str(inst.id)
        ]

        self.log.info(f"Total instruments on BYBIT: {len(all_instruments)}")
        self.log.info(f"BTC CryptoOption contracts found: {len(options)}")

        if not options:
            self.log.error(
                "No BTC option instruments found in cache. "
                "Ensure Bybit Testnet has BTC options available and that "
                "the instrument provider loaded them (load_all=True)."
            )
            return None

        # ── Step 3: Exclude expired ──────────────────────────────────────
        active_options = [o for o in options if o.expiration_ns > now_ns]
        self.log.info(f"Active (unexpired) BTC options: {len(active_options)}")

        if not active_options:
            self.log.error("All discovered BTC options are expired!")
            return None

        # ── Step 4: Group by expiry, pick nearest ────────────────────────
        expiry_map: dict[int, list[CryptoOption]] = {}
        for opt in active_options:
            expiry_map.setdefault(opt.expiration_ns, []).append(opt)

        sorted_expiries = sorted(expiry_map.keys())
        nearest_expiry = sorted_expiries[0]
        expiry_options = expiry_map[nearest_expiry]

        expiry_dt = datetime.fromtimestamp(nearest_expiry / 1e9, tz=timezone.utc)
        self.log.info(
            f"Nearest expiry selected: {expiry_dt.strftime('%Y-%m-%d %H:%M UTC')} "
            f"({len(expiry_options)} contracts)"
        )

        # ── Step 5–6: Find strikes with both Call + Put ──────────────────
        strikes: dict[float, dict[OptionKind, CryptoOption]] = {}
        for opt in expiry_options:
            strike_val = float(opt.strike_price)
            kind = opt.option_kind
            strikes.setdefault(strike_val, {})[kind] = opt

        valid_strikes = {
            s: kinds for s, kinds in strikes.items()
            if OptionKind.CALL in kinds and OptionKind.PUT in kinds
        }

        self.log.info(
            f"Strikes with both Call+Put: {len(valid_strikes)} "
            f"(out of {len(strikes)} total strikes)"
        )

        if not valid_strikes:
            self.log.error(
                "No strikes found with both a Call and Put contract! "
                "Cannot form a straddle."
            )
            return None

        # ── Step 7: Pick ATM strike ──────────────────────────────────────
        if reference_price is None:
            # Fallback: use the median of available strikes as rough reference
            sorted_strikes = sorted(valid_strikes.keys())
            reference_price = sorted_strikes[len(sorted_strikes) // 2]
            self.log.warning(
                f"No underlying reference price available yet. "
                f"Using median strike as proxy: {reference_price:,.0f}"
            )

        atm_strike = min(valid_strikes.keys(), key=lambda s: abs(s - reference_price))
        call_inst = valid_strikes[atm_strike][OptionKind.CALL]
        put_inst = valid_strikes[atm_strike][OptionKind.PUT]

        self.log.info(
            f"ATM Strike: {atm_strike:,.0f} "
            f"(reference price: {reference_price:,.2f}, "
            f"distance: {abs(atm_strike - reference_price):,.2f})"
        )

        return call_inst, put_inst, atm_strike, nearest_expiry

    # ═════════════════════════════════════════════════════════════════════
    # ORDER EXECUTION
    # ═════════════════════════════════════════════════════════════════════

    def _execute_straddle(self) -> None:
        """Submit SELL market orders for both ATM Call and ATM Put legs."""
        if self._straddle_executed:
            self.log.warning("Straddle already executed — skipping duplicate.")
            return

        qty = Quantity.from_str(self.config.trade_quantity)

        self.log.info("=" * 70)
        self.log.info("EXECUTING SHORT STRADDLE")
        self.log.info("=" * 70)

        # ── SELL the ATM Call ────────────────────────────────────────────
        call_order = self.order_factory.market(
            instrument_id=self._call_id,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(call_order)
        self.log.info(
            f"📤  SELL Call submitted | {self._call_id} | "
            f"qty={qty} | order_id={call_order.client_order_id}"
        )

        # ── SELL the ATM Put ─────────────────────────────────────────────
        put_order = self.order_factory.market(
            instrument_id=self._put_id,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(put_order)
        self.log.info(
            f"📤  SELL Put submitted  | {self._put_id} | "
            f"qty={qty} | order_id={put_order.client_order_id}"
        )

        self._straddle_executed = True

    # ═════════════════════════════════════════════════════════════════════
    # MARKET DATA HANDLER
    # ═════════════════════════════════════════════════════════════════════

    def on_quote_tick(self, tick) -> None:
        """
        Handle incoming quote ticks for:
          - The underlying (BTCUSDT-LINEAR) → update reference price.
          - ATM Call / Put → update option quotes and log combined MtM.
        """
        iid = tick.instrument_id
        iid_str = str(iid)

        # ── Underlying reference price ───────────────────────────────────
        if iid == self._underlying_id:
            bid = float(tick.bid_price)
            ask = float(tick.ask_price)
            mid = (bid + ask) / 2.0
            self._last_underlying_mid = mid

            # If straddle hasn't been executed yet and we have no ATM,
            # retry discovery with real reference price
            if not self._straddle_executed and self._call_id is None:
                self.log.info(
                    f"Received underlying quote: mid={mid:,.2f} — "
                    f"retrying ATM discovery with live price..."
                )
                result = self._discover_and_select_atm(reference_price=mid)
                if result is not None:
                    call_inst, put_inst, atm_strike, expiry_ns = result
                    self._setup_straddle(call_inst, put_inst, atm_strike, expiry_ns)
            return

        # ── Option quotes ────────────────────────────────────────────────
        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        mid = (bid + ask) / 2.0

        self._option_quotes[iid_str] = {
            "bid": bid,
            "ask": ask,
            "mid": mid,
        }

        # ── Combined straddle MtM ───────────────────────────────────────
        if self._straddle_executed and self._call_id and self._put_id:
            call_q = self._option_quotes.get(str(self._call_id))
            put_q = self._option_quotes.get(str(self._put_id))

            if call_q and put_q:
                combined_buyback_ask = call_q["ask"] + put_q["ask"]
                combined_mid = call_q["mid"] + put_q["mid"]

                # Calculate unrealized PnL if we have fill prices
                entry_premium = 0.0
                pnl_str = ""
                if self._call_fill_price is not None and self._put_fill_price is not None:
                    entry_premium = self._call_fill_price + self._put_fill_price
                    unrealized_pnl = entry_premium - combined_mid
                    pnl_str = (
                        f" | Entry Premium={entry_premium:.4f}"
                        f" | Unrealized PnL={unrealized_pnl:+.4f}"
                    )

                underlying_str = ""
                if self._last_underlying_mid is not None:
                    underlying_str = f" | BTC={self._last_underlying_mid:,.2f}"

                self.log.info(
                    f"📊 STRADDLE MtM | "
                    f"Call Bid/Ask={call_q['bid']:.4f}/{call_q['ask']:.4f} | "
                    f"Put Bid/Ask={put_q['bid']:.4f}/{put_q['ask']:.4f} | "
                    f"Combined Buyback={combined_buyback_ask:.4f} | "
                    f"Combined Mid={combined_mid:.4f}"
                    f"{pnl_str}{underlying_str}"
                )

    # ═════════════════════════════════════════════════════════════════════
    # ORDER EVENT HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def on_order_accepted(self, event) -> None:
        """Log when an order is accepted by the venue."""
        self.log.info(
            f"✅ ORDER ACCEPTED | order_id={event.client_order_id} | "
            f"instrument={event.instrument_id}"
        )

    def on_order_rejected(self, event) -> None:
        """Log when an order is rejected by the venue."""
        self.log.error(
            f"❌ ORDER REJECTED | order_id={event.client_order_id} | "
            f"instrument={event.instrument_id} | "
            f"reason={event.reason}"
        )

    def on_order_filled(self, event) -> None:
        """Log when an order is filled and track entry premiums."""
        fill_price = float(event.last_px)
        fill_qty = float(event.last_qty)

        self.log.info(
            f"💰 ORDER FILLED | order_id={event.client_order_id} | "
            f"instrument={event.instrument_id} | "
            f"side={event.order_side} | "
            f"price={fill_price:.4f} | "
            f"qty={fill_qty} | "
            f"commission={event.commission}"
        )

        # Track entry fill prices for PnL calculation
        iid = event.instrument_id
        if iid == self._call_id:
            self._call_fill_price = fill_price
            self.log.info(f"  → Call leg entry premium recorded: {fill_price:.4f}")
        elif iid == self._put_id:
            self._put_fill_price = fill_price
            self.log.info(f"  → Put leg entry premium recorded: {fill_price:.4f}")

        # Log when both legs are filled
        if self._call_fill_price is not None and self._put_fill_price is not None:
            total_premium = self._call_fill_price + self._put_fill_price
            self.log.info(
                f"🎯 STRADDLE FULLY FILLED | "
                f"Call Premium={self._call_fill_price:.4f} | "
                f"Put Premium={self._put_fill_price:.4f} | "
                f"Total Collected={total_premium:.4f}"
            )

    # ═════════════════════════════════════════════════════════════════════
    # POSITION EVENT HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def on_position_opened(self, event) -> None:
        """Log when a new position is opened."""
        self.log.info(
            f"📂 POSITION OPENED | instrument={event.instrument_id} | "
            f"side={event.entry} | "
            f"quantity={event.quantity} | "
            f"avg_price={event.avg_px_open:.4f}"
        )

    def on_position_changed(self, event) -> None:
        """Log when an existing position changes."""
        self.log.info(
            f"📈 POSITION CHANGED | instrument={event.instrument_id} | "
            f"side={event.entry} | "
            f"quantity={event.quantity} | "
            f"avg_open={event.avg_px_open:.4f} | "
            f"realized_pnl={event.realized_pnl} | "
            f"unrealized_pnl={event.unrealized_pnl}"
        )

    def on_position_closed(self, event) -> None:
        """Log when a position is closed (should not happen in Level 1)."""
        self.log.warning(
            f"📕 POSITION CLOSED (unexpected in Level 1) | "
            f"instrument={event.instrument_id} | "
            f"realized_pnl={event.realized_pnl} | "
            f"duration={event.duration}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# TRADING NODE RUNNER
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Configure TradingNode, register strategy, and run."""
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

    print("\n" + "=" * 70)
    print(f"  LEVEL 1 — BTC ATM SHORT STRADDLE — NautilusTrader + {env_label}")
    print("=" * 70)
    print(f"  API Key : {api_key[:8]}...{api_key[-4:]}")
    print(f"  Mode    : {env_label} (no real funds at risk)")
    print(f"  Strategy: Naked Short Straddle (no SL/TP/exit logic)")
    print("=" * 70 + "\n")

    # ── Node Configuration ───────────────────────────────────────────────
    config_node = TradingNodeConfig(
        trader_id=TraderId("STRADDLE-001"),
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_lookback_mins=1440,
        ),
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

    # ── Build node ───────────────────────────────────────────────────────
    node = TradingNode(config=config_node)
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)
    node.build()

    # ── Register strategy ────────────────────────────────────────────────
    strategy = ShortStraddleStrategy(
        config=ShortStraddleConfig(
            trade_quantity="0.01",
            underlying_symbol="BTCUSDT-LINEAR.BYBIT",
        )
    )
    node.trader.add_strategy(strategy)

    # ── Run ──────────────────────────────────────────────────────────────
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
