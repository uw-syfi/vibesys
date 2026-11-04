"""A logical turn's permit is the only admission of its successors, and their deadline."""

import enum
import hashlib
import json
from collections.abc import Mapping, Sequence

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_agent.api import AgentExecutionCancelledError
from vs_core.api.testing import sample_limits
from vs_runtime.api.infrastructure import create_run_control_channel
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads, examples

from .test_measurements import plan
from .test_run_park import park
from .test_write_turn_retention import write_turn_world

_DEADLINE = 100.0


def _at(state: core.CoreState, now_at: float) -> core.CoreState:
    if now_at == state.run.now_at:
        return state
    return core.step(state, core.ClockAdvanced(now_at=now_at)).state


def _deadline(state: core.CoreState, request_id: core.RequestId) -> float:
    record = state.records.record(request_id)
    assert record is not None
    return record.until.at


@pytest.mark.regression
def test_a_drain_evaluation_keeps_the_turns_deadline() -> None:
    """Pause at 99 of a turn due at 100 must not accept an evaluation due at 199."""
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    state = _at(state, 99.0)
    draining = park(state)
    result = core.step(
        draining.state,
        core.AgentMeasurementRequested(
            scope=turn.scope,
            plan=plan(),
            within=core.Within(limit=100.0),
            call_id="late",
        ),
    )
    submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
    assert len(submissions) == 1
    request_id = submissions[0].request_id
    assert request_id is not None
    assert _deadline(result.state, request_id) <= _DEADLINE
    answered = result.state.evaluation.agent_calls[-1]
    assert answered.deadline_at is not None
    assert answered.deadline_at <= _DEADLINE


@pytest.mark.regression
def test_an_evaluation_queued_before_pause_keeps_the_turns_deadline() -> None:
    """A submission requested while the turn is running cannot outlive that turn."""
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    wide = plan(queue_allowance=400.0)
    result = core.step(
        state,
        core.AgentMeasurementRequested(
            scope=turn.scope,
            plan=wide,
            within=core.Within(limit=400.0),
            call_id="queued",
        ),
    )
    submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
    assert len(submissions) == 1
    request_id = submissions[0].request_id
    assert request_id is not None
    assert _deadline(result.state, request_id) <= _DEADLINE
    paused = park(result.state)
    assert _deadline(paused.state, request_id) <= _DEADLINE


@pytest.mark.regression
@example(pause_at=99, limits=(100,), continuations=1)
@example(pause_at=100, limits=(100,), continuations=1)
@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(
    pause_at=st.integers(min_value=0, max_value=100),
    limits=st.lists(st.integers(min_value=1, max_value=400), max_size=3),
    continuations=st.integers(min_value=0, max_value=3),
)
def test_admitted_successors_start_inside_the_turn_deadline(
    pause_at: int, limits: list[int], continuations: int
) -> None:
    """Pause at any point, then any continuations and evaluations.

    Every successor core admits starts, and none of that work is due after the turn.
    """
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    state = _at(state, float(pause_at))
    draining = park(state).state
    permit = core.permit_for_request(draining, turn)
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    channel.land_park()
    if permit is None:
        channel.prepare_execution("blocked", turn.scope)
        with pytest.raises(AgentExecutionCancelledError):
            channel.start_execution(lambda: None, execution_id="blocked")
    else:
        assert permit.deadline_at == _DEADLINE
        for index in range(continuations):
            execution_id = f"continuation-{index}"
            channel.prepare_execution(execution_id, turn.scope, permit=permit)
            started: list[str] = []
            channel.start_execution(
                lambda execution_id=execution_id, started=started: started.append(execution_id),
                execution_id=execution_id,
            )
            assert started == [execution_id]
            channel.finish_execution(execution_id)
    for index, limit in enumerate(limits):
        call_id = f"call-{pause_at}-{index}"
        result = core.step(
            draining,
            core.AgentMeasurementRequested(
                scope=turn.scope,
                plan=plan(queue_allowance=float(limit)),
                within=core.Within(limit=float(limit)),
                call_id=call_id,
            ),
        )
        draining = result.state
        submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
        if not submissions:
            continue
        assert permit is not None
        request_id = submissions[0].request_id
        assert request_id is not None
        assert _deadline(draining, request_id) <= permit.deadline_at
        answered = next(call for call in draining.evaluation.agent_calls if call.call_id == call_id)
        assert answered.deadline_at is not None
        assert answered.deadline_at <= permit.deadline_at
        channel.prepare_evaluation(request_id.root, turn.scope, permit=permit)
        channel.start_execution(lambda: None, evaluation_id=request_id.root)


