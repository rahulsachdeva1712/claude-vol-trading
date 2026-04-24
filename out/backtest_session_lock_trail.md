# Session lock-and-trail — backtest & sweep report

- Range: **2025-10-24** .. **2026-04-23** (130 weekdays, 228 index-sessions)
- Indices: NIFTY, BANKNIFTY
- Data source: Dhan `/v2/charts/rollingoption` cache under
  `data/backtest_cache/rollingoption/` (196 JSON files = 2 indices ×
  7 months × 14 offsets). Fetched fresh by
  `scripts/backtest_history_fetch.py`.
- Strategy: unchanged from
  [FRD.md §5 / §9](../FRD.md) — Repeated OTM Strangle with
  per-cycle `max_loss=2500`, `target=300`, base legs ATM±6, lazy on
  opposite side at fresh ATM±6, leg-SL slip modelled at 100 bps.
- Session lock-and-trail is layered ON TOP of the per-cycle rules as
  an additional gate per (index, session); it never overrides or
  weakens per-cycle risk.

---

## 1. Baseline (no session halt)

From the fresh run — identical to the headline numbers in
`out/backtest_6m_new.md` (the old `backtest_6m_baseline.md` used a
since-revised Dhan cache for NIFTY April 2026, giving 5093 cycles /
+Rs 1.53M — our fresh fetch returns 4809 cycles / +Rs 1.43M; the
delta is entirely NIFTY 2026-04, everything else matches to the rupee).

| Metric | Value |
|:--|--:|
| Cycles simulated | 4,809 |
| Total P&L | **+Rs 1,434,327.35** |
| Cycle win rate | 83.5% |
| Session-day win rate (228 pairs) | **86.0%** (196 pos / 1 flat / 31 neg) |
| Mean daily P&L (per index-session) | +Rs 6,290.91 |
| Stdev daily P&L | Rs 7,524.06 |
| Sharpe-ish (mean/stdev) | 0.836 |
| Best session-day | +Rs 36,387 (NIFTY 2026-03-24) |
| Worst session-day | **-Rs 2,506** (BANKNIFTY 2025-12-23) |
| Month win rate | 100% (all 7 months net positive) |
| Week win rate | 92.6% |

**Exit-reason mix (baseline):**

| Reason | Count | % | P&L |
|:--|--:|--:|--:|
| MTM_TARGET | 4,482 | 93.2% | +Rs 1,563,671 |
| SESSION_CLOSE | 214 | 4.4% | -Rs 69,888 |
| LEG_SL | 109 | 2.3% | -Rs 48,678 |
| MTM_MAX_LOSS | 4 | 0.1% | -Rs 10,778 |

### Peak-to-close "give-back" profile

The motivating anecdote was *"MTM went till Rs 5000 and then came
back to Rs 2310"*. That pattern in the 228-day sample:

- Session-days that **peaked ≥ Rs 2,000 and gave back ≥ Rs 1,500**
  before session close: **6 / 228 (2.6%)**
- Largest give-back was NIFTY 2026-03-27: peak +Rs 3,497 → close
  +Rs 62 (gave back Rs 3,435 but still finished positive).
- Only 1 day (NIFTY 2026-01-13) peaked above Rs 5,000
  (peak +Rs 6,097, close +Rs 4,189 — gave back Rs 1,908).
- Worst session-day bottomed at **-Rs 2,506** — no session breached
  even a -Rs 3,000 floor. Every `daily_loss_limit` tested
  (3,000 / 5,000) therefore **never fires** in this 6-month window.

---

## 2. Sweep table (ranked by composite)

Composite score = `total_pnl × (session_day_win_rate/100) / (1 + |stdev/mean|)`.
Ties broken by total P&L.

All 63 soft-mode configs from the Cartesian product (6 lock-triggers
× 4 trail-drops × 3 daily-loss-limits × 1 halt-mode, minus the
collapsed `lock_trigger=None ⇒ trail_drop=None` duplicates). Baseline
is rank 1. The `daily_loss_limit` column is dashed where the config
has it set but it never fires (identical output to the `off` variant).

