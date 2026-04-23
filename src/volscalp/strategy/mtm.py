"""MTM controller — aggregate-MTM exit, target + max_loss, both intrabar.

Two evaluation paths:

* ``update(cycle, mtm)`` — full evaluation called on **bar close**.
  Updates both peak and trough with the bar-close sample and fires
  MAX_LOSS (priority) or TARGET as a last-resort safety net if the
  intrabar path missed a tick (e.g. WS stall, engine backpressure).

* ``evaluate_intrabar(cycle, mtm)`` — full evaluation called on the
  engine's **1-second tick**. Updates peak and trough continuously so
  the running stats reflect actual intrabar extremes, and fires
  MAX_LOSS / TARGET the moment the aggregate MTM crosses either
  threshold. This is the primary exit path for both thresholds.

Exit priority (FRD §5.4):
    1. session close          (handled by engine, not here)
    2. MTM_MAX_LOSS           (``mtm <= -max_loss`` at any tick intrabar)
    3. MTM_TARGET             (``mtm >= target`` at any tick intrabar)
    4. LEG_SL                 (handled per-leg, not here)

Lock-and-trail was evaluated and removed (2026-04) — it did not improve
P&L vs the plain max_loss/target pair in the 2y backtest.
Target was moved from bar-close to intrabar (2026-04) after backtest
showed +55% cycle count and +55% P&L across April 2026 with
unchanged MAX_LOSS. MAX_LOSS was subsequently moved to intrabar as
well for consistency — on every tick both thresholds are checked,
MAX_LOSS first. See FRD §5.2 / §5.4.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import MtmProfile
from ..models import Cycle, ExitReason


@dataclass
class MtmDecision:
    exit: bool
    reason: ExitReason | None = None


class MtmController:
    def __init__(self, profile: MtmProfile):
        self.profile = profile

    def _check_thresholds(self, mtm: float) -> MtmDecision:
        """Shared threshold logic: MAX_LOSS (priority) then TARGET."""
        if mtm <= -abs(self.profile.max_loss):
            return MtmDecision(exit=True, reason=ExitReason.MTM_MAX_LOSS)
        if mtm >= self.profile.target:
            return MtmDecision(exit=True, reason=ExitReason.MTM_TARGET)
        return MtmDecision(exit=False)

    def update(self, cycle: Cycle, mtm: float) -> MtmDecision:
        """Bar-close evaluation — safety net + end-of-bar peak/trough stamp.

        Updates peak and trough with the bar-close sample and runs the
        same threshold check as the intrabar path. In steady state the
        intrabar tick loop will have already fired any breach; this
        method is the backstop for the cases where the tick loop was
        starved.
        """
        cycle.mtm = mtm
        if mtm > cycle.peak_mtm:
            cycle.peak_mtm = mtm
        if mtm < cycle.trough_mtm:
            cycle.trough_mtm = mtm
        return self._check_thresholds(mtm)

    def evaluate_intrabar(self, cycle: Cycle, mtm: float) -> MtmDecision:
        """Intrabar evaluation — primary path for MAX_LOSS and TARGET.

        Updates peak and trough continuously (every tick sample, not
        just bar-close), so cycle KPIs reflect true intrabar extremes.
        MAX_LOSS is checked first per FRD §5.4 priority.
        """
        cycle.mtm = mtm
        if mtm > cycle.peak_mtm:
            cycle.peak_mtm = mtm
        if mtm < cycle.trough_mtm:
            cycle.trough_mtm = mtm
        return self._check_thresholds(mtm)
