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

> **Non-goals (v0.x)**: multi-strategy hosting, portfolio-level risk,
> execution algos beyond plain market orders. Historical backtesting is
> **not part of the application runtime** — the live process does not
> backtest, there is no backtest UI, and no `scripts/backtest_*.py` code
> is imported by `src/volscalp/*`. Standalone developer-only research
> scripts under `scripts/` (`rollingoption_probe.py`,
> `backtest_history_fetch.py`, `backtest_2y.py`) use Dhan's
> `/v2/charts/rollingoption` endpoint (Expired Options Data add-on) to
> replay the strategy offline over multi-year windows. These scripts
> share strategy constants with the engine by duplication, not import;
> any change to entry/exit/MTM rules must be mirrored in both places.

### 1.3 Definitions
- **ATM**: At-the-Money — the strike closest to the current underlying spot price.
- **OTM +6 CE**: Call option at ATM + 6 strikes above spot.
- **OTM -6 PE**: Put option at ATM - 6 strikes below spot.
- **Base Leg**: The primary CE or PE entered at the start of each cycle.
- **Lazy Leg**: A secondary leg opened only after the opposite base leg is stopped out.
- **Cycle**: One complete entry-to-exit sequence from entry through MTM or SL close.
- **MTM**: Mark-to-Market — real-time unrealised P&L for all open legs in the cycle.
- **Leg SL**: Individual option leg stop-loss, calculated from leg entry price.
- **Lock & Trail**: *(removed 2026-04)* Evaluated as a protective profit-floor ratchet on cycle MTM; did not improve P&L vs the plain `max_loss`/`target` pair in the 2y backtest and was stripped from all surfaces. Kept here for glossary completeness only.
- **Momentum Filter**: Minimum candle return required on the option's own bar before a leg entry is triggered.
- **Bar**: One OHLCV candle on the options data feed; minimum engine granularity.
- **Fill-ack bridge**: The reconciler loop that polls Dhan `/positions`
  and promotes PENDING legs to ACTIVE once the broker confirms the fill.
  Required because Dhan's REST `/orders` response returns `PENDING` with
  `tradedQuantity=0` even on successful market orders — the actual fill
  lands a few hundred milliseconds later in the position book. See §5.1.
- **EXTERNAL_CLOSE**: Exit reason stamped on an ACTIVE leg when its
  broker position disappears without an app-initiated exit (manual
  square-off in the Dhan portal, broker-initiated stop, etc.). Detected
  by the reconciler. See §5.4.
- **Open-cycle adoption**: On process start, the engine re-hydrates any
  cycle the previous run left open (status PENDING/ACTIVE on disk) so
  positions are managed across restarts rather than orphaned. The
  reconciler then confirms each leg against the broker. See §8.2.

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
| 4    | Per bar     | For entered legs: check leg SL. Check cycle-level MTM `max_loss` (bar-close). |
| 4b   | Per tick    | Check cycle-level MTM `target` intrabar (1 Hz) against current LTP. See §5.2. |
| 5    | Base leg SL | Immediately enter opposite-side lazy leg (no momentum gate) if not yet opened in this cycle. |
| 6    | Cycle exit  | Close all open legs. Record cycle P&L.                                 |
| 7    | Next bar    | Start next cycle immediately (cooldown = 0 minutes).                   |
| 8    | 15:15       | Force-close all open positions. End session. Generate session report.  |

### 2.3 Index-Specific Configuration
| Parameter              | NIFTY            | BANKNIFTY        |
|------------------------|------------------|------------------|
| Strategy Structure     | OTM Strangle     | OTM Strangle     |
| Strike Offset CE       | ATM + 6          | ATM + 6          |
| Strike Offset PE       | ATM - 6          | ATM - 6          |
| Strike Interval        | 50               | 100              |
| Lot Size (NSE)         | 65               | 30               |
| Momentum Threshold     | 1% per bar       | 1% per bar       |
| Lazy Mode              | Enabled          | Enabled          |
| MTM Risk Profile       | max_loss=Rs.2500, target=Rs.300 | max_loss=Rs.2500, target=Rs.300 |
| Base Leg SL            | 15% below entry  | 15% below entry  |
| Lazy Leg SL            | 12% below entry  | 12% below entry  |
| Session Close          | 15:15            | 15:15            |
| Cooldown Between Cycles| 0 minutes        | 0 minutes        |
| Expiry                 | Monthly          | Monthly          |

