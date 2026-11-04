"""An accepted interruption always ends: it completes, or it fails with a named reason.

An interruption cancels a turn, retains its workspace, then refunds. Its claim waits on the
retained checkpoint. When the executor answers the checkpoint request without a retained
revision, or core never gets a checkpoint request out, nothing is left to end the claim, so
core closes it and reports ``InterruptFailed``. These tests drive the public ``step``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_session_interruptions import interrupt_state
from .test_session_turns import reload_step, turn_observation


def _interrupted_until_checkpoint() -> tuple[core.CoreState, core.SnapshotAndRetainRun]:
    state, event = interrupt_state()
    interrupted = reload_step(state, event)
    request = state.records.records[0].request
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


def _open_interrupt_waits(state: core.CoreState) -> list[core.OpenWait]:
    return [wait for wait in core.orphan_waits(state) if wait.waiter == core.WaitKind.INTERRUPT]


@settings(max_examples=examples(pr=3, nightly=100))
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
    assert result.state.sessions.interrupts[0].wait is None
    assert _open_interrupt_waits(result.state) == []


def test_a_live_checkpoint_keeps_the_interruption_waiting() -> None:
    state, snapshot = _interrupted_until_checkpoint()
    waits = [wait for wait in core.waits(state) if wait.waiter == core.WaitKind.INTERRUPT]
    assert [wait.producer for wait in waits] == [core.Producer.PENDING_REQUEST]
    assert snapshot.request_id is not None


def test_a_draining_interruption_records_the_checkpoint_it_waits_on() -> None:
    """The step that requests the checkpoint names it on the claim (#2428); nothing infers it."""
    state, snapshot = _interrupted_until_checkpoint()
    (claim,) = state.sessions.interrupts
    assert snapshot.request_id is not None
    assert claim.phase == "draining"
    assert claim.wait == core.WaitOn(requests=(snapshot.request_id,))


def test_an_interruption_whose_checkpoint_keeps_answering_retryably_is_retried_then_fails() -> None:
    """A retryable answer is asked again after a delay, a bounded number of times (#2154).

    Without that the checkpoint stayed dispatched with nobody to ask again, the clock ticked
    to the run deadline, and the interruption never ended. After every step no wait of any
    kind is an orphan, and the interruption ends failed with the named reason.
    """
    state, snapshot = _interrupted_until_checkpoint()
    assert snapshot.request_id is not None
    limit = state.run.limits.request_retries.limit
    clock = 10.0
    asks = 0
    events: list[core.StrategyEvent] = []
    for sequence in range(2, 80):
        if asks > limit + 1:
            break
        phase = next(
            row.phase for row in state.records.records if row.request_id == snapshot.request_id
        )
        if phase == core.RequestPhase.PREPARED:
            state = core.step(state, core.DispatchAuthorized(request_id=snapshot.request_id)).state
            asks += 1
            answer = turn_observation(
                snapshot,
                sequence=sequence,
                accepted=True,
                terminal=False,
                status=core.ObservationStatus.FAILED,
            )
            result = reload_step(state, core.RequestObserved(observation=answer))
        elif phase == core.RequestPhase.COMPLETED:
            break
        else:
            clock += 31.0
            result = reload_step(state, core.ClockAdvanced(now_at=clock))
        state = result.state
        events.extend(result.events)
        assert core.orphan_waits(state) == ()
    assert asks == limit + 1
    [failure] = [row for row in events if isinstance(row, core.InterruptFailed)]
    assert failure.invocation == snapshot.invocation
    assert state.sessions.interrupts[0].phase == "blocked"