class _Successor(enum.StrEnum):
    """A provider turn that follows an admitted one, and the hold it may meet."""

    SUSPENDED_RESUME = "suspended-resume"
    EVALUATION_WAIT = "evaluation-wait"
    CORRECTION = "correction"
    HOST_RESTART = "host-restart"
    QUOTA_PAUSE = "quota-pause"
    OPERATOR_PAUSE = "operator-pause"


_FOLLOWS = (
    _Successor.SUSPENDED_RESUME,
    _Successor.EVALUATION_WAIT,
    _Successor.CORRECTION,
    _Successor.HOST_RESTART,
)
"""Successors a running run admits, each with the deadline it was asked with."""


def _ended(
    request_id: core.RequestId, scope: core.Scope, status: core.ObservationStatus
) -> core.Observation:
    """A terminal observation, so a correction may follow the turn."""
    return core.Observation(
        event_id=core.EventId(root=f"ended-{status.value}"),
        request_id=request_id,
        scope=scope,
        sequence=1,
        observed_at=1.0,
        status=status,
        terminal=True,
        resource_id=core.ResourceId(root="conversation"),
    )


def _successor_world(kind: _Successor) -> tuple[core.CoreState, core.RequestTurn]:
    """One successor of an admitted turn whose own deadline is ``_DEADLINE``.

    A resume continues a suspended turn. A correction follows a terminal one:
    a failed reply, or the cancel a host restart leaves (``EPOCH_INTERRUPTED``).
    """
    state, turn = write_turn_world(access=core.Access.READ_ONLY)
    invocation = state.sessions.invocations[0]
    session = state.sessions.sessions[0]
    follow = kind in (_Successor.QUOTA_PAUSE, _Successor.OPERATOR_PAUSE)
    resume = follow or kind in (_Successor.SUSPENDED_RESUME, _Successor.EVALUATION_WAIT)
    if resume:
        resume_id = core.InvocationId(root="resume")
        continuation_id = core.ContinuationId(root="wait")
        next_ref = invocation.invocation.model_copy(update={"invocation_id": resume_id})
        spec = invocation.turn.model_copy(
            update={
                "invocation_id": resume_id,
                "continuation_id": continuation_id,
                "charge_class": "resume",
                "predecessor": None,
            }
        )
        state = state.model_copy(
            update={
                "sessions": state.sessions.model_copy(
                    update={
                        "sessions": (
                            session.model_copy(update={"phase": core.SessionPhase.SUSPENDED}),
                        ),
                        "invocations": (
                            invocation.model_copy(update={"phase": core.SessionPhase.SUSPENDED}),
                        ),
                    }
                ),
                "evaluation": core.EvaluationState(
                    continuations=(
                        core.Continuation(
                            continuation_id=continuation_id,
                            invocation=invocation.invocation,
                            next_invocation=next_ref,
                            jobs=(
                                (core.ResourceId(root="measurement"),)
                                if kind is _Successor.EVALUATION_WAIT
                                else ()
                            ),
                            deadline_at=80.0,
                            phase=core.ContinuationPhase.AUTHORIZED,
                        ),
                    )
                ),
            }
        )
    else:
        status = (
            core.ObservationStatus.CANCELLED
            if kind is _Successor.HOST_RESTART
            else core.ObservationStatus.FAILED
        )
        dispatch = next(
            row.request_id
            for row in state.records.records
            if isinstance(row.request, core.DispatchTurn)
        )
        again = core.InvocationId(root="again")
        # The attempt already paid for this ask, so admission dispatches it in the same step
        # instead of leaving it to wait for a charge that this fixture never grants.
        paid = core.ChargeReceipt(
            charge_id=core.ChargeId(root="again"),
            kind=core.ChargeKind.TURN,
            invocation_id=again,
            charged=1,
        )
        owner = state.attempts.attempts[0]
        spec = invocation.turn.model_copy(
            update={
                "invocation_id": again,
                "charge_class": "correction",
                "predecessor": invocation.invocation,
                "continuation_id": None,
            }
        )
        state = state.model_copy(
            update={
                "attempts": core.AttemptsState(
                    attempts=(owner.model_copy(update={"charges": (*owner.charges, paid)}),)
                ),
                "sessions": state.sessions.model_copy(
                    update={
                        "sessions": (session.model_copy(update={"phase": core.SessionPhase.IDLE}),),
                        "invocations": (
                            invocation.model_copy(
                                update={
                                    "phase": core.SessionPhase.TERMINAL,
                                    "observation": _ended(dispatch, turn.scope, status),
                                }
                            ),
                        ),
                    }
                ),
            }
        )
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"limits": sample_limits(max_turns=10)})}
    )
    decision = core.RequestTurn(
        decision_id=core.DecisionId(root=kind.value), scope=turn.scope, turn=spec
    )
    return state, decision


