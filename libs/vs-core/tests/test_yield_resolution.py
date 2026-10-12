"""A turn that asked to wait always ends: it is suspended, or it fails with a named reason.

Core records a suspension on the turn when the agent validates a wait, and publishes it
(``TurnSuspended``) only when the checkpoint of the turn's workspace comes back. Every way
that checkpoint can fail to come back (declined by name, failed by the executor, never
requested, retained after the scope stopped accepting work) used to leave the suspension
pending, so the run waited for a publication nothing would make. The step now ends such a
turn with ``YieldPublicationFailed``. These tests drive the production attempts and
sessions through the public ``step`` with generated worlds and executor answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_sibling_fakes import fake_evaluation
from .test_session_yield_completion import terminal_yield, yield_state
from .test_write_turn_retention import write_turn_world

REVISION = core.RevisionRef(
    revision_id=core.RevisionId(root="c" * 40), digest="git-commit:" + "c" * 40
)


def _evaluation(
    state: core.EvaluationState, context: core.EvaluationContext, event: core.EvaluationEvent
) -> core.AreaChange[core.EvaluationState]:
    """Publication is recorded by the fake; every other evaluation event is the real one."""
    if isinstance(event, core.TurnSuspended):
        return fake_evaluation(state, context, event)
    if isinstance(event, core.ContinuationRetireRequested):
        # The fake's continuation names no job, which the real retirement refuses; the
        # continuation's own lifecycle is not under test here.
        return core.AreaChange(state=state)
    return core.advance_evaluation(state, context, event)


REDUCERS = core.CoreReducers(evaluation=_evaluation)


class Run:
    """One yielding turn in an attempt, and everything core said while it was resolved."""

    def __init__(self, state: core.CoreState, request: core.DispatchTurn) -> None:
        self.state = state
        self.request = request
        self.events: list[core.StrategyEvent] = []
        self.snapshots: list[core.SnapshotAndRetain] = []
        self.answered: set[str] = set()
        self.dispatched: set[str] = set()  # snapshot requests the shell has dispatched
        self.fed = 0
        self.clock = 10.0

    @property
    def invocation(self) -> core.Invocation:
        return self.state.sessions.invocations[0]

    def feed(self, event: core.CoreEvent) -> core.Transition:
        self.fed += 1
        result = core.step(self.state, event, reducers=REDUCERS)
        self.state = result.state
        self.events.extend(result.events)
        issued = [row for row in result.requests if isinstance(row, core.SnapshotAndRetain)]
        self.snapshots.extend(issued)
        for snapshot in issued:
            # The shell dispatches a request as soon as the step that issued it commits.
            assert snapshot.request_id is not None
            self.dispatched.add(snapshot.request_id.root)
            self.state = core.step(
                self.state,
                core.DispatchAuthorized(request_id=snapshot.request_id),
                reducers=REDUCERS,
            ).state
        # The invariant under test holds after every step, not only at the end of a run:
        # no wait of any kind is left without something that will end it.
        assert core.orphan_waits(self.state) == ()
        return result

    def outstanding(self) -> list[core.SnapshotAndRetain]:
        """The checkpoint requests the executor has been asked and has not yet concluded."""
        return [
            row.request
            for row in self.state.intents.intents
            if isinstance(row.request, core.SnapshotAndRetain)
            and row.phase != core.IntentPhase.COMPLETED
            and row.request.request_id is not None
            and row.request.request_id.root not in self.answered
        ]

    def wait_out_retries(self, limit: int = 40) -> None:
        """An executor that answers every ask retryably: time passes, the core asks again."""
        for _ in range(limit):
            asked = self.outstanding()
            if not asked:
                return
            for snapshot in asked:
                self.answer(snapshot, core.ObservationStatus.FAILED, retained=False, terminal=False)
            self.clock += 31.0
            self.feed(core.ClockAdvanced(now_at=self.clock))

    def end_turn(self) -> None:
        _, _, suspension = yield_state("dispatch")
        self.feed(terminal_yield(self.state, self.request, suspension))

    def answer(
        self,
        snapshot: core.SnapshotAndRetain,
        status: core.ObservationStatus,
        *,
        retained: bool,
        terminal: bool | None = None,
    ) -> None:
        """The workspace executor's answer; a terminal answer to a request is final."""
        assert snapshot.request_id is not None
        key = snapshot.request_id.root
        if key in self.answered:
            return
        if terminal is None:
            terminal = status != core.ObservationStatus.UNKNOWN
        if terminal:
            self.answered.add(key)
        revision = REVISION if retained and status == core.ObservationStatus.SUCCEEDED else None
        self.feed(
            core.RequestObserved(
                observation=core.Observation(
                    event_id=core.EventId(root=f"answer-{self.fed}"),
                    request_id=snapshot.request_id,
                    scope=snapshot.scope,
                    admission_id=snapshot.admission_id,
                    sequence=self.fed + 1,
                    observed_at=2.0,
                    status=status,
                    accepted=True,
                    terminal=terminal,
                    children_complete=True,
                    revision=revision,
                ),
                revision=revision,
            )
        )

    def of_kind[E: core.Value](self, kind: type[E]) -> list[E]:
        return [row for row in self.events if isinstance(row, kind)]

    def failures(self) -> list[core.YieldPublicationFailed]:
        """The yield failures core reported, found by their wire kind."""
        return [
            row
            for row in self.events
            if row.kind == "yield_publication_failed"
            and isinstance(row, core.YieldPublicationFailed)
        ]

    def live_checkpoint(self) -> bool:
        return any(
            isinstance(row.request, core.SnapshotAndRetain)
            and row.phase in (core.IntentPhase.PREPARED, core.IntentPhase.DISPATCHED)
            for row in self.state.intents.intents
        ) or any(
            isinstance(row.request, core.SnapshotAndRetain)
            and row.phase == core.IntentPhase.RECONCILING
            for row in self.state.intents.intents
        )


