"""Scheduler readings are ordered per attempt: a lower reading is never news."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_slurm.api import PhaseAnomaly, PhaseRegister, SlurmPhase, phase_of

_ORDERED = (SlurmPhase.PENDING, SlurmPhase.RUNNING, SlurmPhase.COMPLETING, SlurmPhase.ENDED)
_READING = st.tuples(st.sampled_from((*_ORDERED, SlurmPhase.UNKNOWN)), st.integers(0, 3))


@given(readings=st.lists(_READING, max_size=40))
def test_the_held_order_never_decreases_within_an_attempt(
    readings: list[tuple[SlurmPhase, int]],
) -> None:
    """For any reading sequence, (attempt, phase) is non-decreasing and UNKNOWN is inert."""
    register = PhaseRegister()
    held: list[tuple[int, int]] = []
    for phase, attempt in readings:
        merged = register.merge(phase, attempt=attempt)
        if register.phase is not None:
            held.append((register.attempt, _ORDERED.index(register.phase)))
        if phase is SlurmPhase.UNKNOWN:
            assert merged.anomaly is None
    assert held == sorted(held)


def test_a_requeue_is_recorded_not_a_regression() -> None:
    """RUNNING then PENDING of a higher attempt restarts the order."""
    register = PhaseRegister()
    register.merge(SlurmPhase.PENDING)
    register.merge(SlurmPhase.RUNNING)
    merged = register.merge(SlurmPhase.PENDING, attempt=1)
    assert (merged.phase, merged.attempt, merged.requeued) == (SlurmPhase.PENDING, 1, True)
    assert merged.anomaly is None
    stale = register.merge(SlurmPhase.RUNNING, attempt=0)
    assert stale.anomaly is PhaseAnomaly.STALE_ATTEMPT
    assert stale.phase is SlurmPhase.PENDING


def test_a_lower_phase_of_the_same_attempt_is_clamped_and_reported() -> None:
    """Without a new attempt, RUNNING then PENDING is a stale read, not a requeue."""
    register = PhaseRegister()
    register.merge(SlurmPhase.RUNNING)
    merged = register.merge(SlurmPhase.PENDING)
    assert merged.phase is SlurmPhase.RUNNING
    assert merged.anomaly is PhaseAnomaly.REGRESSION_CLAMPED


@given(
    raw=st.sampled_from(
        (
            "PENDING",
            "CONFIGURING",
            "REQUEUED",
            "RUNNING",
            "SUSPENDED",
            "COMPLETING",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "NODE_FAIL",
            "PREEMPTED",
            "SOMETHING_NEW",
        )
    )
)
def test_every_raw_state_has_exactly_one_phase(raw: str) -> None:
    """The mapping is total: new Slurm states are UNKNOWN, never an error."""
    assert phase_of(raw) in set(SlurmPhase)
    assert (phase_of(raw) is SlurmPhase.UNKNOWN) == (raw == "SOMETHING_NEW")