def _submit(state: core.CoreState, decision: core.RequestTurn, limit: float) -> core.Transition:
    return core.step(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=limit)),
            expected_revision=state.revision,
        ),
    )


def _hold(state: core.CoreState, owner: core.PauseOwner) -> core.CoreState:
    """Pause at the run's current time. A quota pause sets no park; an operator pause does."""
    return core.step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(
                control_id=core.ControlId(root=owner.value),
                action="pause",
                owner=owner,
            ),
            now_at=state.run.now_at,
        ),
    ).state


def _turn_limit(state: core.CoreState, before: set[core.RequestId]) -> float | None:
    limits = [
        row.limit_from_dispatch
        for row in state.records.records
        if row.request_id not in before
        and isinstance(row.request, core.DispatchTurn | core.ResumeSessionTurn)
        and row.limit_from_dispatch is not None
    ]
    return None if not limits else limits[0]


def _ids(state: core.CoreState) -> set[core.RequestId]:
    return {row.request_id for row in state.records.records}


def _admits_its_own_deadline(
    state: core.CoreState, decision: core.RequestTurn, limit: float
) -> None:
    """``decision`` starts, the run stays running, and the turn keeps ``limit``."""
    before = _ids(state)
    result = _submit(state, decision, limit)
    assert state.run.park is None
    assert state.run.status is core.RunStatus.RUNNING
    assert any(isinstance(event, core.Accepted) for event in result.events)
    assert result.state.run.park is None
    assert result.state.run.status is core.RunStatus.RUNNING
    assert _turn_limit(result.state, before) == limit
    finish = core.Finish(
        decision_id=core.DecisionId(root="finish"),
        scope=decision.scope,
        mode="drain",
        result=core.RunResultProposal(outcome="cancelled", reason="done"),
    )
    closed = core.step(
        result.state,
        core.DecisionSubmitted(decision=finish, expected_revision=result.state.revision),
    )
    assert not any(
        isinstance(event, core.Rejected) and event.code == core.RejectionCode.CLOSED_SCOPE
        for event in closed.events
    )
    assert closed.state.run.status is not core.RunStatus.DRAINING
    assert closed.state.run.park is None


