"""MTM controller — simple max_loss / target guard.

Lock-and-trail was removed (2026-04). Backtests showed it didn't
improve outcomes at the cycle horizons we run, and the extra state
machinery complicated paper/live parity checks.

Exit priority (FRD §5.4):
    1. session close (handled by engine, not here)
    2. MTM_MAX_LOSS
    3. MTM_TARGET
    4. LEG_SL (handled per-leg, not here)
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
        """Update cycle peak/trough and return an exit decision if any."""
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
