"""MTM controller with lock-and-trail logic.

FRD §5.2:
    - Once cycle MTM reaches `lock_activation`, lock a floor at `lock_floor`.
    - After lock, the floor trails up in `trail_step` increments as MTM improves.
    - Exit when MTM <= floor (post-lock) OR MTM <= max_loss OR MTM >= target.

Exit priority (FRD §5.4):
    1. session close (handled by engine, not here)
    2. MTM_MAX_LOSS
    3. LOCK_TRAIL_FLOOR
    4. MTM_TARGET
    5. LEG_SL (handled per-leg, not here)
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import MtmProfile
from ..models import Cycle, ExitReason


@dataclass
class MtmDecision:
    exit: bool
    reason: ExitReason | None = None
    floor: float = 0.0
    locked: bool = False


class MtmController:
    def __init__(self, profile: MtmProfile):
        self.profile = profile

    def update(self, cycle: Cycle, mtm: float) -> MtmDecision:
        """Update cycle peak/trough/lock state and return an exit decision if any."""
        cycle.mtm = mtm
        if mtm > cycle.peak_mtm:
            cycle.peak_mtm = mtm
        if mtm < cycle.trough_mtm:
            cycle.trough_mtm = mtm

        # Trail step: after lock, raise the floor by trail_step for every
        # `trail_step` improvement above the activation level.
        if cycle.lock_activated:
            improved_by = max(0.0, mtm - self.profile.lock_activation)
            steps = int(improved_by // self.profile.trail_step)
            trailed = self.profile.lock_floor + steps * self.profile.trail_step
            if trailed > cycle.lock_floor:
                cycle.lock_floor = trailed
        elif mtm >= self.profile.lock_activation:
            cycle.lock_activated = True
            cycle.lock_floor = self.profile.lock_floor

        # Priority order.
        if mtm <= -abs(self.profile.max_loss):
            return MtmDecision(exit=True, reason=ExitReason.MTM_MAX_LOSS,
                               floor=cycle.lock_floor, locked=cycle.lock_activated)
        if cycle.lock_activated and mtm <= cycle.lock_floor:
            return MtmDecision(exit=True, reason=ExitReason.LOCK_TRAIL_FLOOR,
                               floor=cycle.lock_floor, locked=True)
        if mtm >= self.profile.target:
            return MtmDecision(exit=True, reason=ExitReason.MTM_TARGET,
                               floor=cycle.lock_floor, locked=cycle.lock_activated)

        return MtmDecision(exit=False, floor=cycle.lock_floor, locked=cycle.lock_activated)
