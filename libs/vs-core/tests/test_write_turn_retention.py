"""A terminal write turn's revision is retained, through the real public `step`.

Without it no candidate revision ever exists: an implementer edits its workspace, its
turn ends, and nothing snapshots the result, so nothing can be settled, adopted or
chosen as a parent. The same producer serves interruption claims and yields; here the
focus is the plain write turn and every way the request can be declined.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_yield_completion import terminal_yield, yield_state

CHARGE = core.ChargeReceipt(
    charge_id=core.ChargeId(root="write-turn"),
    kind=core.ChargeKind.TURN,
    invocation_id=core.InvocationId(root="yield-turn"),
    charged=1,
)


def write_turn_world(
    *, access: core.Access = core.Access.WRITE_CANDIDATE, charged: bool = True
) -> tuple[core.CoreState, core.DispatchTurn | core.ResumeSessionTurn]:
    """An active attempt whose write turn is executing."""
    state, request, _ = yield_state("dispatch")
    owner = state.attempts.attempts[0]
    session = state.sessions.sessions[0]
    invocation = state.sessions.invocations[0]
    spec = session.spec.model_copy(update={"access": access})
    turn = invocation.turn.model_copy(update={"session": spec})
    owner = owner.model_copy(update={"charges": (CHARGE,) if charged else ()})
    ready = core.RecoveryBarrier(phase=core.RecoveryPhase.READY)
    assert owner.admission_id is not None
    admission = core.StartAttempt(
        decision_id=owner.admission_id,
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=owner.attempt_id,
        item_id=owner.item_id,
        workspace=owner.workspace,
        budget=owner.budget,
    )
    started = core.DecisionReceipt(
        decision_id=admission.decision_id,
        decision=admission,
        payload_digest="accepted-admission",
        feedback=core.Accepted(decision_id=admission.decision_id),
    )
    slot = core.Slot(
        attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
        admission_id=owner.admission_id,
        admitted_at=0.0,
    )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (*state.run.receipts, started)}),
            "scheduling": core.SchedulingState(slots=(slot,)),
            "intents": state.intents.model_copy(update={"recovery": ready}),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(
                sessions=(session.model_copy(update={"spec": spec}),),
                invocations=(invocation.model_copy(update={"turn": turn}),),
            ),
        }
    ), request


def snapshots(result: core.Transition) -> list[core.SnapshotAndRetain]:
    return [row for row in result.requests if isinstance(row, core.SnapshotAndRetain)]


def answer(
    state: core.CoreState,
    request: core.SnapshotAndRetain,
    status: core.ObservationStatus,
    revision: core.RevisionRef | None,
) -> core.Transition:
    """The workspace executor's terminal answer to the snapshot request."""
    assert request.request_id is not None
    dispatched = core.step(state, core.DispatchAuthorized(request_id=request.request_id)).state
    observation = core.Observation(
        event_id=core.EventId(root="snapshot-observed"),
        request_id=request.request_id,
        scope=request.scope,
        admission_id=request.admission_id,
        sequence=1,
        observed_at=2.0,
        status=status,
        accepted=True,
        terminal=True,
        children_complete=True,
        revision=revision,
    )
    return core.step(dispatched, core.RequestObserved(observation=observation, revision=revision))


def test_a_write_turn_that_ends_requests_one_wip_snapshot_of_its_invocation() -> None:
    state, request = write_turn_world()
    ended = core.step(state, terminal_yield(state, request, None))
    [snapshot] = snapshots(ended)
    invocation = state.sessions.invocations[0].invocation
    assert snapshot.invocation == invocation
    assert snapshot.retention == "wip"
    assert snapshot.request_id == core.write_turn_authority(invocation)
    assert snapshot.request_id != request.request_id
    assert snapshot.request_id in ended.state.attempts.attempts[0].pending_intents
    assert ended.state.attempts.attempts[0].checkpoint_declines == ()
    replay = core.step(ended.state, terminal_yield(state, request, None))
    assert snapshots(replay) == []


