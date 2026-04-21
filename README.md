# volscalp

High-frequency intraday options scalping engine for **NIFTY 50** and **BANKNIFTY**, implementing the Repeated OTM Strangle strategy defined in [`FRD.md`](./FRD.md). Paper and live (Dhan) modes share identical strategy logic; divergence is treated as a bug.

> **Status:** v0.1 — code complete for the core loop, not yet validated against a live Dhan session. See [Validation checklist](#validation-checklist) before going live.

## Architecture

Single Python process, single asyncio loop, `uvloop` when available.

```
 Dhan WS ──► tick queue ──► MarketDataService ──► CandleBuilder
                                  │  │  │
                                  │  │  └─► StrategyEngine (per index)
                                  │  │         │
                                  │  │         ├─► OrderManager ──► PaperBackend | LiveBackend → Dhan REST
                                  │  │         └─► MtmController (lock-and-trail)
                                  │  └─► EventBus ──► FastAPI /ws ──► Browser dashboard
                                  └──────────► Database (SQLite, WAL)
```

### Design principles
- **Zero disk I/O on the hot path.** DB writes happen on bar close / order events, not per tick.
- **Frozen dataclasses for ticks.** Minimal allocations; engine state is mutable and owned by a single coroutine.
- **Bounded tick queue with drop-oldest overflow.** The strategy always sees the freshest price, even under load.
- **Paper backend reuses the live market feed.** Only the order path differs.

## Quick start

```bash
# 1. Clone and set up
git clone <repo-url>
cd claude-vol-trading
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Configure secrets (NEVER commit this)
cp .env.example .env
$EDITOR .env          # fill DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN

# 3. Run (paper mode by default)
volscalp --config configs/default.yaml

# 4. Open http://127.0.0.1:8765 in your browser
```

Switch to **live** mode from the dashboard (Apply → confirms) or pass `--mode live` on the CLI.

## Directory layout

```
src/volscalp/
  broker/
    dhan_client.py      # async REST (place_order, positions, funds)
    dhan_ws.py          # binary WS parser for Dhan v2 quote feed
    instruments.py      # scrip-master loader + ATM/strike lookups
  market_data/
    feed.py             # tick router + listener dispatch
    candles.py          # 1-min OHLCV aggregator (per instrument)
  strategy/
    engine.py           # cycle state machine, entry/exit, lazy legs
    mtm.py              # lock-and-trail controller (FRD §5.2)
  orders/
    manager.py          # OrderManager + PaperBackend + LiveBackend
    reconciler.py       # Dhan positions poll; filters by correlation/orderId
  storage/
    db.py               # aiosqlite wrapper + repository helpers
    schema.sql          # sessions / cycles / legs / orders / bars / decisions
  dashboard/
    app.py              # FastAPI + WebSocket push
    event_bus.py        # fan-out to dashboard subscribers
    static/             # single-page UI (vanilla JS + Chart.js)
  config.py             # Pydantic config + env secrets
  process_guard.py      # PID file, stale-process cleanup, signal handlers
  logging_setup.py      # structlog JSON + rotating file
  main.py               # RuntimeState + wiring
```

## Runtime controls (dashboard)

| Control | Notes |
|---|---|
| Mode (paper/live) | Live requires browser confirm. Swaps the order backend in-place. |
| Lots/trade | Applied to next new leg. Active legs keep their original size. |
| Kill switch | Sets `kill_switch` event → engines force-close all legs and stop starting new cycles. |

## Process hygiene

- On startup, `process_guard.setup_process_hygiene()` scans for stale `volscalp` processes (via PID file at `~/.claude-vol-trading/run/app.pid` plus a broader cmdline scan) and terminates them before starting.
- SIGINT / SIGTERM trigger `run_shutdown_callbacks()` in LIFO order: engines stop → market data stops → WS disconnects → DB session ends → DB closes → Dhan HTTP client closes.
- PID file is removed only if it contains our PID.

## Data model

See `src/volscalp/storage/schema.sql`. Every entry/exit decision is persisted to `decisions` with the inputs evaluated (bar values, momentum pct, mtm) so paper and live traces can be diffed offline.

## Dhan integration notes

- **Instrument master** is downloaded from `https://images.dhan.co/api-data/api-scrip-master-detailed.csv` and cached daily under `data/instruments/`.
- **Market feed** is the v2 binary WS at `wss://api-feed.dhan.co`. We default to the `quote` (17) subscription for LTP + OHLC + volume; `depth_20` is available via config.
- **Orders** are placed as MARKET, INTRADAY on the `/v2/orders` REST endpoint with a `correlationId` equal to our client order ID so the reconciler can filter the positions response.
- **Token expiry**: the access token typically expires every ~24 hours. Regenerate from the Dhan developer portal and update `.env`; the app will pick up the new value on restart.

## Security

- `.env` is gitignored. Never commit `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `DHAN_PIN`, or `DHAN_TOTP_SECRET`.
- Live mode requires an explicit confirm (config: `dashboard.live_mode_require_confirm`).
- Kill switch requires confirm (config: `dashboard.kill_switch_require_confirm`).
- The dashboard binds to `127.0.0.1` by default. Do not expose to the public internet without a reverse proxy + auth.

## Validation checklist

Before running the first **live** session, verify each item:

- [ ] `.env` has valid `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`; token not yet expired.
- [ ] App starts in paper mode, dashboard loads, WS connects (green pill).
- [ ] Instrument master shows 30+ strikes for NIFTY and BANKNIFTY monthly expiry.
- [ ] Spot quote arrives for both indices (dashboard `Spot: …`).
- [ ] A paper cycle opens at 09:30 with ATM ± 6 strikes resolved.
- [ ] Place a single 1-lot live test outside market volatility; verify Dhan position shows up.
- [ ] MTM reported by app matches Dhan's position MTM for our `correlationId`.
- [ ] Kill switch flattens all active legs within 1 second.
- [ ] Session close (15:15) forces exits and writes a session row with final P&L.
- [ ] Rotate Dhan credentials after testing.

## Known gaps (v0.1)

- Backtest mode is scaffolded but not yet implemented; `mode: backtest` will fail to start.
- Access-token auto-refresh via TOTP is not wired in; expiry requires a manual `.env` update.
- Reconciler compares `orderId` fields only; if Dhan starts returning `correlationId` in positions, add that key.
- No daily loss circuit breaker (per FRD answer — manual override only).
- UI is single-user (no auth) and intended for localhost.

## FRD

See [`FRD.md`](./FRD.md). Keep it in sync with code changes — PR reviewers should reject changes that update behaviour without updating the FRD.
