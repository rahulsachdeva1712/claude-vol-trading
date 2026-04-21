# FUNCTIONAL REQUIREMENTS DOCUMENT

**Repeated OTM Strangle Strategy — Trading Application**
**Version 1.0**

Indices: NIFTY 50, BANKNIFTY
Strategy Type: Intraday Repeated OTM Strangle (Long Options)

> **0.0 MOST IMPORTANT**: this document is the living source of truth.
> Whenever the application is modified, this FRD MUST be updated in the
> same change so it always reflects the actual behaviour of the app.

---

## Section 1 — Introduction

### 1.1 Purpose
This FRD defines the complete behaviour, business logic, data contracts, and
UI requirements for a trading application implementing the Repeated OTM
Strangle Strategy on NSE indices (NIFTY 50 and BANKNIFTY). It is the
authoritative specification for the development team.

### 1.2 Scope
- Paper trading mode: live market feed, simulated execution, no real capital
- Live trading mode: real order execution via broker API (Dhan)
- Strategy configuration management
- Per-cycle and per-session trade logging and P&L reporting

> **Non-goals (v0.x)**: historical backtesting, multi-strategy hosting,
> portfolio-level risk, execution algos beyond plain market orders.

### 1.3 Definitions
- **ATM**: At-the-Money — the strike closest to the current underlying spot price.
- **OTM +6 CE**: Call option at ATM + 6 strikes above spot.
- **OTM -6 PE**: Put option at ATM - 6 strikes below spot.
- **Base Leg**: The primary CE or PE entered at the start of each cycle.
- **Lazy Leg**: A secondary leg opened only after the opposite base leg is stopped out.
- **Cycle**: One complete entry-to-exit sequence from entry through MTM or SL close.
- **MTM**: Mark-to-Market — real-time unrealised P&L for all open legs in the cycle.
- **Leg SL**: Individual option leg stop-loss, calculated from leg entry price.
- **Lock & Trail**: MTM profit locking mechanism — locks a floor once a threshold is crossed, then trails upward.
- **Momentum Filter**: Minimum candle return required on the option's own bar before a leg entry is triggered.
- **Bar**: One OHLCV candle on the options data feed; minimum engine granularity.

---

## Section 2 — Strategy Overview

### 2.1 Conceptual Summary
The strategy buys OTM strangles (one CE, one PE) on a chosen index, repeating
new entry cycles throughout the trading session with zero cooldown between
cycles. It profits from large intraday directional moves on either side.
The strategy is purely long premium — it does not sell options. Edge comes
from momentum-filtered entries and disciplined per-leg and portfolio-level
risk controls.

### 2.2 Session Flow
| Step | Time        | Action                                                                 |
|------|-------------|------------------------------------------------------------------------|
| 1    | 09:15–09:29 | App initialises, loads config, establishes data feed, computes initial ATM. |
| 2    | 09:30       | Cycle 1 starts. Begin watching ATM+6 CE and ATM-6 PE for momentum entry. |
| 3    | Per bar     | Check entry condition for each unentered base leg. Enter on first qualifying bar. |
| 4    | Per bar     | For entered legs: check leg SL. Check overall MTM target / max loss / lock-trail. |
| 5    | Base leg SL | Schedule lazy leg entry for opposite side if not yet opened in this cycle. |
| 6    | Cycle exit  | Close all open legs. Record cycle P&L.                                 |
| 7    | Next bar    | Start next cycle immediately (cooldown = 0 minutes).                   |
| 8    | 15:15       | Force-close all open positions. End session. Generate session report.  |

### 2.3 Index-Specific Configuration
| Parameter              | NIFTY            | BANKNIFTY        |
|------------------------|------------------|------------------|
| Strategy Structure     | OTM Strangle     | OTM Strangle     |
| Strike Offset CE       | ATM + 6          | ATM + 6          |
| Strike Offset PE       | ATM - 6          | ATM - 6          |
| Momentum Threshold     | 1% per bar       | 1% per bar       |
| Lazy Mode              | Enabled          | Enabled          |
| MTM Risk Profile       | Protective MTM   | Tight MTM        |
| Base Leg SL            | 15% below entry  | 15% below entry  |
| Lazy Leg SL            | 12% below entry  | 12% below entry  |
| Session Close          | 15:15            | 15:15            |
| Cooldown Between Cycles| 0 minutes        | 0 minutes        |
| Expiry                 | Monthly          | Monthly          |