def _world(
    *,
    access: core.Access = core.Access.WRITE_CANDIDATE,
    charged: bool = True,
    phase: core.AttemptPhase = core.AttemptPhase.ACTIVE,
    peer_writer: bool = False,
) -> Run:
    state, request = write_turn_world(access=access, charged=charged)
    owner = state.attempts.attempts[0].model_copy(update={"phase": phase})
    sessions = state.sessions
    if peer_writer:
        mine = sessions.invocations[0]
        other = mine.invocation.model_copy(update={"invocation_id": core.InvocationId(root="peer")})
        peer = mine.model_copy(
            update={
                "invocation": other,
                "observation": None,
                "phase": core.SessionPhase.EXECUTING,
                "turn": mine.turn.model_copy(
                    update={
                        "invocation_id": other.invocation_id,
                        "session": mine.turn.session.model_copy(
                            update={
                                "session_id": other.session_id,
                                "access": core.Access.WRITE_CANDIDATE,
                            }
                        ),
                    }
                ),
            }
        )
        sessions = sessions.model_copy(update={"invocations": (mine, peer)})
    state = state.model_copy(
        update={"attempts": core.AttemptsState(attempts=(owner,)), "sessions": sessions}
    )
    assert isinstance(request, core.DispatchTurn)
    return Run(state, request)


def _assert_resolved(run: Run, *, answered_checkpoint: bool) -> None:
    """The turn that asked to wait was suspended, is still being checkpointed, or failed."""
    invocation = run.invocation
    if not any(row.suspending for row in run.of_kind(core.TurnResult)):
        # Core refused the wait at the end of the turn (or never saw the turn end), so it
        # never promised a publication: nothing was opened, and nothing may be pending.
        assert invocation.pending_suspension is None
        assert run.failures() == []
        return
    failures = run.failures()
    assert len(failures) <= 1
    if failures:
        [failure] = failures
        assert failure.invocation == invocation.invocation
        assert invocation.pending_suspension is None
        assert invocation.phase == core.SessionPhase.TERMINAL
        assert run.state.sessions.sessions[0].phase in (
            core.SessionPhase.IDLE,
            core.SessionPhase.CLOSING,
            core.SessionPhase.TERMINAL,
        )
        assert run.state.run.receipts[0].completion == core.CompletionStatus.FAILED
        assert not run.state.evaluation.continuations
        recorded = [row.reason for row in run.state.attempts.attempts[0].checkpoint_declines]
        if recorded and failure.reason == core.CheckpointFailure.CHECKPOINT_DECLINED:
            assert failure.decline == recorded[-1]
    elif invocation.pending_suspension is None:
        assert invocation.phase == core.SessionPhase.SUSPENDED
        assert len(run.state.evaluation.continuations) == 1
    else:
        # Still waiting is legitimate only while the checkpoint is genuinely in flight.
        assert run.live_checkpoint()
        assert not answered_checkpoint or run.snapshots


