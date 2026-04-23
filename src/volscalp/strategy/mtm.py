"""MTM controller — aggregate-MTM exit with lock-and-trail ratchet.

Mirrors Codex's ``simulate_strategy_day`` (spread_lab.py:1264-1287). Fed
one ``mtm`` value per call (``realized + unrealized`` summed across all
legs in the current cycle); emits an exit decision if any threshold is
breached.

Exit priority (FRD §5.4):
    1. session close          (handled by engine, not here)
    2. MTM_MAX_LOSS           (``mtm <= -max_loss``)
    3. MTM_TARGET             (``mtm >= target``)
    4. LOCK_TRAIL             (``mtm <= lock_floor`` once armed)
    5. LEG_SL                 (handled per-leg, not here)

Lock-and-trail is a protective profit-floor ratchet: once peak_mtm
clears ``lock_start``, the floor sits at ``lock_profit`` and moves up by
``trail_lock_step`` for every full ``trail_step`` rupees of peak beyond
``lock_start``. The cycle force-closes if the unrealised gives back
enough to breach that floor. Disable by leaving the lock fields unset.
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

    def _lock_floor(self, peak_mtm: float) -> float | None:
        """Protective floor derived from peak_mtm, or None if inactive.

        Inactive if any of the four lock knobs is unset, or if peak has
        not yet cleared ``lock_start``. The floor ratchets up step-wise
        with peak above ``lock_start`` — one ``trail_lock_step`` of
        floor per full ``trail_step`` of peak progression.
        """
        p = self.profile
        if (
            p.lock_start is None
            or p.lock_profit is None
            or p.trail_step is None
            or p.trail_lock_step is None
            or p.trail_step <= 0
        ):
            return None
        if peak_mtm < p.lock_start:
            return None
        extra_steps = int(max(0.0, peak_mtm - p.lock_start) // p.trail_step)
        return p.lock_profit + extra_steps * p.trail_lock_step

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

        floor = self._lock_floor(cycle.peak_mtm)
        if floor is not None and mtm <= floor:
            return MtmDecision(exit=True, reason=ExitReason.LOCK_TRAIL)

        return MtmDecision(exit=False)