@pytest.mark.regression
@example(kind=_Successor.EVALUATION_WAIT, now_at=0, limit=50)
@example(kind=_Successor.EVALUATION_WAIT, now_at=99, limit=100)
@example(kind=_Successor.CORRECTION, now_at=100, limit=400)
@example(kind=_Successor.HOST_RESTART, now_at=100, limit=400)
@example(kind=_Successor.SUSPENDED_RESUME, now_at=99, limit=400)
@example(kind=_Successor.QUOTA_PAUSE, now_at=99, limit=400)
@example(kind=_Successor.OPERATOR_PAUSE, now_at=99, limit=400)
@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(
    kind=st.sampled_from(tuple(_Successor)),
    now_at=st.integers(min_value=0, max_value=100),
    limit=st.integers(min_value=1, max_value=400),
)
def test_without_a_pause_a_successor_is_admitted_on_the_running_run(
    kind: _Successor, now_at: int, limit: int
) -> None:
    """Each successor is admitted as main admits it, and only an operator Pause caps it.

    A running turn's permit used to open the drain, and then to cap a later provider
    turn at the ancestor deadline. A resume after a quota outage was asked again with
    its own turn budget; the cap made that ask due immediately and cancelled it.
    A quota pause holds the run and admits nothing new until it lifts. An operator
    Pause admits the successor into the drain and caps it at the admitted turn.
    """
    asked = float(limit)
    at = float(now_at)
    state, decision = _successor_world(kind)
    state = _at(state, at)
    if kind in _FOLLOWS:
        _admits_its_own_deadline(state, decision, asked)
        return
    if kind is _Successor.QUOTA_PAUSE:
        held = _hold(state, core.PauseOwner.QUOTA)
        assert held.run.park is None
        assert held.run.status is core.RunStatus.PAUSED
        refused = _submit(held, decision, asked)
        assert any(
            isinstance(event, core.Rejected) and event.code == core.RejectionCode.CLOSED_SCOPE
            for event in refused.events
        )
        assert refused.state.run.status is core.RunStatus.PAUSED
        assert refused.state.run.park is None
        resumed = core.step(
            refused.state,
            core.RunControlEvent(
                control=core.ControlInput(
                    control_id=core.ControlId(root="lift"), action="resume", when="now"
                ),
                now_at=refused.state.run.now_at,
            ),
        ).state
        again = decision.model_copy(update={"decision_id": core.DecisionId(root="after-quota")})
        _admits_its_own_deadline(resumed, again, asked)
        return
    draining = _hold(state, core.PauseOwner.OPERATOR)
    assert draining.run.park is not None
    assert not draining.run.park.immediate
    result = _submit(draining, decision, asked)
    if at >= _DEADLINE:
        assert any(
            isinstance(event, core.Rejected) and event.code == core.RejectionCode.CLOSED_SCOPE
            for event in result.events
        )
        assert result.state.run.status is core.RunStatus.DRAINING
        return
    assert any(isinstance(event, core.Accepted) for event in result.events)
    assert result.state.run.status is core.RunStatus.DRAINING
    remaining = _DEADLINE - at
    assert _turn_limit(result.state, _ids(draining)) == min(asked, remaining)


_RUN_END = 10_000_000.0
_WIDE = 50_000.0


def _credited_turn(credit: float, now_at: float) -> tuple[core.CoreState, core.RequestTurn, float]:
    """A suspended resume whose ancestor holds ``credit`` seconds, at ``now_at``.

    The run deadline sits past any credit this property draws, so the turn bound is the
    stated limit plus that credit and not the run's end. The hold is attached before the
    clock passes the stated limit, which would otherwise block the turn with no credit.
    """
    state, decision = _successor_world(_Successor.SUSPENDED_RESUME)
    continuations = tuple(
        row.model_copy(update={"deadline_at": _RUN_END}) for row in state.evaluation.continuations
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"deadline_at": _RUN_END}),
            "evaluation": state.evaluation.model_copy(update={"continuations": continuations}),
        }
    )
    invocation = state.sessions.invocations[0]
    held = core.step(
        state,
        core.TurnHeld(
            invocation_id=invocation.invocation.invocation_id,
            hold=core.CapacityHold(credit=credit, budget=credit),
        ),
    ).state
    record = next(
        row
        for row in held.records.records
        if isinstance(row.request, core.DispatchTurn | core.ResumeSessionTurn)
    )
    assert record.capacity_hold is not None
    assert record.capacity_hold.credit == credit
    credited = record.until.at + credit
    assert credited <= _RUN_END
    return _at(held, now_at), decision, credited