def test_the_workspace_answer_becomes_the_attempts_checkpoint_the_strategy_can_settle() -> None:
    state, request = write_turn_world()
    ended = core.step(state, terminal_yield(state, request, None))
    [snapshot] = snapshots(ended)
    revision = core.RevisionRef(
        revision_id=core.RevisionId(root="a" * 40), digest="git-commit:" + "a" * 40
    )
    retained = answer(ended.state, snapshot, core.ObservationStatus.SUCCEEDED, revision)
    owner = retained.state.attempts.attempts[0]
    [checkpoint] = owner.checkpoints
    assert checkpoint.invocation == state.sessions.invocations[0].invocation
    assert checkpoint.retention == "wip"
    assert checkpoint.revision == revision
    assert owner.pending_intents == ()
    assert core.project(retained.state).attempts[0].checkpoint == revision
    assert retained.state.sessions.invocations[0].phase == core.SessionPhase.CHECKPOINTED


@given(status=st.sampled_from([core.ObservationStatus.FAILED, core.ObservationStatus.REJECTED]))
def test_a_failed_snapshot_releases_the_attempt_and_records_why(
    status: core.ObservationStatus,
) -> None:
    state, request = write_turn_world()
    ended = core.step(state, terminal_yield(state, request, None))
    [snapshot] = snapshots(ended)
    failed = answer(ended.state, snapshot, status, None)
    owner = failed.state.attempts.attempts[0]
    assert owner.checkpoints == ()
    assert owner.pending_intents == ()
    assert [row.reason for row in owner.checkpoint_declines] == [
        core.CheckpointDecline.SNAPSHOT_FAILED
    ]


@settings(max_examples=60, deadline=None)
@given(
    access=st.sampled_from(list(core.Access)),
    status=st.sampled_from(
        [
            core.ObservationStatus.SUCCEEDED,
            core.ObservationStatus.FAILED,
            core.ObservationStatus.CANCELLED,
        ]
    ),
)
def test_only_a_conclusive_write_turn_requests_a_snapshot(
    access: core.Access, status: core.ObservationStatus
) -> None:
    state, request = write_turn_world(access=access)
    ended = core.step(state, terminal_yield(state, request, None, status))
    wanted = access == core.Access.WRITE_CANDIDATE and status == core.ObservationStatus.SUCCEEDED
    assert len(snapshots(ended)) == int(wanted)
    assert ended.state.attempts.attempts[0].checkpoint_declines == ()


def test_a_write_turn_with_no_charge_in_the_attempt_is_declined_by_name() -> None:
    state, request = write_turn_world(charged=False)
    ended = core.step(state, terminal_yield(state, request, None))
    assert snapshots(ended) == []
    assert [row.reason for row in ended.state.attempts.attempts[0].checkpoint_declines] == [
        core.CheckpointDecline.UNCHARGED
    ]


def test_a_checkpoint_request_nothing_authorizes_is_declined_by_name() -> None:
    state, request = write_turn_world()
    ended = core.step(state, terminal_yield(state, request, None))
    owner = ended.state.attempts.attempts[0]
    forged = core.InvocationCheckpointRequested(
        attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
        invocation=state.sessions.invocations[0].invocation,
        retention="wip",
        authority=core.RequestId(root="made-up"),
    )
    declined = core.step(ended.state, forged)
    assert snapshots(declined) == []
    assert [row.reason for row in declined.state.attempts.attempts[0].checkpoint_declines] == [
        core.CheckpointDecline.UNAUTHORIZED
    ]
    assert core.step(declined.state, forged).state.attempts == declined.state.attempts


type Step = tuple[str, int]

STEPS = st.one_of(
    st.tuples(st.just("end"), st.integers(0, 2)),
    st.tuples(st.just("answer"), st.integers(0, 4)),
    st.tuples(st.just("retire"), st.integers(0, 2)),
    st.tuples(st.just("again"), st.integers(0, 5)),
)

REVISIONS = (
    None,
    core.RevisionRef(revision_id=core.RevisionId(root="b" * 40), digest="git-commit:" + "b" * 40),
)