ACCESS = st.sampled_from([core.Access.READ_ONLY, core.Access.WRITE_CANDIDATE])
PHASES = st.sampled_from(list(core.AttemptPhase))
# The retryable answer is a failure the executor does not call final (terminal=False).
ANSWERS = st.sampled_from(
    [
        (core.ObservationStatus.SUCCEEDED, True, True),
        (core.ObservationStatus.SUCCEEDED, False, True),
        (core.ObservationStatus.FAILED, False, True),
        (core.ObservationStatus.FAILED, False, False),
        (core.ObservationStatus.REJECTED, False, True),
        (core.ObservationStatus.CANCELLED, False, True),
        (core.ObservationStatus.UNKNOWN, False, False),
    ]
)


@dataclass(frozen=True)
class Scenario:
    """A world the turn ends in, and what the executor and the operator then do."""

    access: core.Access
    charged: bool
    phase: core.AttemptPhase
    peer_writer: bool
    answers: tuple[tuple[core.ObservationStatus, bool, bool], ...]
    retire: Literal["cancel", "park", "settle"] | None
    replay: bool


SCENARIOS = st.builds(
    Scenario,
    access=ACCESS,
    charged=st.booleans(),
    phase=PHASES,
    peer_writer=st.booleans(),
    answers=st.lists(ANSWERS, max_size=3).map(tuple),
    retire=st.sampled_from([None, "cancel", "park", "settle"]),
    replay=st.booleans(),
)


@settings(max_examples=300, deadline=None)
@given(scenario=SCENARIOS)
def test_a_turn_that_asked_to_wait_is_suspended_or_failed_whatever_the_checkpoint_does(
    scenario: Scenario,
) -> None:
    """Every generated world and answer sequence leaves no wait that nothing will end.

    The attempt may be in any phase when the turn ends, charged or not, with a peer writer
    still running; the executor answers the checkpoint any number of times, any way, with a
    retirement between; and every step is replayed. After every step ``orphan_waits`` names
    no yield publication, and at the end the turn is suspended, failed with one named
    reason, or genuinely waiting on a checkpoint that is still in flight.
    """
    run = _world(
        access=scenario.access,
        charged=scenario.charged,
        phase=scenario.phase,
        peer_writer=scenario.peer_writer,
    )
    retire_between = scenario.retire
    answers = scenario.answers
    replay = scenario.replay
    try:
        run.end_turn()
    except core.ContractError:
        return  # a world the attempt's own phase refuses to take a turn in
    owner = run.state.attempts.attempts[0]
    for status, retained, terminal in answers:
        for snapshot in list(run.snapshots):
            run.answer(snapshot, status, retained=retained, terminal=terminal)
        if retire_between is not None and owner.admission_id is not None:
            run.feed(
                core.RetireRequested(
                    attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
                    disposition=retire_between,
                    authority=core.RequestId(root="withdraw"),
                    admission_id=owner.admission_id,
                    requested_at=3.0,
                )
            )
            retire_between = None
    if replay:
        before = run.state
        run.end_turn()
        assert run.state.model_copy(update={"revision": before.revision}) == before
    run.wait_out_retries()
    _assert_resolved(run, answered_checkpoint=bool(answers))


DECLINES = [
    pytest.param(False, False, core.CheckpointDecline.UNCHARGED, id="uncharged"),
    pytest.param(True, True, core.CheckpointDecline.WRITER_ACTIVE, id="writer-active"),
]


@pytest.mark.parametrize(("charged", "peer_writer", "reason"), DECLINES)
def test_a_declined_checkpoint_ends_the_turn_with_the_decline_it_was_given(
    *, charged: bool, peer_writer: bool, reason: core.CheckpointDecline
) -> None:
    """The reported bug: a decline left the yield pending and the run stalled on it."""
    run = _world(charged=charged, peer_writer=peer_writer)
    run.end_turn()
    assert run.snapshots == []
    assert [row.reason for row in run.state.attempts.attempts[0].checkpoint_declines] == [reason]
    assert run.invocation.pending_suspension is None
    assert core.orphan_waits(run.state) == ()
    [failure] = run.failures()
    assert failure.reason == core.CheckpointFailure.CHECKPOINT_DECLINED
    assert failure.decline == reason