@pytest.mark.regression
@example(credit=10804, now_at=102, paused=False)
@example(credit=10804, now_at=102, paused=True)
@example(credit=0, now_at=50, paused=False)
@example(credit=0, now_at=50, paused=True)
@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(
    credit=st.integers(min_value=0, max_value=20_000),
    now_at=st.integers(min_value=0, max_value=200),
    paused=st.booleans(),
)
def test_hold_credit_is_the_turn_bound_and_pause_is_the_only_cap(
    credit: int, now_at: int, *, paused: bool
) -> None:
    """A successor keeps one deadline: the turn bound, which includes hold credit.

    The permit used to read the invocation's stated until. A hold credited to end at
    10904 (stated 100, credit 10804) was already over at time 102, so the successor
    was cut to 2 seconds past that until and lost as transport instead of running out
    its credit. Without a Pause the successor keeps the deadline it was asked with,
    which is never earlier than that credited bound. A non-immediate Pause of this
    epoch caps it at the bound and no later.
    """
    assume(now_at < _DEADLINE + credit)
    at = float(now_at)
    state, decision, credited = _credited_turn(float(credit), at)
    # The ask extends past the credited bound, so a missing cap and a short cap both show.
    assert at + _WIDE >= credited
    if paused:
        state = _hold(state, core.PauseOwner.OPERATOR)
        assert state.run.park is not None
        assert not state.run.park.immediate
    before = _ids(state)
    result = _submit(state, decision, _WIDE)
    assert any(isinstance(event, core.Accepted) for event in result.events), result.events
    limit = _turn_limit(result.state, before)
    assert limit is not None
    end = at + limit
    if paused:
        assert end <= credited
        assert end == min(at + _WIDE, credited)
        assert result.state.run.status is core.RunStatus.DRAINING
        return
    assert end >= credited
    assert end == at + _WIDE
    assert result.state.run.status is core.RunStatus.RUNNING
    assert result.state.run.park is None


class _Hold(enum.StrEnum):
    """Whether the run is draining under an operator Pause, held for quota, or running."""

    NONE = "none"
    OPERATOR = "operator"
    QUOTA = "quota"


class _When(enum.StrEnum):
    """Where an invocation's deadline sits relative to now and to a later turn."""

    PAST = "past"
    SOON = "soon"
    LATER = "later"


_NOW = 100.0
_AT = {_When.PAST: 40.0, _When.SOON: 200.0, _When.LATER: 400.0}
_ASKED = 500.0
_Other = tuple[_When, core.SessionPhase, bool]
"""One other invocation: when it is due, its phase, and whether it is the ancestor."""


def _digest(request: core.DispatchTurn) -> str:
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _member(
    invocation: core.Invocation,
    record: core.RequestRecord,
    target: tuple[float, core.SessionPhase, core.InvocationRef | None, str | None],
) -> tuple[core.Invocation, core.RequestRecord, core.InvocationRef]:
    """``invocation`` and its dispatch, retargeted at ``target``'s deadline, phase and ancestor."""
    deadline, phase, predecessor, identity = target
    ref = invocation.invocation
    request = record.request
    assert isinstance(request, core.DispatchTurn)
    spec = invocation.turn.model_copy(update={"predecessor": predecessor})
    if identity is not None:
        ref = ref.model_copy(update={"invocation_id": core.InvocationId(root=identity)})
        spec = spec.model_copy(update={"invocation_id": ref.invocation_id})
        request = request.model_copy(
            update={
                "request_id": core.RequestId(root=f"dispatch-{identity}"),
                "decision_id": core.DecisionId(root=f"decision-{identity}"),
                "turn": spec,
            }
        )
    else:
        request = request.model_copy(update={"turn": spec})
    until = core.Until(at=deadline)
    updated = invocation.model_copy(
        update={"invocation": ref, "turn": spec, "phase": phase, "until": until}
    )
    stored = record.model_copy(
        update={
            "request_id": request.request_id,
            "request": request,
            "until": until,
            "payload_digest": _digest(request),
        }
    )
    return updated, stored, ref