| Rank | Lock | Trail | DLL | Cyc | Total P&L | Cycle win% | SDay win% | Stdev/day | Composite | Δ vs baseline |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1  | off     | off      | off  | 4809 | +1,434,327 | 83.5% | 86.0% | 7,524 | 561,479 | **baseline** |
| 1= | off     | off      | 3000 | 4809 | +1,434,327 | 83.5% | 86.0% | 7,524 | 561,479 | 0 |
| 1= | off     | off      | 5000 | 4809 | +1,434,327 | 83.5% | 86.0% | 7,524 | 561,479 | 0 |
| 1= | 1500    | off      | off  | 4809 | +1,434,327 | 83.5% | 86.0% | 7,524 | 561,479 | 0 |
| …  | *(rows 4–39 all identical to baseline — the trigger never arms OR the trail never fires)* |
| 40 | 4000    | 2000     | any  | 4794 | +1,430,680 | 83.5% | 86.0% | 7,527 | 559,148 | -3,647 |
| 43 | 2500    | 2000     | any  | 4780 | +1,428,779 | 83.5% | 86.0% | 7,531 | 557,849 | -5,549 |
| 46 | 10000   | 1000     | any  | 4769 | +1,414,173 | 83.5% | 86.0% | 7,372 | 555,475 | -20,154 |
| 49 | 6000    | 1000     | any  | 4728 | +1,398,213 | 83.4% | 86.0% | 7,312 | 548,255 | -36,114 |
| 52 | 1500    | 2000     | any  | 4747 | +1,419,589 | 83.5% | 85.1% | 7,553 | 545,791 | -14,738 |
| 55 | 4000    | 1000     | any  | 4699 | +1,389,895 | 83.4% | 86.0% | 7,319 | 542,950 | -44,432 |
| 58 | 2500    | 1000     | any  | 4601 | +1,367,024 | 83.4% | 86.0% | 7,308 | 529,616 | -67,303 |
| 61 | 1500    | 1000     | any  | 4547 | +1,356,152 | 83.5% | 85.1% | 7,334 | 516,745 | -78,175 |

Full CSV in `out/sweep_session.csv`; per-config monthly breakdowns in
`out/sweep_session.md`.

**Hard-mode spot-check** (lt ∈ {1500, 2500, 4000} × td ∈ {1000, 2000},
12 configs in `out/sweep_session_hard.csv`):

| Config | Total P&L | Δ vs baseline |
|:--|--:|--:|
| lt=4000 td=2000 hm=hard | +1,411,882 | -22,446 |
| lt=2500 td=2000 hm=hard | +1,406,302 | -28,026 |
| lt=1500 td=2000 hm=hard | +1,396,886 | -37,441 |

Hard mode force-closes the active cycle at the bar the trail fires,
which removes ~55 cycles per config and ~Rs 20-40k from the book
vs baseline. No hard-mode config beats baseline.

### Observations

1. **Loss-limit configs are inert in this window.** The single worst
   session-day finished at -Rs 2,506 — no day ever crossed a
   -Rs 3,000 floor, so `daily_loss_limit=3000` and `=5000` behave
   identically to `off`. That's why dozens of rows tie at rank 1.
2. **High lock_trigger + wide trail_drop = identical to baseline.**
   A trail armed at lock=10000 with drop=3500 never fires because no
   session pair in this sample peaks above Rs 10,000 and then gives
   back ≥ Rs 3,500 (top cross-index day peaks at +Rs 36,387 but
   never retraces by that magnitude).
3. **The only configs that *do* fire — tight triggers with tight
   trails — all LOSE money.** Every row that deviates from baseline
   deviates negatively. The tightest (lt=1500 td=1000) loses Rs 78k
   (-5.4% vs baseline) and cuts 262 cycles (~5.5%) without
   materially improving stdev.
4. **Session-day win rate barely moves.** Baseline 86.0% → tightest
   halt config 85.1%. Halting a day after the trail fires prevents
   a few good cycles; the natural flow already exits losing cycles
   quickly via per-cycle `max_loss=2500`.
5. **Every month is positive under baseline.** Month-win-rate and
   week-win-rate are 100% and 92.6% respectively — can't be improved.

---

## 3. Top 5 configs — full breakdown

### #1 — baseline (no session halt)
- Cycles: 4,809 · Total: **+Rs 1,434,327**
- Cycle/session-day win: 83.5% / 86.0%
- Mean/stdev daily: +Rs 6,291 / Rs 7,524 · Sharpe-ish 0.836
- Best/worst day: +Rs 36,387 / -Rs 2,506