---

## Section 3 — Entry Logic

### 3.1 Cycle Start
- The first cycle of every session starts at **09:30** exactly.
- Subsequent cycles start on the first available bar after the prior cycle's exit (cooldown = 0).
- No new cycle may start at or after 15:15.

### 3.2 Strike Selection at Cycle Start
- At cycle start, compute current ATM from the underlying spot price.
- Target CE: ATM + 6 strikes.
- Target PE: ATM - 6 strikes.
- Strike selection is computed **once at cycle start**. The target strike does NOT float after that — the engine tracks these fixed strikes through the cycle.
- If a strike does not exist in the live chain, log a warning and skip that leg for the cycle.

### 3.3 Momentum Entry Condition
Each base leg (CE and PE) enters independently. The entry condition is checked on every new bar after cycle start.

| Parameter          | Specification                                              |
|--------------------|------------------------------------------------------------|
| Condition          | Option candle return >= momentum threshold                 |
| Candle Return      | (Close - Open) / Open * 100%                               |
| Momentum Threshold | 1.0% (configurable per index)                              |
| Entry Bar          | First bar where condition is met after cycle start         |
| Entry Price        | Close price of the triggering bar (configurable: Open of next bar) |
| Independence       | CE and PE legs enter independently; one may enter earlier than the other |

> If neither leg meets the entry condition before session close, that leg is never entered for that cycle.

---

## Section 4 — Lazy Leg Logic

### 4.1 Overview
Lazy legs are recovery legs opened on one side when the base leg on the opposite side has been stopped out. The intent is to follow the directional move that caused the base stop.

### 4.2 Trigger Conditions
| Event                    | Lazy Leg Scheduled           |
|--------------------------|------------------------------|
| Base Call (CE) SL hit    | Schedule Lazy Put (PE) entry |
| Base Put (PE) SL hit     | Schedule Lazy Call (CE) entry|