_Deadlines = Mapping[core.InvocationRef, float]
_Predecessors = Mapping[core.InvocationRef, core.InvocationRef | None]


def _measurement_world(
    requester_when: _When,
    others: Sequence[_Other],
    hold: _Hold,
    *,
    requester_captured: bool,
) -> tuple[core.CoreState, core.Scope, core.InvocationRef | None, _Deadlines, _Predecessors]:
    """A scope whose executing turn asks for a measurement, plus any other invocations.

    ``requester_captured`` matters for an operator Pause: when it is false the drain
    snapshot admits the others and not the turn that then asks.
    """
    state, _turn = write_turn_world(access=core.Access.READ_ONLY)
    base = state.sessions.invocations[0]
    record = next(
        row
        for row in state.records.records
        if isinstance(row.request, core.DispatchTurn | core.ResumeSessionTurn)
    )
    ancestor_at = next((index for index, other in enumerate(others) if other[2]), None)
    other_refs: list[core.InvocationRef] = []
    invocations: list[core.Invocation] = []
    records: list[core.RequestRecord] = []
    deadlines: dict[core.InvocationRef, float] = {}
    predecessor: dict[core.InvocationRef, core.InvocationRef | None] = {}
    for index, (when, phase, _ancestor) in enumerate(others):
        deadline = _AT[when]
        invocation, stored, ref = _member(base, record, (deadline, phase, None, f"other-{index}"))
        other_refs.append(ref)
        invocations.append(invocation)
        records.append(stored)
        deadlines[ref] = deadline
        predecessor[ref] = None
    ancestor = None if ancestor_at is None else other_refs[ancestor_at]
    requester_deadline = _AT[requester_when]
    requester, requester_record, requester_ref = _member(
        base, record, (requester_deadline, core.SessionPhase.EXECUTING, ancestor, None)
    )
    deadlines[requester_ref] = requester_deadline
    predecessor[requester_ref] = ancestor
    executing = [
        ref
        for ref, row in ((requester_ref, requester), *zip(other_refs, invocations, strict=True))
        if row.phase == core.SessionPhase.EXECUTING
    ]
    asking = executing[0] if len(executing) == 1 else None
    session = state.sessions.sessions[0].model_copy(
        update={"invocation": requester.turn.invocation_id}
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"now_at": _NOW}),
            "sessions": state.sessions.model_copy(
                update={"sessions": (session,), "invocations": (requester, *invocations)}
            ),
            "records": state.records.model_copy(update={"records": (requester_record, *records)}),
        }
    )
    if hold is _Hold.OPERATOR:
        state = _hold(state, core.PauseOwner.OPERATOR)
        park = state.run.park
        assert park is not None
        assert not park.immediate
        if not requester_captured:
            admitted = tuple(ref for ref in park.admitted_turns if ref != requester_ref)
            state = state.model_copy(
                update={
                    "run": state.run.model_copy(
                        update={"park": park.model_copy(update={"admitted_turns": admitted})}
                    )
                }
            )
    elif hold is _Hold.QUOTA:
        state = _hold(state, core.PauseOwner.QUOTA)
        assert state.run.park is None
    return state, requester.scope, asking, deadlines, predecessor


def _selected(
    start: core.InvocationRef | None,
    links: tuple[_Predecessors, _Deadlines],
    *,
    drain: bool,
    admitted: set[core.InvocationRef],
    live: bool,
) -> float | None:
    """The permit bound the approved rule names, or None when the turn holds none."""
    predecessor, deadlines = links
    seen: set[core.InvocationRef] = set()
    ref = start
    while ref is not None and ref not in seen:
        seen.add(ref)
        holds = ref in admitted if drain else True
        if holds and (not live or deadlines[ref] > _NOW):
            return deadlines[ref]
        if not drain:
            return None
        ref = predecessor[ref]
    return None


