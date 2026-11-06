# BTC ATM Short Straddle — Level 1 (NautilusTrader + Bybit Testnet)

A **Level 1** implementation of a BTC At-The-Money (ATM) Short Straddle strategy using [NautilusTrader](https://nautilustrader.io/) connected to [Bybit Testnet](https://testnet.bybit.com/).

## What This Does

A **short straddle** involves simultaneously **selling** an ATM Call **and** an ATM Put option with the same strike price and expiry. The position profits when the underlying (BTC) stays near the strike price, and the collected premium exceeds any adverse price movement.

### Level 1 Scope

| ✅ Implemented | ❌ Not Implemented (Level 2+) |
|---|---|
| NautilusTrader TradingNode + Bybit Testnet connection | Stop-Loss orders |
| Automatic BTC option contract discovery | Take-Profit orders |
| Dynamic ATM strike selection from live BTC price | Automated exit conditions |
| SELL Call + SELL Put market order execution | Delta hedging |
| Full order lifecycle tracking (accepted → filled) | Position sizing logic |
| Live mark-to-market straddle quote logging | Multi-expiry management |
| Position opened/changed event logging | Greeks monitoring |

---

## Prerequisites

- **Python 3.10+** (3.11 or 3.12 recommended)
- **Bybit Testnet Account** with:
  - Unified Trading Account (UTA) enabled
  - Options + Derivatives trading permissions
  - API keys generated at: https://testnet.bybit.com/app/user/api-management
- **Testnet BTC balance** (claim testnet funds from the Bybit faucet)

---

## Setup

### 1. Clone and Install Dependencies

```bash
cd btc-options-straddle
pip install -r requirements.txt
```

> **Note:** If the latest `nautilus_trader` is a pre-release, you may need:
> ```bash
> pip install -U nautilus_trader --pre
> ```

### 2. Configure API Keys

```bash
cp .env.example .env
```

Edit `.env` with your Bybit Testnet API credentials:

```env
BYBIT_TESTNET_API_KEY=your_actual_key_here
BYBIT_TESTNET_API_SECRET=your_actual_secret_here
```

### 3. Verify Connection

Run the diagnostic script first to confirm everything works:

```bash
python verify_connection.py
```

**Expected output:**
- ✅ Connection to Bybit Testnet established
- ✅ BTC option instruments discovered and enumerated
- ✅ Expiry dates, strikes, call/put counts printed

### 4. Run the Strategy (Live Bybit Testnet)

**Level 1 (Naked Short Straddle — No Automated Exits):**
```bash
python level1_naked_straddle.py
```

**Level 2 (Risk Managed Short Straddle — SL 1.0% / TP 2.0% Exits):**
```bash
python level2_straddle_risk_managed.py
```

The strategy will:
1. Connect to Bybit Testnet
2. Load all BTC option + linear instruments
3. Subscribe to `BTCUSDT-LINEAR` for the BTC reference price
4. Select the nearest expiry and ATM strike with both Call + Put
5. Submit 2x SELL market orders (1 Call + 1 Put)
6. Continuously log order events and mark-to-market straddle quotes
7. **(Level 2 only):** Continuously monitor Stop-Loss (1.0% underlying move or adverse PnL) and Take-Profit (+2.0% decay) thresholds, and automatically submit closing BUY market orders (`reduce_only=True`) to flatten both legs simultaneously.

**Press `Ctrl+C` to stop.**

### 5. Run Offline Simulation Mode (Zero Keys or Network Needed)

If you are subject to regional ISP blocks (such as Indian FIU blocks on offshore exchange domains) or want to test the entire strategy lifecycle instantly without an exchange account:

**Level 1 Simulation (Entry, Fills, MtM Tracking):**
```bash
python run_simulation.py
```

**Level 2 Simulation (Take-Profit & Stop-Loss Automated Exits):**
```bash
python run_simulation_level2.py
```

This runs NautilusTrader's native `BacktestEngine` with a simulated Bybit matching exchange, loads an unexpired BTC option chain, streams realistic market price movements, dynamically selects the ATM strike, executes the short straddle, fills both orders, opens positions, verifies automated Take-Profit and Stop-Loss triggers, and logs realized PnL accounting upon closure.

---

## Project Structure

```
btc-options-straddle/
├── interview_prep/                  # 🎓 Dedicated Interview Preparation & Documentation Package
│   ├── BTC_Options_Straddle_Interview_Master_Guide.pdf  # Publication-quality 8-page interview master PDF
│   ├── INTERVIEW_MASTER_GUIDE.md    # Complete markdown interview guide (quant thesis, architecture, Q&A)
│   ├── Agent.md                     # Agent specification document
│   ├── walkthrough.md               # End-to-end strategy & simulation walkthrough
│   └── build_interview_pdf.py       # Standalone PDF generator script
├── .env.example                     # Template for Bybit Testnet API credentials
├── .env                             # Your actual credentials (git-ignored)
├── requirements.txt                 # Pinned dependencies
├── README.md                        # Project documentation
├── verify_connection.py             # Diagnostic: verify API + list option instruments
├── level1_naked_straddle.py         # Complete Level 1 Strategy + TradingNode runner
├── level2_straddle_risk_managed.py  # Complete Level 2 Strategy with SL (1%) & TP (2%)
├── run_simulation.py                # Level 1 offline simulation (BacktestEngine)
└── run_simulation_level2.py         # Level 2 offline simulation (SL & TP verification)
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      TradingNode (STRADDLE-001)                  │
│                                                                  │
│  ┌──────────────────┐     ┌──────────────────────────────────┐  │
│  │ BybitDataClient   │     │ ShortStraddleStrategy             │  │
│  │ ─ OPTION          │────▶│                                    │  │
│  │ ─ LINEAR          │     │  on_start()                       │  │
│  └──────────────────┘     │   ├─ Discover CryptoOption insts  │  │
│                            │   ├─ Select nearest ATM strike    │  │
│  ┌──────────────────┐     │   ├─ Subscribe quote ticks         │  │
│  │ BybitExecClient   │     │   └─ Execute SELL Call + Put      │  │
│  │ ─ OPTION          │◀────│                                    │  │
│  │ ─ LINEAR          │     │  on_quote_tick()                  │  │
│  └──────────────────┘     │   └─ Log combined straddle MtM    │  │
│                            │                                    │  │
│  ┌──────────────────┐     │  on_order_filled()                 │  │
│  │ Instrument Cache  │     │   └─ Track entry premiums         │  │
│  │ (CryptoOption)    │     │                                    │  │
│  └──────────────────┘     │  on_position_opened/changed()      │  │
│                            │   └─ Log position state            │  │
│                            └──────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

## Expected Log Output

When running successfully, you'll see structured logs like:

```
LEVEL 1 — BTC ATM SHORT STRADDLE — STARTING
──────────────────────────────────────────────
Underlying reference: BTCUSDT-LINEAR.BYBIT
Subscribed to underlying quotes: BTCUSDT-LINEAR.BYBIT
Total instruments on BYBIT: 1523
BTC CryptoOption contracts found: 847
Active (unexpired) BTC options: 312
Nearest expiry selected: 2026-11-07 08:00 UTC (48 contracts)
Strikes with both Call+Put: 24 (out of 24 total strikes)
ATM Strike: 72000 (reference price: 71,834.50, distance: 165.50)
──────────────────────────────────────────────
Call Contract: BTC-7NOV26-72000-C-OPTION.BYBIT
Put Contract : BTC-7NOV26-72000-P-OPTION.BYBIT
Quantity/leg : 0.01
══════════════════════════════════════════════
EXECUTING SHORT STRADDLE
📤 SELL Call submitted | BTC-7NOV26-72000-C-OPTION.BYBIT | qty=0.01
📤 SELL Put submitted  | BTC-7NOV26-72000-P-OPTION.BYBIT | qty=0.01
✅ ORDER ACCEPTED | order_id=...
💰 ORDER FILLED | price=0.0435 | qty=0.01 | commission=...
📂 POSITION OPENED | instrument=BTC-7NOV26-72000-C-OPTION.BYBIT | side=SELL
...
📊 STRADDLE MtM | Call Ask=0.0440 Put Ask=0.0420 | Combined Buyback=0.0860
```

## Troubleshooting

| Issue | Solution |
|---|---|
| `No BTC option instruments found` | Check that your Bybit Testnet account has UTA enabled and options trading permissions. BTC options may not always be available on testnet. |
| `ORDER REJECTED` | Ensure you have sufficient testnet USDC margin. Options on Bybit require USDC collateral in a UTA. |
| `Connection timeout` | Bybit Testnet can be intermittently slow. Retry after a few minutes. |
| `ImportError: nautilus_trader` | Run `pip install -U nautilus_trader --pre` to get the latest version. |
| `API key invalid` | Double-check your `.env` file. Testnet and mainnet keys are different. |
| `All discovered BTC options are expired` | All near-term contracts may have expired. Wait for new expiries to be listed, or check the Bybit Testnet options chain manually. |

---

## License

Educational use. Not financial advice. Use at your own risk — even on testnet.