### 4.3 Entry Rules for Lazy Legs
- A lazy leg uses the same momentum entry condition as a base leg (>=1% candle return on the new strike's bar).
- The strike for the lazy leg is computed **fresh from the current ATM at the time of scheduling** — it is NOT the same strike as the stopped base leg.
- Only one lazy CE and one lazy PE may be open per cycle. Duplicate lazy entries are blocked.
- If the lazy leg's momentum condition is never met before cycle exit, the lazy leg is not entered.

### 4.4 Maximum Legs Per Cycle
| Slot | Type           | Description                                       |
|------|----------------|---------------------------------------------------|
| 1    | Base Call (CE) | Opened at cycle start if momentum met             |
| 2    | Base Put (PE)  | Opened at cycle start if momentum met             |
| 3    | Lazy Put (PE)  | Opened only if Base Call is stopped out           |
| 4    | Lazy Call (CE) | Opened only if Base Put is stopped out            |

Maximum 4 active legs at any point within a single cycle.

---

## Section 5 — Exit Logic

### 5.1 Leg-Level Stop Loss
| Leg Type  | Stop Loss Formula     | Example (Entry Rs.100)  |
|-----------|-----------------------|-------------------------|
| Base Leg  | Entry * (1 - 0.15)    | Stop at Rs.85.00        |
| Lazy Leg  | Entry * (1 - 0.12)    | Stop at Rs.88.00        |

- Stop price is fixed at entry and does not change during the leg's life.
- The stop is evaluated on each bar's close price (configurable: low price for intrabar simulation).
- When a leg is stopped, it is exited at the stop price (slippage configurable; default none).

### 5.2 Strategy-Level MTM Controls
MTM controls operate at the **cycle level** — they monitor the combined P&L of all open legs within the current cycle.

**NIFTY — Protective MTM**

| Control         | Value    | Behaviour                                            |
|-----------------|----------|------------------------------------------------------|
| Max Loss        | Rs.2500  | Exit all legs when cycle MTM <= -Rs.2500             |
| Target          | Rs.300   | Exit all legs when cycle MTM >= Rs.300               |
| Lock Activation | Rs.400   | Once MTM reaches Rs.400, lock floor at Rs.250        |
| Lock Floor      | Rs.250   | Cycle exits with minimum Rs.250 once locked          |
| Trail Step      | Rs.1     | Floor rises in Rs.1 steps as MTM improves            |

**BANKNIFTY — Tight MTM**

| Control         | Value    | Behaviour                                            |
|-----------------|----------|------------------------------------------------------|
| Max Loss        | Rs.3500  | Exit all legs when cycle MTM <= -Rs.3500             |
| Target          | Rs.300   | Exit all legs when cycle MTM >= Rs.300               |
| Lock Activation | Rs.500   | Once MTM reaches Rs.500, lock floor at Rs.350        |
| Lock Floor      | Rs.350   | Cycle exits with minimum Rs.350 once locked          |
| Trail Step      | Rs.1     | Floor rises in Rs.1 steps as MTM improves            |

### 5.3 Session Close
- At **15:15**, all open legs across all active cycles are forcibly closed at prevailing market price.
- No new cycle may be initiated after 15:15.
- Partial cycles (legs entered but no exit triggered) are closed at session end.

### 5.4 Exit Priority Order
When multiple exit conditions could apply simultaneously, priority is:
1. Session close at 15:15 (always overrides all others)
2. Overall MTM max loss breached
3. Lock & Trail floor breached (exit at floor)
4. Overall MTM target reached
5. Individual leg SL hit (exits only that leg, cycle continues)

---

## Section 6 — Repeat Cycle Mechanics

### 6.1 Repeat Wrapper Behaviour
- After a cycle closes (any exit trigger), the engine immediately schedules the next cycle.
- Cooldown = 0 minutes. Next cycle starts on the first available bar after the exit bar.
- All cycle state (MTM accumulator, leg positions, lock/trail flags) is fully reset between cycles.
- Session-level state (total session P&L, cycle counter) accumulates across all cycles.

### 6.2 Cycle State Machine
| State     | Entry Condition          | Transitions                                          |
|-----------|--------------------------|------------------------------------------------------|
| IDLE      | App start / post-session | -> WATCHING on 09:30                                 |
| WATCHING  | Cycle start              | -> ACTIVE when any base leg enters. -> IDLE at 15:15 |
| ACTIVE    | First leg entered        | -> EXITING on any MTM exit trigger. -> IDLE at 15:15 |
| EXITING   | Exit trigger fired       | -> WATCHING when all legs closed                     |
| CLOSED    | 15:15 or end of session  | Terminal state for the day                           |

### 6.3 Reset Between Cycles
Fields reset at each new cycle start:
- Cycle MTM accumulator -> 0
- All 4 leg slot statuses -> EMPTY
- Lock activated flag -> false
- Lock floor value -> 0
- Lazy leg scheduled flags -> false
- Cycle entry timestamps -> null

---

## Section 7 — Data Requirements

### 7.1 Market Data Feed
| Field        | Specification                                                                 |
|--------------|-------------------------------------------------------------------------------|
| Granularity  | 1-minute OHLCV candles built from Dhan tick feed; tick feed used for intrabar SL. |
| Underlying   | NIFTY 50 spot (or futures) and BANKNIFTY spot (or futures).                   |
| Options Chain| Full chain for current and next expiry. Strikes at minimum ATM ± 15.          |
| OHLCV Fields | Open, High, Low, Close, Volume, OI per strike per bar.                        |
| Latency      | <= 500 ms bar delivery in paper/live mode.                                    |

### 7.2 Lookups Required
- **relative_lookup**: Given current ATM and offset (±N), return the option instrument token and last traded price.
- **fixed_lookup**: Given a specific instrument token (entered leg), return the current OHLCV bar.
- **day_timestamps**: Ordered list of all bar timestamps for the session — used for cycle clock and repeat scheduling.

### 7.3 Persistence
| Entity        | Fields to Persist                                                                  |
|---------------|------------------------------------------------------------------------------------|
| Session       | Date, Index, Mode, Total Cycles, Total P&L, Start Time, End Time                   |
| Cycle         | Cycle #, Start Bar, Exit Bar, Exit Reason, Cycle P&L, Peak MTM, Trough MTM         |
| Leg           | Slot, Type (Base/Lazy), CE/PE, Strike, Entry Bar, Entry Price, Exit Bar, Exit Price, Exit Reason, Leg P&L |
| Bar Snapshot  | Timestamp, Underlying Spot, ATM, all open leg prices per bar (for replay and audit)|

---

## Section 8 — Application Modules

| ID   | Module               | Responsibility                                                               |
|------|----------------------|------------------------------------------------------------------------------|
| M-01 | Config Manager       | Load, validate, and expose all strategy and risk parameters.                 |
| M-02 | Market Data Service  | Connect to broker/exchange feed; normalise and cache OHLCV bars.             |
| M-03 | ATM Calculator       | Compute ATM and strike offsets from spot price on each bar.                  |
| M-04 | Entry Engine         | Apply momentum filter; schedule and execute base and lazy leg entries.       |
| M-05 | Position Manager     | Track all open legs, compute leg-level MTM, enforce leg SLs.                 |
| M-06 | MTM Controller       | Evaluate cycle-level MTM; trigger exits on target/loss/lock-trail.           |
| M-07 | Cycle Manager        | Manage cycle state machine, reset between cycles, schedule next cycle.       |
| M-08 | Order Manager        | Route buy/sell orders to broker API (live/paper); handle fills.              |
| M-09 | Trade Logger         | Persist all session, cycle, leg, and bar snapshot data.                      |
| M-10 | Report Engine        | Generate end-of-session and on-demand P&L reports and charts.                |
| M-11 | UI / Dashboard       | Real-time display of open positions, cycle MTM, session summary.             |

### 8.2 Operational Modes
- **Paper**: Connect to live feed; simulate entries and exits without real orders. Mirror of live logic.
- **Live**: Full live execution via Dhan broker API. All MTM, SL, and entry logic identical to paper mode.

> Backtesting on historical data is explicitly out of scope (see §1.2).

---

## Section 9 — Configuration Parameters

### 9.1 Global Parameters
| Parameter              | Default | Description                                   |
|------------------------|---------|-----------------------------------------------|
| session_start          | 09:30   | First cycle start time                        |
| session_end            | 15:15   | Hard session close time                       |
| cooldown_minutes       | 0       | Wait time between cycles (minutes)            |
| base_leg_sl_pct        | 15%     | Base leg stop loss as % below entry           |
| lazy_leg_sl_pct        | 12%     | Lazy leg stop loss as % below entry           |
| momentum_threshold_pct | 1.0%    | Minimum option candle return for entry        |
| strike_offset_ce       | +6      | CE strike offset from ATM                     |
| strike_offset_pe       | -6      | PE strike offset from ATM                     |
| lazy_enabled           | true    | Whether lazy leg feature is active            |
| lots_per_trade         | 1       | Lots per leg (configurable from UI)           |

### 9.2 MTM Profile Parameters
| Parameter        | Protective (NIFTY) | Tight (BANKNIFTY) | Description            |
|------------------|--------------------|-------------------|------------------------|
| mtm_max_loss     | Rs.2500            | Rs.3500           | Max cycle loss                                   |
| mtm_target       | Rs.400             | Rs.500            | Cycle profit target                              |
| lock_activation  | Rs.300             | Rs.300            | Lock trigger level (must be < target)            |
| lock_floor       | Rs.250             | Rs.200            | Min profit after lock (must be < lock_activation)|
| trail_step       | Rs.1               | Rs.1              | Trail increment/bar                              |

**Invariant**: `lock_floor < lock_activation < target`. The lock-and-trail arms at
`lock_activation` (on the way up) so that if MTM retreats to `lock_floor` the
cycle exits with guaranteed profit — this must fire *before* the absolute
`target` take-profit, otherwise the trail mechanism is inert.

---

## Section 10 — UI / Dashboard Requirements

### 10.1 Real-Time Dashboard
- Current cycle number and state (WATCHING / ACTIVE / EXITING) — per index.
- Live MTM for current cycle — colour-coded (green positive, red negative).
- Lock & trail status: locked / not locked, current floor value.
- Leg table: all 4 slots showing status (EMPTY / WATCHING / ACTIVE / STOPPED), strike, entry price, current price, P&L per leg.
- Session summary: total cycles completed, session P&L, win/loss counts.
- Underlying spot price and current ATM — updated on every tick.
- KPI strip (required, visible at all times):
  - Today MTM P&L
  - Today Realized P&L
  - Today Unrealized P&L
  - Cumulative P&L (across sessions)
  - Open Positions count
  - Closed Trades count
  - Win Rate
  - Average P&L per Closed Trade
  - Profit Factor
  - Max Drawdown
- Controls (on the dashboard):
  - **Mode toggle**: Paper / Live (Live requires explicit confirmation)
  - **Kill switch**: one click halts all activity, force-closes all live positions, disables entries
  - **Lots per trade** input
  - **Start / Stop engine** buttons

### 10.2 Trade Log View
- Filterable table of all completed cycles for the session
- Columns: Cycle #, Start Time, Exit Time, Exit Reason, Legs Entered, Peak MTM, Cycle P&L
- Expandable row to show individual leg detail

### 10.3 Configuration Panel
- All parameters from Section 9 editable before session start
- Index selector: NIFTY / BANKNIFTY / Both
- Mode selector: Paper / Live
- MTM profile selector: Protective / Tight / Custom
- Save and load named configuration profiles

### 10.4 Report Screen
- End-of-session P&L curve (cycle-by-cycle cumulative)
- Cycle duration distribution histogram
- Win rate, average win, average loss, profit factor
- Largest drawdown per session
- Export to CSV and PDF

---

## Section 11 — Non-Functional Requirements

### 11.1 Performance
- Bar processing latency: <= 100 ms from bar receipt to all decisions evaluated.
- Order placement latency (live mode): <= 200 ms from decision to API call.
- Internal tick → decision latency: sub-millisecond on VPS.
- UI refresh: realtime via WebSocket push (not polling).

### 11.2 Reliability
- Engine must handle missed bars gracefully — no crash, log the gap, continue.
- Broker API disconnection: pause order placement, alert UI, retry with exponential backoff.
- All persisted trade data must be written before acknowledging bar completion.
- Startup must clean up stale processes from prior runs (PID file + psutil scan).
- Graceful shutdown on SIGINT / SIGTERM — closes WS, flushes DB, removes PID file.

### 11.3 Configurability
- All numeric thresholds (SL %, MTM levels, momentum %, strike offsets) must be configurable without code changes (YAML + UI).
- MTM profiles must be addable as named configurations.

### 11.4 Auditability
- Every entry and exit decision logs: timestamp, bar values used, condition evaluated, outcome.
- Paper and live modes must produce identical logic traces (same inputs → same decisions) — divergence is a bug.

### 11.5 Security (Live Mode)
- Dhan credentials never stored in plain text in the repo. Loaded from `.env` (gitignored) or OS keyring.
- Live mode requires explicit user confirmation before session start.
- Daily loss circuit breaker: configurable; halts all activity if session P&L crosses threshold.
- Only orders placed by this app are tracked by MTM; any manual Dhan trades outside the app are filtered out by order-id tagging.

---

## Section 12 — Open Questions & Assumptions (Resolved)

### 12.1 Assumptions
- Entry price is the Close of the triggering bar. If next-bar-open fills are required, this must be explicitly configured.
- Stop loss is evaluated on bar Close, not intrabar Low (conservative). Intrabar tick SL is a configurable enhancement.
- Lot size and quantity are configurable outside this document and not part of core strategy logic.
- ATM is the nearest listed strike to spot. If spot falls exactly between two strikes, the lower strike is selected (configurable).

### 12.2 Answers to Open Questions
1. **Daily maximum loss across all cycles?** — Manual override only. No hard daily stop by default.
2. **Adaptive strike offset?** — No. Remain hard-coded at ±6.
3. **Expiry?** — Monthly.
4. **Slippage in paper?** — None. Market orders, LTP fills.
5. **Brokerage / STT in P&L?** — No. Match with broker's own numbers.
6. **Lazy leg target strike missing in chain?** — Order fails at broker; status reflected in the app UI.
7. **Trail step cadence?** — As defined in the strategy (Rs.1 per improvement point).

### 12.3 Platform Decisions
- **Broker**: Dhan (REST for orders + WebSocket for market data).
- **Tech stack**: Python 3.11, uvloop, asyncio, FastAPI, SQLite, dhanhq SDK.
- **Deployment**: VPS. Dashboard bound to `127.0.0.1` by default.
- **Paper trading fills**: LTP (fallback to best bid/ask when available from 20-depth).
- **Market data feed**: Dhan ticker / quote WebSocket. Upgradable to 20-depth if needed.
- **Order type**: MARKET, intraday (MIS) only.
- **Concurrency**: Max 2 active cycles (1 NIFTY + 1 BANKNIFTY). Each cycle can have up to 4 legs.
- **Auto square-off**: 15:15 hard close.
- **Process hygiene**: PID file + psutil check on startup; SIGINT/SIGTERM handlers for clean shutdown.

---

**END OF DOCUMENT**
