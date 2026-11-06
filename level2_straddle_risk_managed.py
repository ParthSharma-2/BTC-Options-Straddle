#!/usr/bin/env python3
"""
level2_straddle_risk_managed.py — BTC ATM Short Straddle with SL & TP (Level 2)
================================================================================
A complete, self-contained NautilusTrader strategy that implements Level 2:

  1. Connects to Bybit (OPTION + LINEAR product types, Testnet or Demo).
  2. Discovers active BTC option contracts from cache.
  3. Subscribes to the BTC reference price (BTCUSDT-LINEAR).
  4. Dynamically selects the nearest expiry and ATM strike with both Call & Put.
  5. Executes the Short Straddle: SELL 1 ATM Call + SELL 1 ATM Put.
  6. Tracks full order lifecycle and open short positions.
  7. LEVEL 2 RISK MANAGEMENT:
     - Continuously monitors live mark-to-market and underlying movements.
     - Stop-Loss (1.0%): Automatically exits if adverse PnL reaches -1.0%
       or if underlying BTC moves >= 1.0% from ATM strike.
     - Take-Profit (2.0%): Automatically exits if profit reaches +2.0%.
     - Automated Exit: Submits BUY market orders (reduce_only=True) to flatten
       both legs simultaneously.
     - Full position closure tracking and realized PnL accounting.

Usage:
    python level2_straddle_risk_managed.py
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
from nautilus_trader.model.enums import OptionKind, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.instruments import CryptoOption
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


# ═════════════════════════════════════════════════════════════════════════════
# Strategy Configuration
# ═════════════════════════════════════════════════════════════════════════════

class Level2StraddleConfig(StrategyConfig, frozen=True):
    """
    Configuration for the Level 2 Risk-Managed Short Straddle strategy.

    Parameters
    ----------
    trade_quantity : str
        Contract quantity per leg (e.g. "0.01" for 0.01 BTC).
    underlying_symbol : str
        Instrument ID of the BTC reference price source.
    stop_loss_pct : float
        Stop-loss threshold as a percentage of entry premium (default: 1.0%).
    take_profit_pct : float
        Take-profit threshold as a percentage of entry premium (default: 2.0%).
    stop_loss_underlying_pct : float
        Stop-loss trigger if underlying BTC deviates by >= this % from ATM strike (default: 1.0%).
    """
    trade_quantity: str = "0.01"
    underlying_symbol: str = "BTCUSDT-LINEAR.BYBIT"
    stop_loss_pct: float = 1.0
    take_profit_pct: float = 2.0
    stop_loss_underlying_pct: float = 1.0


# ═════════════════════════════════════════════════════════════════════════════
# Strategy Implementation
# ═════════════════════════════════════════════════════════════════════════════

class Level2StraddleStrategy(Strategy):
    """
    Level 2 — BTC ATM Short Straddle with Automated Risk Management.
    """

    def __init__(self, config: Level2StraddleConfig) -> None:
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
        self._exit_triggered: bool = False
        self._exit_reason: str | None = None

        # Entry and exit tracking
        self._call_fill_price: float | None = None
        self._put_fill_price: float | None = None
        self._entry_premium: float | None = None

        self._call_closed: bool = False
        self._put_closed: bool = False
        self._total_realized_pnl: float = 0.0

    # ═════════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═════════════════════════════════════════════════════════════════════

    def on_start(self) -> None:
        """Strategy startup: discover options, select ATM, subscribe, execute."""
        self.log.info("=" * 70)
        self.log.info("LEVEL 2 — BTC ATM SHORT STRADDLE (RISK MANAGED) — STARTING")
        self.log.info(f"  Stop-Loss Threshold   : {self.config.stop_loss_pct:.1f}%")
        self.log.info(f"  Take-Profit Threshold : {self.config.take_profit_pct:.1f}%")
        self.log.info(f"  Underlying SL Band    : {self.config.stop_loss_underlying_pct:.1f}%")
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
        self.log.info("LEVEL 2 — STRATEGY SUMMARY")
        self.log.info("-" * 70)
        if self._atm_strike:
            self.log.info(f"  ATM Strike       : {self._atm_strike:,.0f}")
            self.log.info(f"  Call Contract    : {self._call_id}")
            self.log.info(f"  Put Contract     : {self._put_id}")
            self.log.info(f"  Total Entry Prem : {self._entry_premium}")
            self.log.info(f"  Exit Triggered   : {self._exit_triggered}")
            self.log.info(f"  Exit Reason      : {self._exit_reason}")
            self.log.info(f"  Total Realized PnL: {self._total_realized_pnl:+.4f}")
        self.log.info("=" * 70)

    # ═════════════════════════════════════════════════════════════════════
    # ATM DISCOVERY & SELECTION
    # ═════════════════════════════════════════════════════════════════════

    def _discover_and_select_atm(
        self, reference_price: float,
    ) -> tuple[CryptoOption, CryptoOption, float, int] | None:
        """Discover BTC options from cache and select nearest ATM strike."""
        venue = Venue("BYBIT")
        now_ns = time.time_ns()

        all_instruments = self.cache.instruments(venue=venue)
        options: list[CryptoOption] = [
            inst for inst in all_instruments
            if isinstance(inst, CryptoOption) and "BTC" in str(inst.id)
        ]

        if not options:
            self.log.error("No BTC option instruments found in cache.")
            return None

        # Exclude expired
        active_options = [o for o in options if o.expiration_ns > now_ns]
        if not active_options:
            self.log.error("All discovered BTC options are expired!")
            return None

        # Group by expiry, pick nearest
        expiry_map: dict[int, list[CryptoOption]] = {}
        for opt in active_options:
            expiry_map.setdefault(opt.expiration_ns, []).append(opt)

        sorted_expiries = sorted(expiry_map.keys())
        nearest_expiry = sorted_expiries[0]
        expiry_options = expiry_map[nearest_expiry]

        # Find strikes with both Call + Put
        strikes: dict[float, dict[OptionKind, CryptoOption]] = {}
        for opt in expiry_options:
            strike_val = float(opt.strike_price)
            kind = opt.option_kind
            strikes.setdefault(strike_val, {})[kind] = opt

        valid_strikes = {
            s: kinds for s, kinds in strikes.items()
            if OptionKind.CALL in kinds and OptionKind.PUT in kinds
        }

        if not valid_strikes:
            self.log.error("No strikes found with both Call and Put!")
            return None

        # Select ATM strike closest to reference price
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
            return

        qty = Quantity.from_str(self.config.trade_quantity)

        self.log.info("=" * 70)
        self.log.info("EXECUTING SHORT STRADDLE ENTRY")
        self.log.info("=" * 70)

        # SELL ATM Call
        call_order = self.order_factory.market(
            instrument_id=self._call_id,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(call_order)
        self.log.info(f"📤 SELL Call submitted | {self._call_id} | qty={qty}")

        # SELL ATM Put
        put_order = self.order_factory.market(
            instrument_id=self._put_id,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(put_order)
        self.log.info(f"📤 SELL Put submitted  | {self._put_id} | qty={qty}")

        self._straddle_executed = True

    def _exit_straddle(self, reason: str) -> None:
        """
        LEVEL 2 AUTOMATED EXIT:
        Closes both short legs using close_all_positions (submits BUY market orders
        with reduce_only=True and proper position tracking).
        """
        if self._exit_triggered:
            return

        self._exit_triggered = True
        self._exit_reason = reason
        qty = Quantity.from_str(self.config.trade_quantity)

        self.log.info("=" * 70)
        self.log.warning(f"🚨 RISK MANAGEMENT TRIGGERED EXIT: {reason}")
        self.log.info("=" * 70)

        # Close Call leg
        if self._call_id is not None:
            self.close_all_positions(self._call_id)
            self.log.info(f"📥 BUY Call (Exit) submitted | {self._call_id} | qty={qty}")

        # Close Put leg
        if self._put_id is not None:
            self.close_all_positions(self._put_id)
            self.log.info(f"📥 BUY Put (Exit) submitted  | {self._put_id} | qty={qty}")

    # ═════════════════════════════════════════════════════════════════════
    # MARKET DATA & RISK MONITORING
    # ═════════════════════════════════════════════════════════════════════

    def on_quote_tick(self, tick) -> None:
        """Handle quote ticks and monitor SL/TP risk thresholds."""
        iid = tick.instrument_id
        iid_str = str(iid)

        # ── Underlying reference price ───────────────────────────────────
        if iid == self._underlying_id:
            bid = float(tick.bid_price)
            ask = float(tick.ask_price)
            mid = (bid + ask) / 2.0
            self._last_underlying_mid = mid

            # Initial discovery trigger if not yet started
            if not self._straddle_executed and self._call_id is None:
                self.log.info(
                    f"Received underlying quote: mid={mid:,.2f} — selecting ATM strike..."
                )
                result = self._discover_and_select_atm(reference_price=mid)
                if result is not None:
                    call_inst, put_inst, atm_strike, expiry_ns = result
                    self._setup_straddle(call_inst, put_inst, atm_strike, expiry_ns)
                return

            # Check Underlying Stop-Loss condition
            if self._straddle_executed and not self._exit_triggered and self._atm_strike:
                price_deviation_pct = abs(mid - self._atm_strike) / self._atm_strike * 100.0
                if price_deviation_pct >= self.config.stop_loss_underlying_pct:
                    self._exit_straddle(
                        f"Stop-Loss: Underlying BTC moved {price_deviation_pct:.2f}% "
                        f"away from ATM strike (Threshold: {self.config.stop_loss_underlying_pct:.1f}%)"
                    )
            return

        # ── Option quotes & PnL calculation ──────────────────────────────
        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        mid = (bid + ask) / 2.0

        self._option_quotes[iid_str] = {"bid": bid, "ask": ask, "mid": mid}

        if self._straddle_executed and not self._exit_triggered and self._call_id and self._put_id:
            call_q = self._option_quotes.get(str(self._call_id))
            put_q = self._option_quotes.get(str(self._put_id))

            if call_q and put_q:
                combined_buyback_ask = call_q["ask"] + put_q["ask"]
                combined_mid = call_q["mid"] + put_q["mid"]

                if self._entry_premium is not None and self._entry_premium > 0:
                    unrealized_pnl = self._entry_premium - combined_mid
                    pnl_pct = (unrealized_pnl / self._entry_premium) * 100.0

                    self.log.info(
                        f"📊 STRADDLE MtM | "
                        f"Call Mid={call_q['mid']:.4f} Put Mid={put_q['mid']:.4f} | "
                        f"Buyback={combined_buyback_ask:.4f} | "
                        f"PnL={unrealized_pnl:+.4f} ({pnl_pct:+.2f}%) | "
                        f"BTC={self._last_underlying_mid:,.2f}"
                    )

                    # 1. Take-Profit Check (+2.0%)
                    if pnl_pct >= self.config.take_profit_pct:
                        self._exit_straddle(
                            f"Take-Profit: Profit reached {pnl_pct:+.2f}% "
                            f"(Target: +{self.config.take_profit_pct:.1f}%)"
                        )
                        return

                    # 2. Stop-Loss Check (-1.0%)
                    if pnl_pct <= -self.config.stop_loss_pct:
                        self._exit_straddle(
                            f"Stop-Loss: Adverse PnL reached {pnl_pct:+.2f}% "
                            f"(Threshold: -{self.config.stop_loss_pct:.1f}%)"
                        )
                        return

    # ═════════════════════════════════════════════════════════════════════
    # ORDER & POSITION EVENT HANDLERS
    # ═════════════════════════════════════════════════════════════════════

    def on_order_accepted(self, event) -> None:
        self.log.info(f"✅ ORDER ACCEPTED | id={event.client_order_id} | inst={event.instrument_id}")

    def on_order_rejected(self, event) -> None:
        self.log.error(f"❌ ORDER REJECTED | id={event.client_order_id} | reason={event.reason}")

    def on_order_filled(self, event) -> None:
        fill_price = float(event.last_px)
        fill_qty = float(event.last_qty)
        iid = event.instrument_id

        self.log.info(
            f"💰 ORDER FILLED | id={event.client_order_id} | "
            f"inst={iid} | side={event.order_side} | px={fill_price:.4f} | qty={fill_qty}"
        )

        # Entry Fill Tracking
        if not self._exit_triggered:
            if iid == self._call_id:
                self._call_fill_price = fill_price
            elif iid == self._put_id:
                self._put_fill_price = fill_price

            if self._call_fill_price is not None and self._put_fill_price is not None:
                self._entry_premium = self._call_fill_price + self._put_fill_price
                self.log.info(
                    f"🎯 STRADDLE FULLY OPENED | "
                    f"Call Prem={self._call_fill_price:.4f} | "
                    f"Put Prem={self._put_fill_price:.4f} | "
                    f"Total Collected={self._entry_premium:.4f}"
                )

    def on_position_opened(self, event) -> None:
        self.log.info(
            f"📂 POSITION OPENED | inst={event.instrument_id} | "
            f"side={event.entry} | qty={event.quantity} | avg_px={event.avg_px_open:.4f}"
        )

    def on_position_changed(self, event) -> None:
        self.log.info(
            f"📈 POSITION CHANGED | inst={event.instrument_id} | "
            f"qty={event.quantity} | pnl={event.unrealized_pnl}"
        )

    def on_position_closed(self, event) -> None:
        realized = float(event.realized_pnl)
        self._total_realized_pnl += realized

        duration_sec = event.duration_ns / 1e9 if hasattr(event, "duration_ns") else 0.0
        self.log.info(
            f"📕 POSITION CLOSED | inst={event.instrument_id} | "
            f"realized_pnl={realized:+.4f} | duration={duration_sec:.2f}s"
        )

        if event.instrument_id == self._call_id:
            self._call_closed = True
        elif event.instrument_id == self._put_id:
            self._put_closed = True

        if self._call_closed and self._put_closed:
            self.log.info("=" * 70)
            self.log.info(
                f"🏁 LEVEL 2 STRADDLE FULLY FLATTENED | "
                f"Reason: {self._exit_reason} | "
                f"Total Realized PnL: {self._total_realized_pnl:+.4f}"
            )
            self.log.info("=" * 70)


# ═════════════════════════════════════════════════════════════════════
# TRADING NODE RUNNER
# ═════════════════════════════════════════════════════════════════════

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

    print("\n" + "=" * 70)
    print(f"  LEVEL 2 — RISK MANAGED SHORT STRADDLE (SL=1.0%, TP=2.0%)")
    print(f"  Platform : NautilusTrader + {env_label}")
    print("=" * 70 + "\n")

    config_node = TradingNodeConfig(
        trader_id=TraderId("STRADDLE-L2-001"),
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

    node = TradingNode(config=config_node)
    node.add_data_client_factory(BYBIT, BybitLiveDataClientFactory)
    node.add_exec_client_factory(BYBIT, BybitLiveExecClientFactory)
    node.build()

    strategy = Level2StraddleStrategy(
        config=Level2StraddleConfig(
            trade_quantity="0.01",
            underlying_symbol="BTCUSDT-LINEAR.BYBIT",
            stop_loss_pct=1.0,
            take_profit_pct=2.0,
            stop_loss_underlying_pct=1.0,
        )
    )
    node.trader.add_strategy(strategy)

    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
