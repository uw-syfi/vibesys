"""Every record phase is open work or it is not, and every reader of that agrees (#3475).

A record is open work while it waits on its executor for a result: prepared, dispatched or
reconciling. A completed record is done. A blocked record ended at its limit and nothing sends it
again, so it is not open: a quota pause must not wait on it, and it is not part of the outbox.
A blocked record that named a resource still holds the run's closure until the release is proven,
which is a different question (`unreleased_records`).
"""

from __future__ import annotations

import pytest

import vs_core.api as core

from .test_silent_executor_deadline import _silent

_OPEN = frozenset(
    {core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.RECONCILING}
)
_ENDED = frozenset({core.RequestPhase.COMPLETED, core.RequestPhase.BLOCKED})


def _paused_with(phase: core.RequestPhase) -> core.CoreState:
    """A quota-paused run whose only record is in ``phase``."""
    state = _silent(phase, core.ObservationStatus.UNKNOWN, terminal=False)
    return state.model_copy(
        update={"run": state.run.model_copy(update={"status": core.RunStatus.PAUSED})}
    )


def test_every_phase_is_open_or_ended() -> None:
    """A new phase fails here until someone says whether it is open work."""
    assert frozenset(core.RequestPhase) == _OPEN | _ENDED
    assert not _OPEN & _ENDED


@pytest.mark.parametrize("phase", list(core.RequestPhase), ids=lambda phase: phase.name)
def test_pause_waits_for_open_work_and_for_nothing_else(phase: core.RequestPhase) -> None:
    """A blocked record holds no permit: a pause with only it left lands (#3475)."""
    state = _paused_with(phase)
    assert core.admitted_work_remains(state) is (phase in _OPEN)


@pytest.mark.parametrize("phase", list(core.RequestPhase), ids=lambda phase: phase.name)
def test_the_outbox_is_the_open_records(phase: core.RequestPhase) -> None:
    state = _paused_with(phase)
    assert bool(core.pending_requests(state.records)) is (phase in _OPEN)