@settings(max_examples=150, deadline=None)
@given(plan=st.lists(STEPS, min_size=1, max_size=12))
def test_write_turns_never_fail_the_step_or_strand_a_closing_attempt(plan: list[Step]) -> None:
    """Write turns end with or without changes, the workspace answers any way, retire anywhere.

    The turn's outcome varies (a success, a failure, a cancellation); the snapshot answer
    names a new revision, the unchanged one, none, or fails; the attempt may be retired
    before, between or after. No order may raise, and once the attempt has stopped, no
    checkpoint request may still be pending on an answer that already concluded.
    """
    state, request = write_turn_world()
    seen: list[core.CoreEvent] = []
    issued: list[core.SnapshotAndRetain] = []
    dispatched: set[str] = set()
    finished: set[str] = set()

    def feed(event: core.CoreEvent) -> None:
        nonlocal state
        seen.append(event)
        result = core.step(state, event)
        state = result.state
        issued.extend(snapshots(result))

    statuses = (
        core.ObservationStatus.SUCCEEDED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    )
    owner = state.attempts.attempts[0]
    target = core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
    for kind, index in plan:
        if kind == "end":
            feed(terminal_yield(state, request, None, statuses[index]))
        elif kind == "answer" and issued:
            snapshot = issued[index % len(issued)]
            assert snapshot.request_id is not None
            if snapshot.request_id.root in finished:
                continue  # a terminal request never changes its disposition
            answered = {
                0: (core.ObservationStatus.SUCCEEDED, REVISIONS[1]),
                1: (core.ObservationStatus.SUCCEEDED, state.run.facts.baseline),
                2: (core.ObservationStatus.SUCCEEDED, None),
                3: (core.ObservationStatus.FAILED, None),
                4: (core.ObservationStatus.UNKNOWN, None),
            }[index]
            if snapshot.request_id.root not in dispatched:
                dispatched.add(snapshot.request_id.root)
                state = core.step(
                    state, core.DispatchAuthorized(request_id=snapshot.request_id)
                ).state
            if answered[0] != core.ObservationStatus.UNKNOWN:
                finished.add(snapshot.request_id.root)
            feed(
                core.RequestObserved(
                    observation=core.Observation(
                        event_id=core.EventId(root=f"answer-{len(seen)}"),
                        request_id=snapshot.request_id,
                        scope=snapshot.scope,
                        admission_id=snapshot.admission_id,
                        sequence=len(seen) + 1,
                        observed_at=2.0,
                        status=answered[0],
                        accepted=True,
                        terminal=answered[0] != core.ObservationStatus.UNKNOWN,
                        children_complete=True,
                        revision=answered[1],
                    ),
                    revision=answered[1],
                )
            )
        elif kind == "retire":
            feed(
                core.RetireRequested(
                    attempt=target,
                    disposition=("cancel", "park", "settle")[index],
                    authority=core.RequestId(root="withdraw"),
                    admission_id=owner.admission_id,
                    requested_at=3.0,
                )
            )
        elif kind == "again" and seen:
            feed(seen[index % len(seen)])
    attempt = state.attempts.attempts[0]
    concluded = {
        intent.request_id
        for intent in state.intents.intents
        if intent.observation is not None
        and intent.observation.terminal
        and intent.observation.status != core.ObservationStatus.UNKNOWN
    }
    assert not set(attempt.pending_intents) & concluded
    assert len({row.request_id for row in attempt.checkpoints}) == len(attempt.checkpoints)


@given(status=st.sampled_from([core.ObservationStatus.SUCCEEDED, core.ObservationStatus.FAILED]))
def test_a_closing_attempt_stops_waiting_on_its_invocation_snapshot(
    status: core.ObservationStatus,
) -> None:
    """The closure's release edge on the snapshot ends with any conclusive answer.

    The edge used to wait for the closure's own retention, which an invocation snapshot
    never is, so a retired attempt with a snapshot in flight never released.
    """
    state, request = write_turn_world()
    owner = state.attempts.attempts[0]
    ended = core.step(state, terminal_yield(state, request, None))
    [snapshot] = snapshots(ended)
    retired = core.step(
        ended.state,
        core.RetireRequested(
            attempt=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
            disposition="cancel",
            authority=core.RequestId(root="withdraw"),
            admission_id=owner.admission_id,
            requested_at=3.0,
        ),
    )
    assert retired.state.attempts.attempts[0].phase == core.AttemptPhase.CLOSING
    edge = core.ReleaseDependency(kind="request", identity=snapshot.request_id)
    assert edge in retired.state.attempts.attempts[0].release_dependencies
    revision = REVISIONS[1] if status == core.ObservationStatus.SUCCEEDED else None
    after = answer(retired.state, snapshot, status, revision).state.attempts.attempts[0]
    assert edge not in after.release_dependencies
    assert after.pending_intents == ()