def test_a_checkpoint_declined_because_the_attempt_is_no_longer_active_ends_the_turn() -> None:
    """The attempt left ACTIVE after the turn ended but before its checkpoint was requested."""
    run = _world()
    run.end_turn()
    owner = run.state.attempts.attempts[0].model_copy(
        update={"phase": core.AttemptPhase.CLOSING, "pending_intents": ()}
    )
    intents = tuple(
        row
        for row in run.state.intents.intents
        if not isinstance(row.request, core.SnapshotAndRetain)
    )
    run.state = run.state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": run.state.intents.model_copy(update={"intents": intents}),
        }
    )
    ref = run.invocation.invocation
    run.feed(
        core.InvocationCheckpointRequested(
            attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
            invocation=ref,
            retention="wip",
            authority=core.write_turn_authority(ref),
        )
    )
    [failure] = run.failures()
    assert failure.decline == core.CheckpointDecline.NOT_ACTIVE
    assert run.invocation.pending_suspension is None


def test_a_failed_snapshot_ends_the_turn_with_snapshot_failed() -> None:
    run = _world()
    run.end_turn()
    [snapshot] = run.snapshots
    run.answer(snapshot, core.ObservationStatus.FAILED, retained=False)
    [failure] = run.failures()
    assert failure.decline == core.CheckpointDecline.SNAPSHOT_FAILED
    assert run.invocation.pending_suspension is None
    assert core.orphan_waits(run.state) == ()


def test_a_checkpoint_retained_after_the_scope_stopped_cannot_publish_and_says_so() -> None:
    run = _world(access=core.Access.READ_ONLY)
    run.end_turn()
    [snapshot] = run.snapshots
    owner = run.state.attempts.attempts[0].model_copy(update={"phase": core.AttemptPhase.CLOSING})
    run.state = run.state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    run.answer(snapshot, core.ObservationStatus.SUCCEEDED, retained=True)
    [failure] = run.failures()
    assert failure.reason == core.CheckpointFailure.UNPUBLISHABLE
    assert run.invocation.pending_suspension is None
    assert core.orphan_waits(run.state) == ()


def test_a_checkpoint_still_in_flight_keeps_the_turn_waiting() -> None:
    run = _world()
    run.end_turn()
    assert run.failures() == []
    assert run.invocation.pending_suspension is not None
    run.answer(run.snapshots[0], core.ObservationStatus.UNKNOWN, retained=False)
    assert run.failures() == []
    assert run.invocation.pending_suspension is not None


def test_a_retained_checkpoint_still_publishes_the_suspension() -> None:
    run = _world()
    run.end_turn()
    [snapshot] = run.snapshots
    run.answer(snapshot, core.ObservationStatus.SUCCEEDED, retained=True)
    assert run.failures() == []
    assert run.invocation.pending_suspension is None
    assert run.invocation.phase == core.SessionPhase.SUSPENDED
    assert len(run.state.evaluation.continuations) == 1


def test_the_scope_of_a_failed_yield_takes_a_new_suspension() -> None:
    """The run can go on: the failed turn no longer claims the scope's one suspension."""
    run = _world(charged=False)
    run.end_turn()
    assert run.failures()
    assert run.state.sessions.sessions[0].phase == core.SessionPhase.IDLE
    assert run.invocation.pending_suspension is None
    assert core.suspension_refusal(run.state, run.invocation.invocation) is None


def test_a_stall_naming_a_suspension_with_a_producer_changes_nothing() -> None:
    """The internal signal is safe to receive from anywhere: it only ends a turn nothing helps."""
    run = _world()
    run.end_turn()
    before = run.state
    result = core.step(
        before,
        core.CheckpointStalled(invocation=run.invocation.invocation),
        reducers=REDUCERS,
    )
    assert result.state.sessions == before.sessions
    assert result.events == ()


def test_a_checkpoint_answered_retryably_every_time_is_retried_then_fails_with_a_named_reason() -> (
    None
):
    """The reported spin (#2154): a retryable answer left the intent dispatched with no driver.

    Core asks again after a growing delay, at most ``max_retries`` more times, and then the
    checkpoint concludes as failed and the turn ends with the executor's failure named.
    """
    run = _world()
    run.end_turn()
    [first] = run.snapshots
    asks = 0
    for _ in range(40):
        asked = run.outstanding()
        if not asked:
            break
        asks += 1
        run.wait_out_retries(limit=1)
    assert asks == run.state.run.limits.max_retries + 1
    assert core.orphan_waits(run.state) == ()
    assert run.invocation.pending_suspension is None
    [failure] = run.failures()
    assert failure.reason == core.CheckpointFailure.CHECKPOINT_DECLINED
    assert {row.request_id for row in run.snapshots} == {first.request_id}