| Month | Days | Pos | P&L |
|:--|--:|--:|--:|
| 2025-10 | 6 | 4 | +Rs 19,095 |
| 2025-11 | 19 | 16 | +Rs 102,724 |
| 2025-12 | 22 | 16 | +Rs 99,382 |
| 2026-01 | 20 | 20 | +Rs 209,963 |
| 2026-02 | 20 | 19 | +Rs 154,823 |
| 2026-03 | 19 | 19 | +Rs 631,019 |
| 2026-04 | 15 | 15 | +Rs 217,322 |

### #2 — `lock_trigger=4000, trail_drop=2000` (soft)
- Cycles: 4,794 (-15) · Total: **+Rs 1,430,680** (-Rs 3,647)
- Identical session-day win rate (86.0%) and stdev.
- The trail fires twice in the whole window (NIFTY 2025-12-18 and
  NIFTY 2026-03-27), closing ~15 cycles that would otherwise have
  run. Net effect: marginal P&L drag, no consistency improvement.

### #3 — `lock_trigger=2500, trail_drop=2000` (soft)
- Cycles: 4,780 (-29) · Total: **+Rs 1,428,779** (-Rs 5,549)
- Same win rate, same stdev floor (-2,506).
- Trail fires ~3 times. Each halt prevents 5-10 cycles from opening
  but those cycles were, on aggregate, profitable.

### #4 — `lock_trigger=10000, trail_drop=1000` (soft)
- Cycles: 4,769 (-40) · Total: **+Rs 1,414,173** (-Rs 20,154)
- Trail arms on ~4 days (days that booked > Rs 10k mid-day on a
  single index). On those days the tight 1k drop catches a lot of
  normal noise and halts too early. Stdev goes DOWN (7,372 from
  7,524) but so does mean — net: negative.

### #5 — `lock_trigger=1500, trail_drop=2000` (soft)
- Cycles: 4,747 (-62) · Total: **+Rs 1,419,589** (-Rs 14,738)
- SDay win rate drops slightly to 85.1% (first config where it moves).
- Trail arms the earliest (any +Rs 1,500 session) → fires most
  often → biggest cycle count hit.

---

## 4. Recommendation

**Ship baseline. Do not enable session lock-and-trail in this window.**

All 24 sweep rows that deviate from baseline deviate negatively in
total P&L (range: -Rs 3,647 to -Rs 78,175); none improve session-day
win rate, month/week consistency (already 100% / 92.6%), or worst-day
drawdown (already capped at -Rs 2,506 by the per-cycle `max_loss`).
The user's motivating example ("5000 → 2310 give-back") happened
zero times in 228 sample days — the dataset's biggest peak-to-close
retrace is NIFTY 2026-03-27 (+3,497 → +62) which still finished
positive; the closest match to the anecdote is NIFTY 2026-01-13
(+6,097 → +4,189), which the proposed 1500/1000 config would have
halted at ~+2,500, *reducing* that day's take.

The per-cycle `max_loss=2500` already does the work that a session
halt is meant to do — the 228-day worst close is -Rs 2,506 and only
31 days finish net negative. Layering a second halt on top only
trims profitable upside. Revisit when we have a 2-year window
(or a single live day that actually hits the pattern) — the
`--session-*` flags are in place and safe to enable per-day from
the CLI.

---

## Reproduction

```bash
# 1. Fetch the cache (one-time, ~5 min, idempotent)
.venv\Scripts\python.exe scripts\backtest_history_fetch.py \
    --from 2025-10-24 --to 2026-04-23 --fan 6

# 2. Baseline
.venv\Scripts\python.exe scripts\backtest_2y.py \
    --from 2025-10-24 --to 2026-04-23 --leg-sl-slip-bps 100

# 3. Soft-mode sweep
.venv\Scripts\python.exe scripts\backtest_sweep_session.py \
    --from 2025-10-24 --to 2026-04-23 \
    --lock-triggers off,1500,2500,4000,6000,10000 \
    --trail-drops off,1000,2000,3500 \
    --loss-limits off,3000,5000 \
    --halt-modes soft \
    --out out\sweep_session.csv --md out\sweep_session.md

# 4. Hard-mode spot-check
.venv\Scripts\python.exe scripts\backtest_sweep_session.py \
    --from 2025-10-24 --to 2026-04-23 \
    --lock-triggers 1500,2500,4000 \
    --trail-drops 1000,2000 \
    --loss-limits off,3000 \
    --halt-modes hard \
    --out out\sweep_session_hard.csv --md out\sweep_session_hard.md
```
