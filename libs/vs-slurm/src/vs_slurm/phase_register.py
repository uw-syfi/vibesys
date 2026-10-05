"""Order scheduler observations of one job so a late or lower reading is never news."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .runner import SlurmPhase

_ORDER = {
    SlurmPhase.PENDING: 0,
    SlurmPhase.RUNNING: 1,
    SlurmPhase.COMPLETING: 2,
    SlurmPhase.ENDED: 3,
}


class PhaseAnomaly(StrEnum):
    """A reading that disagreed with the order and was not applied."""

    REGRESSION_CLAMPED = "regression_clamped"
    STALE_ATTEMPT = "stale_attempt"


@dataclass(frozen=True)
class MergedPhase:
    """The ordered phase after one reading, and what the reading did to the order."""

    phase: SlurmPhase
    attempt: int
    anomaly: PhaseAnomaly | None = None
    # True when this reading started a new attempt (a scheduler requeue).
    requeued: bool = False


class PhaseRegister:
    """Hold the highest (attempt, phase) seen for one job.

    Order is per attempt. Within an attempt the phase only advances, so a reading
    below the held phase (a stale squeue after accounting, or a lagging node) is
    clamped to the held phase and reported as an anomaly. A higher attempt is a
    requeue: the order restarts, so RUNNING followed by PENDING is legal and
    recorded. A lower attempt is a stale reading and is ignored. UNKNOWN carries
    no information and is never merged.
    """

    def __init__(self) -> None:
        """Start with nothing observed."""
        self._attempt = 0
        self._phase: SlurmPhase | None = None

    @property
    def phase(self) -> SlurmPhase | None:
        """The ordered phase so far, or None before the first informative reading."""
        return self._phase

    @property
    def attempt(self) -> int:
        """The highest scheduler attempt seen."""
        return self._attempt

    def merge(self, phase: SlurmPhase, *, attempt: int = 0) -> MergedPhase:
        """Fold one reading into the register and return the ordered result."""
        held = self._phase
        if phase is SlurmPhase.UNKNOWN:
            return MergedPhase(held or SlurmPhase.UNKNOWN, self._attempt)
        if held is None:
            self._phase, self._attempt = phase, attempt
            return MergedPhase(phase, attempt)
        if attempt != self._attempt:
            return self._merge_other_attempt(held, phase, attempt)
        if _ORDER[phase] < _ORDER[held]:
            return MergedPhase(held, attempt, PhaseAnomaly.REGRESSION_CLAMPED)
        self._phase = phase
        return MergedPhase(phase, attempt)

    def _merge_other_attempt(
        self, held: SlurmPhase, phase: SlurmPhase, attempt: int
    ) -> MergedPhase:
        # A job that ended does not requeue, and a lower attempt is old news.
        if attempt < self._attempt or held is SlurmPhase.ENDED:
            return MergedPhase(held, self._attempt, PhaseAnomaly.STALE_ATTEMPT)
        self._phase, self._attempt = phase, attempt
        return MergedPhase(phase, attempt, requeued=True)
