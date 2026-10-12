"""An accepted interruption always ends: it completes, or it fails with a named reason.

An interruption cancels a turn, retains its workspace, then refunds. Its claim waits on the
retained checkpoint. When the executor answers the checkpoint request without a retained
revision, or core never gets a checkpoint request out, nothing is left to end the claim, so
core closes it and reports ``InterruptFailed``. These tests drive the public ``step``.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_interruptions import interrupt_state
from .test_session_turns import reload_step, turn_observation


def _interrupted_until_checkpoint() -> tuple[core.CoreState, core.SnapshotAndRetainRun]:
    state, event = interrupt_state()
    interrupted = reload_step(state, event)
    request = state.intents.intents[0].request
    assert isinstance(request, core.DispatchTurn)
    ended = reload_step(
        interrupted.state,
        core.TurnObserved(
            invocation=event.invocation,
            observation=turn_observation(
                request, terminal=True, accepted=True, status=core.ObservationStatus.CANCELLED
            ),
        ),
    )
    snapshot = next(row for row in ended.requests if isinstance(row, core.SnapshotAndRetainRun))
    return ended.state, snapshot


def _answered(
    state: core.CoreState, snapshot: core.SnapshotAndRetainRun, status: core.ObservationStatus
) -> core.Transition:
    """The shell dispatches the checkpoint request, then the executor answers it."""
    assert snapshot.request_id is not None
    dispatched = core.step(state, core.DispatchAuthorized(request_id=snapshot.request_id))
    observation = turn_observation(snapshot, terminal=True, accepted=True, status=status)
    return reload_step(dispatched.state, core.RequestObserved(observation=observation))


def _open_interrupt_waits(state: core.CoreState) -> list[core.Wait]:
    return [wait for wait in core.orphan_waits(state) if wait.waiter == core.WaitKind.INTERRUPT]


@given(
    status=st.sampled_from(
        [
            core.ObservationStatus.FAILED,
            core.ObservationStatus.CANCELLED,
            core.ObservationStatus.SUCCEEDED,  # succeeded, but retained no revision
        ]
    )
)
def test_an_interruption_whose_checkpoint_fails_ends_failed_not_open(
    status: core.ObservationStatus,
) -> None:
    state, snapshot = _interrupted_until_checkpoint()
    result = _answered(state, snapshot, status)
    failures = [row for row in result.events if isinstance(row, core.InterruptFailed)]
    assert len(failures) == 1
    assert failures[0].invocation == snapshot.invocation
    assert not [row for row in result.events if isinstance(row, core.InterruptCompleted)]
    assert result.state.sessions.interrupts[0].phase == "blocked"
    assert _open_interrupt_waits(result.state) == []


def test_a_live_checkpoint_keeps_the_interruption_waiting() -> None:
    state, snapshot = _interrupted_until_checkpoint()
    waits = [wait for wait in core.waits(state) if wait.waiter == core.WaitKind.INTERRUPT]
    assert [wait.producer for wait in waits] == [core.Producer.PENDING_REQUEST]
    assert snapshot.request_id is not None