def _expected_until(
    live: float | None, hard: float | None, *, limit: float, run_deadline: float
) -> float:
    """The absolute until a positive ask gets after the live cap and the recorded cap."""
    if live is not None:
        limit = min(limit, max(live - _NOW, 0.0))
    at = _NOW + limit
    if run_deadline > _NOW:
        at = min(at, run_deadline)
    if hard is not None:
        at = min(at, hard)
    return at


@pytest.mark.regression
@example(
    requester_when=_When.LATER,
    others=((_When.PAST, core.SessionPhase.SUSPENDED, False),),
    hold=_Hold.NONE,
    requester_captured=True,
)
@example(
    requester_when=_When.LATER,
    others=((_When.PAST, core.SessionPhase.SUSPENDED, True),),
    hold=_Hold.NONE,
    requester_captured=True,
)
@example(
    requester_when=_When.LATER,
    others=((_When.SOON, core.SessionPhase.SUSPENDED, True),),
    hold=_Hold.OPERATOR,
    requester_captured=True,
)
@example(
    requester_when=_When.LATER,
    others=((_When.SOON, core.SessionPhase.SUSPENDED, True),),
    hold=_Hold.OPERATOR,
    requester_captured=False,
)
@example(
    requester_when=_When.LATER,
    others=((_When.PAST, core.SessionPhase.SUSPENDED, False),),
    hold=_Hold.QUOTA,
    requester_captured=True,
)
@example(
    requester_when=_When.LATER,
    others=((_When.PAST, core.SessionPhase.EXECUTING, False),),
    hold=_Hold.NONE,
    requester_captured=True,
)
@settings(max_examples=examples(pr=8, nightly=40), deadline=None)
@given(
    requester_when=st.sampled_from(tuple(_When)),
    others=st.lists(
        st.tuples(
            st.sampled_from(tuple(_When)),
            st.sampled_from((core.SessionPhase.EXECUTING, core.SessionPhase.SUSPENDED)),
            st.booleans(),
        ),
        max_size=2,
    ),
    hold=st.sampled_from(tuple(_Hold)),
    requester_captured=st.booleans(),
)
def test_a_measurement_is_capped_at_the_requesting_turns_bound(
    requester_when: _When,
    others: list[_Other],
    hold: _Hold,
    *,
    requester_captured: bool,
) -> None:
    """A measurement takes the asking turn's own bound, not the earliest one in the scope.

    The scope may also hold suspended or executing invocations whose deadlines are
    already past or still ahead. An earlier deadline of one of those applies only
    when an operator Pause of this epoch is draining and that invocation is the
    ancestor the drain selects. A quota pause is not that drain. With no single
    executing turn, nothing in the scope is asked to stand in.
    """
    state, scope, asking, deadlines, predecessor = _measurement_world(
        requester_when, others, hold, requester_captured=requester_captured
    )
    drain = hold is _Hold.OPERATOR
    admitted = set(state.run.park.admitted_turns) if state.run.park is not None else set()
    links = (predecessor, deadlines)
    live = _selected(asking, links, drain=drain, admitted=admitted, live=True)
    hard = _selected(asking, links, drain=drain, admitted=admitted, live=False)
    measurement = plan(queue_allowance=_ASKED)
    result = core.step(
        state,
        core.AgentMeasurementRequested(
            scope=scope,
            plan=measurement,
            within=core.Within(limit=_ASKED),
            call_id="measure",
        ),
    )
    submissions = [row for row in result.requests if isinstance(row, core.SubmitMeasurement)]
    if drain and live is None:
        assert submissions == [], (live, hard, result.events)
        return
    assert len(submissions) == 1, (live, hard, result.events)
    request_id = submissions[0].request_id
    assert request_id is not None
    expected = _expected_until(
        live, hard, limit=min(_ASKED, measurement.time_bound), run_deadline=state.run.deadline_at
    )
    assert _deadline(result.state, request_id) == expected, (
        _deadline(result.state, request_id),
        expected,
        hold,
        requester_when,
        others,
        requester_captured,
    )