> **Lot size source of truth:** `configs/default.yaml` → `instruments.<IDX>.lot_size`.
> Developer-only backtest scripts (`scripts/backtest_2y.py`,
> `scripts/backtest.py`) duplicate these constants in their own
> `INDEX_SPECS` table and **must be kept in sync** whenever the exchange
> revises lot sizes. The same rule applies to strike offsets — the live
> engine uses `engine.strike_offset_ce/pe = ±6`, and the backtest fetcher
> (`scripts/backtest_history_fetch.py --fan`) defaults to `6` to match.

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
- A lazy leg enters **immediately at the bar close of the minute its
  opposite base leg was stopped** — there is **no momentum gate** on the
  lazy side (aggregate-MTM semantics: the cycle is already "in recovery"
  and we want symmetric coverage at once, not a second filter).
- Entry price is that stop-minute bar's close on the fresh lazy
  instrument (fallback: last known LTP if no fresh bar has formed).
- The strike for the lazy leg is computed **fresh from the current ATM at the time of scheduling** — it is NOT the same strike as the stopped base leg.
- Only one lazy CE and one lazy PE may be open per cycle. Duplicate lazy entries are blocked.
- If the lazy instrument cannot be resolved (out of the subscribed strike
  fan, or no price available), the slot is marked EMPTY and no entry is
  attempted — the cycle continues on whatever legs remain.

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
- The stop is evaluated **intrabar on the bar's low by default** (`engine.sl_price_source: low` in `configs/default.yaml`). This matches Codex's `simulate_strategy_day` trigger and the 2y backtest's intrabar check, so backtest, paper, and live all fire stops on the same bar. Configurable to `close` for a more conservative "end-of-bar" simulation.
- When a leg is stopped, it is exited at the stop price (slippage configurable; default none).
- **Live fill-ack is asynchronous.** Dhan's REST `POST /orders` returns
  `orderStatus="PENDING"` with `tradedQuantity=0` even on a successful
  market order — the actual fill lands in the position book a few
  hundred milliseconds later. The engine therefore treats the REST
  response as an order acknowledgment, not a fill:
  - Leg transitions EMPTY → PENDING the moment `place_order` returns.
    The provisional `entry_price` / `sl_price` are taken from the
    triggering bar so the SL gate still has something to compare
    against on the next bar close.
  - A separate reconciler loop (§8.2) polls `GET /positions` at 1 Hz.
    When a row appears for the leg's `securityId` with `buyAvg > 0`
    and `netQty >= leg.quantity`, the engine's `on_fill_ack()` hook
    promotes the leg to ACTIVE and **re-computes the SL from the
    actual fill price** (so a slippage gap on entry doesn't cause a
    false-positive SL on the next bar).
  - Paper fills stay synchronous — `PaperBackend` fills immediately at
    LTP inside the REST call, so the leg goes EMPTY → ACTIVE with no
    PENDING intermediate and no reconciler involvement.
