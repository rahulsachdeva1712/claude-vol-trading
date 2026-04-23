"""MTM controller — aggregate-MTM exit, target + max_loss only.

Two evaluation paths:

* ``update(cycle, mtm)`` — full evaluation called on **bar close**.
  Checks MAX_LOSS (priority) then TARGET, and updates both peak and
  trough. Used for MAX_LOSS gating so a transient mid-bar dip does
  not force-close a cycle that would have recovered by the bar close.

* ``evaluate_target_only(cycle, mtm)`` — target-only check called on
  the engine's **intrabar 1-second tick**. Updates peak (not trough)
  and fires MTM_TARGET if the aggregate MTM has crossed the take-profit
  threshold at any moment between bar closes. MAX_LOSS is intentionally
  NOT evaluated here so the hard stop stays end-of-bar.

Exit priority (FRD §5.4):
    1. session close          (handled by engine, not here)
    2. MTM_MAX_LOSS           (``mtm <= -max_loss`` at bar close)
    3. MTM_TARGET             (``mtm >= target`` at any tick intrabar)
    4. LEG_SL                 (handled per-leg, not here)

Lock-and-trail was evaluated and removed (2026-04) — it did not improve
P&L vs the plain max_loss/target pair in the 2y backtest.
Target was moved from bar-close to intrabar (2026-04) after backtest
showed +55% cycle count and +55% P&L across April 2026 with
unchanged MAX_LOSS. See FRD §5.2 / §5.4.
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

    def update(self, cycle: Cycle, mtm: float) -> MtmDecision:
        """Bar-close evaluation. Updates peak/trough and returns any exit."""
        cycle.mtm = mtm
        if mtm > cycle.peak_mtm:
            cycle.peak_mtm = mtm
        if mtm < cycle.trough_mtm:
            cycle.trough_mtm = mtm

        if mtm <= -abs(self.profile.max_loss):
            return MtmDecision(exit=True, reason=ExitReason.MTM_MAX_LOSS)
        if mtm >= self.profile.target:
            return MtmDecision(exit=True, reason=ExitReason.MTM_TARGET)

        return MtmDecision(exit=False)

    def evaluate_target_only(self, cycle: Cycle, mtm: float) -> MtmDecision:
        """Intrabar target-only evaluation.

        Updates ``cycle.mtm`` and peak (higher of current peak and this
        sample) — but does NOT touch trough, which stays bar-close to
        match the MAX_LOSS gating cadence. Fires MTM_TARGET if the
        sample crosses the take-profit threshold.
        """
        cycle.mtm = mtm
        if mtm > cycle.peak_mtm:
            cycle.peak_mtm = mtm
        if mtm >= self.profile.target:
            return MtmDecision(exit=True, reason=ExitReason.MTM_TARGET)
        return MtmDecision(exit=False)
