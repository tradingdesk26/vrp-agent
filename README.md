# vrp-agent

Autonomous trading agent that:

- Reads volatility regime (implied vs realised) every minute
- Opens a daily long on **Hyperliquid ETH-PERP** during a session window
- Parks idle capital in a **Uniswap v4 LP on Base** (ETH/USDC) when the regime
  is constructive
- Exits to USDC ("defensive cash") when the regime turns risk-off

Self-custody — **you run it yourself with your own wallet keys**. The agent
never sees other people's funds. Code is MIT-licensed.

Strategy backtest, deployment addresses, research charts:
[github.com/tradingdesk26/regimeshift-fx](https://github.com/tradingdesk26/regimeshift-fx).

---

## Run it yourself in 10 minutes

### Prerequisites

1. **A funded wallet on Base mainnet** with:
   - ~$0.01 ETH for gas (a few cents)
   - At least $15 USDC to trade with (more is better — bridge cost is fixed,
     edge scales with capital)
2. **A Hyperliquid account** at the same address (open it once at
   [app.hyperliquid.xyz](https://app.hyperliquid.xyz) — no deposit needed,
   agent will bridge USDC there itself)
3. **Python 3.10+** and **git**
4. A private Base RPC endpoint (Alchemy / QuickNode / Chainstack — the
   public endpoint rate-limits aggressively)

### Setup

```bash
git clone https://github.com/tradingdesk26/vrp-agent
cd vrp-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your editor of choice (see "Configuration" below)
```

### Configuration

Open `.env` and fill in:

```bash
BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/<your-key>
HL_PRIVATE_KEY=<hex private key of your funded wallet>

# Start in dry-run mode to verify the loop works without spending money
DRY_RUN=true

# Position size cap in USD. With $15 capital, $15 is fine. Scale up as you
# add capital — the strategy benefits from higher capital because per-trade
# bridge/gas costs are roughly fixed.
MAX_POSITION_USD=15

# Daily loss cap — agent halts trading if cumulative daily PnL goes below
# -DAILY_LOSS_LIMIT_USD. Tune to your risk appetite.
DAILY_LOSS_LIMIT_USD=1
```

Optional but recommended:
```bash
# A separate Hyperliquid API wallet (limits blast radius if leaked).
# Generate at app.hyperliquid.xyz → API. Leave empty to sign with main key.
HL_API_WALLET_KEY=<api wallet hex key>
```

### Pre-flight check

```bash
python3 -m src.check_wallet
```

Should print balances on Base + Hyperliquid. If anything is missing or
errors, fix before going live.

### Dry run

```bash
DRY_RUN=true python3 -m src.main
```

You'll see VRP signals, decision logging, "would-trade" lines — but no
on-chain transactions. Run for at least 30 minutes to confirm the loop
is healthy.

### Live

```bash
DRY_RUN=false python3 -m src.main
```

The agent now trades for real. The default session window is 20:00–22:00
UTC daily for the directional trade; outside the window, capital sits in
the LP (if VRP is constructive) or on Hyperliquid as USDC (otherwise).

---

## Run as a systemd service (Linux)

For 24/7 unattended operation, install as a service:

```bash
# Place the repo at /opt/vrp-agent and create a system user
sudo useradd -r -s /bin/false -d /opt/vrp-agent vrpagent
sudo mkdir -p /opt/vrp-agent
sudo chown vrpagent:vrpagent /opt/vrp-agent
sudo -u vrpagent git clone https://github.com/tradingdesk26/vrp-agent /opt/vrp-agent

# Configure .env as above
sudo -u vrpagent cp /opt/vrp-agent/.env.example /opt/vrp-agent/.env
sudo -u vrpagent $EDITOR /opt/vrp-agent/.env

# Install
sudo /opt/vrp-agent/deploy/install.sh
sudo systemctl start vrp-agent
sudo journalctl -u vrp-agent -f
```

Override `AGENT_USER=youruser` or `TARGET=/path` env vars to install at a
different location or for a different user.

---

## Optional: local dashboard

```bash
python3 -m src.dashboard            # render once → out/index.html + out/chart.png
python3 -m http.server --directory out 8082    # serve at http://localhost:8082
```

The dashboard shows current state, current VRP, open position, and last
30 trades. There are systemd unit files in `deploy/` for running the
renderer every 5 minutes and the HTTP server continuously.

---

## How it works

### State machine

```
   ┌──────────────────────────────────────────────────────┐
   │  CASH_ON_HL  ◀────────┐                              │
   │      │                │                              │
   │      │ session entry  │ session exit (vol regime ≤ 0)│
   │      ▼                │                              │
   │  LONG_ON_HL ──────────┤                              │
   │      ▲                │                              │
   │      │ session entry  │ session exit (vol regime >0) │
   │      │                ▼                              │
   │  PARKED_IN_LP ◀───────┘                              │
   │      ▲                                               │
   │      │ regime recovers                              │
   │      │                                               │
   │  DEFENSIVE_CASH ◀── regime crash trigger             │
   └──────────────────────────────────────────────────────┘
```

State is **always reconciled from on-chain reality** at every poll, so
restarts and crashes never desync the agent from chain state.

### Decision logic

All in [`src/session_strategy.py`](src/session_strategy.py) as a pure
function — easy to read, easy to unit-test (see
[`test_session_strategy.py`](test_session_strategy.py)).

### Cross-chain pipelines

The agent handles the full **Base ⇄ HyperEVM ⇄ Hyperliquid** round-trip
end-to-end via CCTP V2:

- LP burn → Uni v3 swap → CCTP burn → Iris attestation → HyperEVM deposit
  → market open (~60 s end-to-end)
- market close → Hyperliquid spot transfer → CCTP burn → Iris attestation
  → Uni v3 swap → LP mint (~60 s end-to-end)

Both pipelines validated end-to-end on mainnet.

---

## Risks and disclaimers

**This is experimental software trading real money on a real exchange.**

- You can lose your entire deposited capital, including via:
  - Strategy losing money on a directional trade
  - Smart contract bug in our hook contract, Uniswap v4 POSM, or CCTP
  - Bug in this agent
  - Cross-chain bridge incident
  - Hyperliquid liquidation
- The agent uses your private key locally; never paste your key anywhere
  that isn't your own machine.
- Start with the smallest capital you'd be ok losing entirely. Scale only
  after you've watched a full Phase-2 cycle complete cleanly.
- **Not investment advice.** Not a regulated investment product. Just
  open-source software that does what the code says.

---

## Architecture details

- [`src/main.py`](src/main.py) — main loop
- [`src/session_strategy.py`](src/session_strategy.py) — decision function
- [`src/phase_session.py`](src/phase_session.py) — entry/exit orchestrators
- [`src/phase2_forward.py`](src/phase2_forward.py) — full PARKED_IN_LP → LONG_ON_HL pipeline
- [`src/phase2_reverse.py`](src/phase2_reverse.py) — full LONG_ON_HL → PARKED_IN_LP pipeline
- [`src/phase_defensive.py`](src/phase_defensive.py) — defensive triggers (LP ⇄ stable USDC on Base)
- [`src/state_machine.py`](src/state_machine.py) — on-chain state reconciler
- [`src/hl_executor.py`](src/hl_executor.py) — Hyperliquid SDK wrapper
- [`src/on_chain/`](src/on_chain/) — Base-side primitives (CCTP V2, Uni v4 POSM, Uni v3 SwapRouter02)
- [`src/signal_engine.py`](src/signal_engine.py) — VRP signal compute (Parkinson realised vol minus implied vol)
- [`src/pnl_tracker.py`](src/pnl_tracker.py) — SQLite-backed trade + state log
- [`src/dashboard.py`](src/dashboard.py) — static HTML dashboard renderer

---

## License

MIT. See [`LICENSE`](LICENSE).

## Contributing

This is a hackathon submission for the Agora Agents Hackathon (Canteen ×
Circle, May 11–25 2026). PRs welcome; issues welcome; share what you
build with it.