- **Per-strike tape after entry.** Once a leg is ACTIVE, it follows the
  entry-captured absolute strike — not the rolling "ATM±N" label (which
  would re-point at a different strike as spot drifts). The strike's
  minute-by-minute bars are only loaded while the strike sat within the
  ±6 strike fan (matching the live engine's subscription window and
  Codex's `MAX_STRIKE_SHIFT=6`). If the strike drifts beyond ±6, its bar
  is absent for the gap; MTM / exit fills then use the most recent
  available close (`_get_row_at_or_before` semantic). This keeps
  backtest and live seeing an identical tape: **neither can trigger a
  leg SL on a minute the live engine isn't subscribed to.**

### 5.2 Strategy-Level MTM Controls
MTM controls operate at the **cycle level** — they monitor the combined P&L
of **all legs in the current cycle** (realised P&L of stopped legs +
unrealised P&L of open legs). A single aggregate breach closes the
whole cycle at once.

The cycle has two thresholds evaluated on different cadences:

- **`max_loss`** (hard stop) — evaluated on **bar close** only. The
  end-of-bar evaluation absorbs transient intrabar dips, so a spike
  that would have force-closed at the bottom of a minute but
  recovered by the bar close does not blow up the cycle.
- **`target`** (take-profit) — evaluated **intrabar**, on every engine
  tick (1 Hz). The live/paper engine uses each leg's current LTP
  (refreshed by the WS feed on every tick); the backtest approximates
  simultaneous intrabar prices with each ACTIVE leg's bar high. As
  soon as aggregate MTM crosses `target`, all ACTIVE legs exit at the
  trigger-moment price. Backtest April 2026 (15 sessions) showed +55 %
  cycle count and +55 % P&L versus the same target evaluated on bar
  close, with MAX_LOSS cadence unchanged.

**NIFTY**

| Control    | Value    | Behaviour                                      |
|------------|----------|------------------------------------------------|
| Max Loss   | Rs.2500  | Exit all legs when cycle MTM <= -Rs.2500       |
| Target     | Rs.300   | Exit all legs when cycle MTM >=  Rs.300        |

**BANKNIFTY**

| Control    | Value    | Behaviour                                      |
|------------|----------|------------------------------------------------|
| Max Loss   | Rs.2500  | Exit all legs when cycle MTM <= -Rs.2500       |
| Target     | Rs.300   | Exit all legs when cycle MTM >=  Rs.300        |

**Lock-and-trail was evaluated and removed (2026-04).** A ratcheting
profit-floor was previously wired in mirroring Codex's "Tight MTM"
profile (`spread_lab.py`), but it did not improve P&L vs the plain
`max_loss`/`target` pair in the 2y backtest. The four knobs
(`lock_start`, `lock_profit`, `trail_step`, `trail_lock_step`) and the
`ExitReason.LOCK_TRAIL` enum value have been removed from configs,
models, the paper/live engine, and both backtest scripts.

**Aggregate-MTM semantics (important):**

- The threshold is evaluated **only while at least one leg is ACTIVE** —
  once every leg has closed, there is nothing left to mark to market and
  the cycle's P&L is fully realised. At that point the cycle is
  considered **done** (see §6.1) regardless of whether a threshold was
  hit, and its exit reason is inherited from the most recently stopped
  leg (typically `LEG_SL`).
- When the aggregate target / max-loss does fire, **all currently-open
  legs are force-closed on that bar**, and their individual exit reason
  is recorded as the cycle's MTM reason (not `LEG_SL`).
- A leg that already stopped via `LEG_SL` earlier in the cycle keeps its
  own `LEG_SL` exit reason — the cycle-level reason only applies to legs
  that were still open at the force-close minute.

### 5.3 Session Close
- At **15:15**, all open legs across all active cycles are forcibly closed at prevailing market price.
- No new cycle may be initiated after 15:15.
- Partial cycles (legs entered but no exit triggered) are closed at session end.

### 5.4 Exit Priority Order
When multiple exit conditions could apply simultaneously, priority is:
1. Kill switch (live only — disarms + flattens all live cycles)
2. Session close at 15:15 (always overrides 3-5)
3. Overall MTM max loss breached (`MTM_MAX_LOSS`) — evaluated on bar close
4. Overall MTM target reached (`MTM_TARGET`) — evaluated intrabar (1 Hz tick)
5. Individual leg SL hit (`LEG_SL` — exits only that leg, cycle continues)
6. External close (`EXTERNAL_CLOSE`) — exits only the observed leg. The
   reconciler stamps this when an ACTIVE leg's position disappears from
   Dhan without the app having placed a sell (e.g. the user manually
   squared off in the portal, or the broker stopped the position). The
   engine treats this as a terminal leg exit and lets the cycle's
   cycle-done detector (§6.1) decide whether the cycle itself is over.
   `EXTERNAL_CLOSE` is out-of-band — it is *observed*, not *triggered*,
   so it doesn't compete with 3–5 above; it simply records whatever
   price the broker filled at (`sellAvg` from `/positions`, falling back
   to last LTP when Dhan hasn't refreshed the row yet).

Priority here is evaluation-cadence-aware: an intrabar target hit at
09:40:37 fires immediately, even though MAX_LOSS for the same minute
wouldn't have been evaluated until the 09:41 bar close. This is
intentional — capturing take-profits the moment they're available is
strictly additive, whereas MAX_LOSS is a hard stop whose job is to
tolerate normal intra-minute volatility (see §5.2).

---

## Section 6 — Repeat Cycle Mechanics

### 6.1 Repeat Wrapper Behaviour
- After a cycle closes (any exit trigger), the engine immediately schedules the next cycle.
- Cooldown = 0 minutes. Next cycle starts on the first available bar after the exit bar.
- All cycle state (MTM accumulator, leg positions, lazy flags) is fully reset between cycles.
- Session-level state (total session P&L, cycle counter) accumulates across all cycles.

**Cycle-done detection (aggregate-MTM corollary):**

A cycle does not have to end via an MTM threshold to be considered done.
A cycle is **done** (and the next cycle starts on the following bar) as
soon as one of these holds:

1. Aggregate MTM trigger fired — `MTM_TARGET` or `MTM_MAX_LOSS` closed
   all open legs.
2. Session close (15:15) — force-closes everything.
3. **No leg is alive** — every leg slot is either `STOPPED` (exited on
   its own `LEG_SL`) or `EMPTY` (strike missing / order rejected), and
   there is no leg in `WATCHING`, `PENDING`, `ACTIVE`, or `EXITING`.
   When this happens the cycle's exit reason is inherited from the
   most recently stopped leg.

Without case 3, a cycle whose both base legs SL out — and no lazy
is eligible (or the lazy is still watching for momentum and never fires) —
would idle to session close and block every subsequent cycle for the
day. The detector is evaluated on every engine tick.

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
| M-04 | Entry Engine         | Apply momentum filter to base legs; immediately enter opposite lazy leg when a base leg SLs. |
| M-05 | Position Manager     | Track all open legs, compute leg-level MTM, enforce leg SLs.                 |
| M-06 | MTM Controller       | Evaluate cycle-level MTM; trigger exits on max_loss or target.               |
| M-07 | Cycle Manager        | Manage cycle state machine, reset between cycles, schedule next cycle.       |
| M-08 | Order Manager        | Route buy/sell orders to broker API (live/paper); handle fills.              |
| M-09 | Trade Logger         | Persist all session, cycle, leg, and bar snapshot data.                      |
| M-10 | Report Engine        | Generate end-of-session and on-demand P&L reports and charts.                |
| M-11 | UI / Dashboard       | Real-time display of open positions, cycle MTM, session summary.             |

### 8.2 Operational Modes — Paper and Live run simultaneously
Paper and live are **not** a toggle. Both run in the same process, on the
same market feed, as independent engine trees:

- **Paper**: Uses the live feed; fills are simulated at LTP. Always active;
  no credentials needed beyond the Dhan market-data token. Paper signals
  are evaluated every bar and paper cycles proceed regardless of live state.
- **Live**: Uses the live feed; orders go to Dhan REST. Live engines
  **start disarmed** — entries are suppressed until the user explicitly
  clicks "Arm" on the live tab. An armed live engine behaves identically
  to the paper engine except for order placement.

**Engine count (per process):** one engine per `(mode, underlying)` pair.
With the default config this is four engines:

| id                  | mode  | underlying | OrderManager | Session  | Arm default | Kill switch   |
|---------------------|-------|------------|--------------|----------|-------------|---------------|
| paper-NIFTY         | paper | NIFTY      | Paper        | session A (paper) | always armed | none          |
| paper-BANKNIFTY     | paper | BANKNIFTY  | Paper        | session A (paper) | always armed | none          |
| live-NIFTY          | live  | NIFTY      | Live (Dhan)  | session B (live)  | disarmed     | live-only     |
| live-BANKNIFTY      | live  | BANKNIFTY  | Live (Dhan)  | session B (live)  | disarmed     | live-only     |

Shared: Dhan market feed, `MarketDataService`, instrument master, SQLite
connection, event bus, dashboard.
Per-engine: `Cycle`, cycle counter, realized/peak/trough P&L, session id,
arm flag reference.
Per-mode (tree): `OrderManager`, lot-size input, kill flag (live only),
DB session row.

Because fills can differ between paper (LTP) and live (Dhan execution),
the two trees' cycles and P&L naturally diverge over time. This is
intended — paper is an idealised twin, not a strict mirror.

**Reconciler — live fill-ack bridge.** A single `PositionReconciler`
task polls Dhan `GET /positions` at 1 Hz (configurable via
`broker.reconcile_interval_s`) and bridges the broker's async fill
model into the engines' leg state machines:

- **PENDING → ACTIVE promotion.** A live `place_order` returns
  `orderStatus="PENDING"` with `tradedQuantity=0` (see §5.1); the leg
  sits in PENDING until the reconciler sees a position row for the
  leg's `securityId` with `buyAvg > 0` and `netQty > 0`, then calls
  `engine.on_fill_ack(security_id, buyAvg, filled_qty)`. The engine
  promotes the leg to ACTIVE, recomputes SL from the real fill price,
  and persists the new entry row to SQLite.
- **EXTERNAL_CLOSE detection.** An ACTIVE leg whose `securityId` is
  absent from the next poll (or whose row shows `netQty==0` with
  `sellQty>0`) is treated as closed out-of-band — the reconciler calls
  `engine.on_external_close(security_id, sellAvg)` and the leg is
  stamped STOPPED with reason `EXTERNAL_CLOSE` (§5.4). The cycle-done
  detector (§6.1) then decides whether the cycle itself is over.
- Paper engines are invisible to the reconciler — `PaperBackend`
  returns FILLED synchronously, so paper legs never sit in PENDING.

**Restart-safe open-cycle adoption.** On startup, after the KPI
backfill (`_backfill_counters_from_db`), each live engine runs
`_restore_open_cycles_from_db`:

- Queries `cycles` + `legs` for today's session-date, this engine's
  `(mode, underlying)`, with `ended_at IS NULL`.
- Rebuilds in-memory `Cycle` + `Leg` objects, carrying each leg's
  persisted `status`, `security_id`, `entry_price`, `sl_price`, and
  `row_id`. Terminal legs (STOPPED / EMPTY) are skipped — they already
  closed on disk.
- Any "ghost" open cycles (more than one open for the same
  (mode, underlying, date) — shouldn't happen) are force-closed with
  `SESSION_CLOSE` so the engine doesn't accumulate orphans.
- The reconciler then reconciles each restored leg against Dhan as
  usual: PENDING legs get promoted once the fill acks land; ACTIVE
  legs get EXTERNAL_CLOSE if the position is gone.

This adoption path is what makes the fill-ack bridge robust across
process restarts: a crash mid-cycle doesn't orphan positions, and the
user doesn't need to re-arm to continue managing them (the armed
check only gates *new* cycle starts — see `_tick_engine`).

> Backtesting on historical data is explicitly out of scope (see §1.2).

---

## Section 9 — Configuration Parameters

### 9.1 Global Parameters
| Parameter              | Default | Description                                           |
|------------------------|---------|-------------------------------------------------------|
| session_start          | 09:30   | First cycle start time                                |
| session_end            | 15:15   | Hard session close time                               |
| cooldown_minutes       | 0       | Wait time between cycles (minutes)                    |
| base_leg_sl_pct        | 15%     | Base leg stop loss as % below entry                   |
| lazy_leg_sl_pct        | 12%     | Lazy leg stop loss as % below entry                   |
| momentum_threshold_pct | 1.0%    | Minimum option candle return for entry                |
| strike_offset_ce       | +6      | CE strike offset from ATM                             |
| strike_offset_pe       | -6      | PE strike offset from ATM                             |
| lazy_enabled           | true    | Whether lazy leg feature is active                    |
| lots_per_trade_paper   | 1       | Lots per leg — paper engines (UI-editable per mode)   |
| lots_per_trade_live    | 1       | Lots per leg — live engines (UI-editable per mode)    |

### 9.2 MTM Profile Parameters
| Parameter        | NIFTY    | BANKNIFTY | Description                                                |
|------------------|----------|-----------|------------------------------------------------------------|
| max_loss         | Rs.2500  | Rs.2500   | Max cycle loss — force-close at bar close when cycle MTM <= -max_loss |
| target           | Rs.300   | Rs.300    | Cycle profit target — force-close intrabar (1 Hz tick) when cycle MTM >= target |

Lock-and-trail fields (`lock_start`, `lock_profit`, `trail_step`,
`trail_lock_step`) and the `ExitReason.LOCK_TRAIL` enum value were
removed in 2026-04 after backtesting showed no P&L improvement over the
plain `max_loss`/`target` pair.

---

## Section 10 — UI / Dashboard Requirements

### 10.1 Real-Time Dashboard
The dashboard is split into two **tabs**: `Paper` and `Live`. Each tab
shows its own engine tree — nothing bleeds across tabs.

Both tabs display:
- Current cycle number and state (WATCHING / ACTIVE / EXITING) — per index.
- Live MTM for current cycle — colour-coded (green positive, red negative).
- Leg table: all 4 slots showing status (EMPTY / WATCHING / ACTIVE / STOPPED), strike, entry price, current price, P&L per leg.
- Underlying spot price and current ATM — updated on every tick.
- KPI strip (required, visible at all times) — mode-scoped:
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
- Closed-trades table — scoped to the selected mode, **today only** (rolls
  up all of today's cycles across any process restarts on the same date).
- **Two P&L curves** per tab, both server-scoped by mode:
  - **Today's P&L curve** — cumulative cycle-by-cycle realised P&L for
    the current session date. X-axis is cycle number, resets daily.
    Empty-state placeholder shown when no cycle has closed yet today.
    Source: `GET /api/closed_trades?mode=` (same data as the closed-trades
    table).
  - **Cumulative P&L across days** — one point per trading day,
    running sum of closed-cycle P&L across every session ever
    recorded. X-axis is `session_date`. The rightmost y-value equals
    the KPI strip's "Cumulative P&L" figure — the KPI is just this
    series' tail. Empty-state placeholder shown when no cycle has
    ever closed for this mode. Source: `GET /api/equity_curve?mode=`.

Per-tab controls:
- `Paper` tab:
  - **Lots per trade** input (paper-only)
- `Live` tab:
  - **Arm / Disarm** toggle: live engines do not place entries until armed.
  - **Kill switch**: disarms + force-closes every live cycle. Does not affect paper.
  - **Clear kill**: resets the kill flag (manual review gate). Does not re-arm.
  - **Lots per trade** input (live-only).

The live tab's tab-chip pill renders one of `disarmed`, `armed`, `killed`,
or `unavailable` (when Dhan credentials are absent).

### 10.2 Trade Log View
- Per-mode filterable table of all completed cycles for the session
- Columns: Cycle #, Start Time, Exit Time, Exit Reason, Legs Entered, Peak MTM, Cycle P&L
- Expandable row to show individual leg detail

### 10.3 Configuration Panel
- All parameters from Section 9 editable before session start (config file)
- Runtime mutable from the dashboard: **lots_per_trade_paper**, **lots_per_trade_live** (per tab).
- Mode is not a selector — both paper and live always run (live gated by Arm).
- Index selector (config file only): NIFTY / BANKNIFTY / Both.

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
- Paper and live decision paths are identical per engine class — the only
  difference is which `OrderManager` backend receives the order. Signal
  *decisions* therefore agree tick-for-tick; outcomes (fills, P&L, SL
  times) diverge naturally based on execution reality.
- Each mode has its own session row in SQLite (`sessions.mode = 'paper'|'live'`)
  for clean audit isolation.

### 11.5 Security (Live Mode)
- Dhan credentials never stored in plain text in the repo. Loaded from `.env` (gitignored) or OS keyring.
- Live mode requires explicit user confirmation via the **Arm** button
  before any live entry is placed. Disarm / Kill are immediately effective.
- Daily loss circuit breaker: not implemented by default (per §12.2 Q1);
  the operator is expected to watch the dashboard and use Kill manually.
- Only orders placed by this app are tracked by MTM. The reconciler
  matches broker positions to in-flight legs by `securityId` within the
  app's own `current_cycle.legs` — it never materialises a leg from a
  position it didn't itself request. A manual Dhan trade outside the app
  stays invisible to the engine (no PENDING leg to promote → no MTM
  contribution). The inverse — an ACTIVE leg that the user squares off
  manually from the portal — is *observed* and stamped `EXTERNAL_CLOSE`
  (§5.4), so the engine doesn't try to exit a position that is already
  flat at the broker.

---

## Section 12 — Open Questions & Assumptions (Resolved)

### 12.1 Assumptions
- Entry price is the Close of the triggering bar. If next-bar-open fills are required, this must be explicitly configured.
- Stop loss is evaluated on bar Close, not intrabar Low (conservative). Intrabar tick SL is a configurable enhancement.
- Lot size tracks the NSE-published contract size for each index (currently
  NIFTY=65, BANKNIFTY=30 — see §2.3). The number is configurable in
  `configs/default.yaml`; any revision must also be mirrored in the
  developer backtest scripts, which keep their own copy of the constant.
  Because MTM thresholds (`max_loss`, `target`) are denominated in rupees,
  changing lot size shifts when those thresholds fire and can reroute a
  session's cycle schedule even though the strategy rules are unchanged.
- ATM is the nearest listed strike to spot. If spot falls exactly between two strikes, the lower strike is selected (configurable).

### 12.2 Answers to Open Questions
1. **Daily maximum loss across all cycles?** — Manual override only. No hard daily stop by default.
2. **Adaptive strike offset?** — No. Remain hard-coded at ±6.
3. **Expiry?** — Monthly.
4. **Slippage in paper?** — None. Market orders, LTP fills.
5. **Brokerage / STT in P&L?** — No. Match with broker's own numbers.
6. **Lazy leg target strike missing in chain?** — Order fails at broker; status reflected in the app UI.
7. **Lock-and-trail?** — Removed after backtesting; see §5.2.

### 12.3 Platform Decisions
- **Broker**: Dhan (REST for orders + WebSocket for market data).
- **Tech stack**: Python ≥3.11, uvloop (POSIX only), asyncio, FastAPI, SQLite, dhanhq SDK.
- **Deployment**: VPS or developer workstation. Dashboard bound to `127.0.0.1` by default.
- **Paper trading fills**: LTP (fallback to best bid/ask when available from 20-depth).
- **Market data feed**: Dhan ticker / quote WebSocket. Upgradable to 20-depth if needed.
- **Order type**: MARKET, intraday (MIS) only.
- **Concurrency**: Four engines per process (paper×NIFTY, paper×BANKNIFTY, live×NIFTY, live×BANKNIFTY). Max one cycle per engine at any time → up to 4 concurrent cycles, each with up to 4 legs.
- **Auto square-off**: 15:15 hard close (applies to both modes).
- **Process hygiene**: PID file + psutil check on startup (skips self, ancestors, descendants to avoid killing the launcher on Windows). SIGINT/SIGTERM handlers for clean shutdown.

---

**END OF DOCUMENT**
