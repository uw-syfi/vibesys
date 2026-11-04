"""A steer reaches the target it names, or is refused with the reason; never someone else.

The run resolves a steer's target against core state when it takes the steer: a role the search
declares, an attempt core holds that has not ended, or an item core holds. The steer control
core records carries the resolved target, and so does the session input that delivers it, so
core hands the message to a matching turn only. A target the run cannot resolve then is
refused: an inbox request is set aside with the reason, and a socket steer is reported
dropped. A socket steer is answered before the run takes it, against a state that may be a
poll behind, so its ack refuses only what no later state accepts: an undeclared role or an
ended attempt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.session_world import SESSION, turn_spec

from vs_core.api import (
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptView,
    CoreState,
    EventId,
    InputDelivered,
    InputDropped,
    InputDropReason,
    InputId,
    InputTarget,
    Invocation,
    InvocationId,
    InvocationRef,
    ItemId,
    ItemInputTarget,
    Observation,
    ObservationStatus,
    RequestId,
    RoleId,
    RoleInputTarget,
    RunControlEvent,
    RunId,
    Scope,
    ScopeInputTarget,
    SessionInputReceived,
    SessionPhase,
    Until,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    step,
)
from vs_project.api import ProjectStateError
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    ControlAction,
    ControlStatus,
    RefusalReason,
    RunControlTransitionKind,
    SteerDelivery,
    SteerDeliveryState,
    SteerMode,
    SteerRequest,
    SteerTarget,
    SteerTargetKind,
    WithdrawRequest,
    create_run_control_channel,
    known_steer_targets,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable

_DECLARED = ("fake-planner", "fake-worker")
_ATTEMPTS = {
    "live": AttemptPhase.ACTIVE,
    "parked": AttemptPhase.PARKED,
    "done": AttemptPhase.TERMINAL,
}


@dataclass
class _Texts:
    held: dict[str, str] = field(default_factory=dict)

    def put(self, ref: ArtifactRef, text: str) -> None:
        self.held[ref.artifact_id.root] = text


@dataclass
class _Run:
    bridge: RunControlBridge
    control: ChannelRunControl
    inbox: FakeControlInbox
    sink: FakeRunControlEventSink


def _run(inbox: FakeControlInbox | None = None) -> _Run:
    threads = SimThreads()
    sink = FakeRunControlEventSink()
    channel = create_run_control_channel(sink, threads=threads)
    control = ChannelRunControl(channel, threads)
    inbox = inbox or FakeControlInbox()
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    return _Run(bridge, control, inbox, sink)


def _attempt(name: str, phase: AttemptPhase, base: CoreState) -> AttemptView:
    return AttemptView(
        attempt_id=AttemptId(root=name),
        generation=1,
        item_id=ItemId(root=f"item-{name}"),
        phase=phase,
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=base.run.facts.baseline),
        budget=AttemptBudget(),
    )


def _core(attempts: dict[str, AttemptPhase] = _ATTEMPTS) -> CoreState:
    base = initial_state()
    declaration = base.run.declaration.model_copy(
        update={"roles": tuple(RoleId(root=role) for role in _DECLARED)}
    )
    return base.model_copy(
        update={
            "run": base.run.model_copy(update={"declaration": declaration}),
            "attempts": base.attempts.model_copy(
                update={
                    "attempts": tuple(
                        _attempt(name, phase, base) for name, phase in attempts.items()
                    )
                }
            ),
        }
    )


def _expected(target: SteerTarget) -> InputTarget | None | str:
    """What the run must make of ``target``: core's input target, or a refusal (a str)."""
    if target.id is None:
        return None
    name = target.id
    live_attempt = _ATTEMPTS.get(name) not in (None, AttemptPhase.TERMINAL)
    oracle: dict[SteerTargetKind, tuple[bool, Callable[[], InputTarget]]] = {
        SteerTargetKind.ROLE: (
            name in _DECLARED,
            lambda: RoleInputTarget(role_id=RoleId(root=name)),
        ),
        SteerTargetKind.ATTEMPT: (
            live_attempt,
            lambda: ScopeInputTarget(scope=Scope(owner=AttemptId(root=name), generation=1)),
        ),
        SteerTargetKind.ITEM: (
            name in {f"item-{attempt}" for attempt in _ATTEMPTS},
            lambda: ItemInputTarget(item_id=ItemId(root=name)),
        ),
    }
    resolves, resolved = oracle[target.kind]
    return resolved() if resolves else target.kind.value


_IDS = {
    SteerTargetKind.ROLE: (*_DECLARED, "fake-judge"),
    SteerTargetKind.ATTEMPT: (*_ATTEMPTS, "unknown"),
    SteerTargetKind.ITEM: (*(f"item-{name}" for name in _ATTEMPTS), "item-unknown"),
}
_TARGETS = st.one_of(
    st.just(SteerTarget()),
    *(
        st.builds(SteerTarget, kind=st.just(kind), id=st.sampled_from(ids))
        for kind, ids in _IDS.items()
    ),
)


def _payload(control_id: str, text: str, target: SteerTarget) -> bytes:
    request = SteerRequest(control_id=control_id, text=text, target=target)
    return request.model_dump_json().encode()


@given(target=_TARGETS, other=_TARGETS)
def test_an_inbox_steer_is_recorded_for_its_target_or_set_aside(
    target: SteerTarget, other: SteerTarget
) -> None:
    core = _core()
    run = _run()
    run.inbox.post("r1", _payload("r1", "prefer latency", target), ended=lambda: None)

    events = run.bridge.poll(core, 1.0)

    expected = _expected(target)
    if isinstance(expected, str):
        assert events == ()
        (rejected,) = run.inbox.rejected()
        assert expected in rejected.reason
        return
    control, delivery = events
    assert isinstance(control, RunControlEvent)
    assert isinstance(delivery, SessionInputReceived)
    assert control.control.target == expected
    run_scope = ScopeInputTarget(scope=Scope(owner=core.run.run_id, generation=0))
    assert delivery.input.target == (expected or run_scope)
    for event in events:
        core = step(core, event).state

    # The same id again is the same request only if it names the same text and target.
    run.bridge.poll(core, 2.0)
    if (other.kind, other.id) != (target.kind, target.id):
        with pytest.raises(ProjectStateError, match="different request"):
            run.inbox.post("r1", _payload("r1", "prefer latency", other), ended=lambda: None)
        return
    run.inbox.post("r1", _payload("r1", "prefer latency", other), ended=lambda: None)
    assert _run(run.inbox).bridge.poll(core, 3.0) == ()
    assert run.inbox.pending() == ()
    assert run.inbox.rejected() == ()


def _refused_when_submitted(target: SteerTarget) -> bool:
    """Whether no later state could take ``target``: an undeclared role or an ended attempt."""
    if target.kind is SteerTargetKind.ROLE:
        return target.id not in _DECLARED
    return target.kind is SteerTargetKind.ATTEMPT and _ATTEMPTS.get(target.id or "") in (
        AttemptPhase.CLOSING,
        AttemptPhase.TERMINAL,
    )


def _drops(run: _Run) -> list[str]:
    return [
        t.text for t in run.sink.transitions if t.kind is RunControlTransitionKind.STEER_DROPPED
    ]


@given(target=_TARGETS)
def test_a_socket_steer_is_refused_in_its_ack_only_when_no_later_state_could_take_it(
    target: SteerTarget,
) -> None:
    """Regression: such a steer was acked pending and then only journaled, with no id."""
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)

    ack = run.control.submit(SteerRequest(control_id="s1", text="look again", target=target))

    expected = _expected(target)
    if _refused_when_submitted(target):
        assert ack.status is ControlStatus.REFUSED
        assert ack.reason in (RefusalReason.UNKNOWN_TARGET, RefusalReason.ENDED_TARGET)
        assert run.bridge.poll(core, 2.0) == ()
        assert _drops(run) == []
    elif isinstance(expected, str):
        assert ack.status is ControlStatus.PENDING
        assert run.bridge.poll(core, 2.0) == ()
        (dropped,) = _drops(run)
        assert dropped.startswith("steer s1 reached no turn")
    else:
        assert ack.status is ControlStatus.PENDING
        (control, _delivery) = run.bridge.poll(core, 2.0)
        assert isinstance(control, RunControlEvent)
        assert control.control.target == expected
    assert (
        run.control.submit(SteerRequest(control_id="s1", text="look again", target=target)) == ack
    )


@pytest.mark.parametrize("kind", [SteerTargetKind.ATTEMPT, SteerTargetKind.ITEM])
def test_a_steer_to_a_target_started_after_the_last_poll_reaches_it(
    kind: SteerTargetKind,
) -> None:
    """Regression: the ack was checked against the last poll's state and refused for good."""
    run = _run()
    run.bridge.poll(_core(), 1.0)
    name = "fresh" if kind is SteerTargetKind.ATTEMPT else "item-fresh"
    target = SteerTarget(kind=kind, id=name)

    ack = run.control.submit(SteerRequest(control_id="s1", text="look again", target=target))
    assert ack.status is ControlStatus.PENDING

    started = _core({**_ATTEMPTS, "fresh": AttemptPhase.ACTIVE})
    (control, delivery) = run.bridge.poll(started, 2.0)
    assert isinstance(control, RunControlEvent)
    assert isinstance(delivery, SessionInputReceived)
    resolved = (
        ScopeInputTarget(scope=Scope(owner=AttemptId(root="fresh"), generation=1))
        if kind is SteerTargetKind.ATTEMPT
        else ItemInputTarget(item_id=ItemId(root="item-fresh"))
    )
    assert control.control.target == resolved
    assert delivery.input.target == resolved
    assert _drops(run) == []


def test_a_steer_whose_target_ends_before_the_run_takes_it_is_reported_dropped() -> None:
    run = _run()
    target = SteerTarget(kind=SteerTargetKind.ROLE, id="fake-judge")
    ack = run.control.submit(SteerRequest(control_id="s1", text="look again", target=target))
    assert ack.status is ControlStatus.PENDING  # no poll yet: the run has not been seen

    assert run.bridge.poll(_core(), 1.0) == ()
    (dropped,) = _drops(run)
    assert dropped.startswith("steer s1 ")
    assert "fake-judge" in dropped
    # The step names the control and carries the bare reason, not the journal's sentence.
    (step_,) = _steps(run, SteerDeliveryState.DROPPED)
    assert step_.control_id == "s1"
    assert step_.action is ControlAction.STEER
    assert step_.reason is not None
    assert "fake-judge" in step_.reason
    assert not step_.reason.startswith("steer s1")


@pytest.mark.parametrize(
    ("reason", "report"),
    [
        (InputDropReason.RUN_TERMINAL, "steer s1 reached no turn: run-terminal"),
        (
            InputDropReason.DELIVERY_UNKNOWN,
            "steer s1 may have reached the agent mid-turn: its turn's provider took it, "
            "and no later turn can confirm it",
        ),
    ],
)
def test_a_steer_core_drops_is_reported_once(reason: InputDropReason, report: str) -> None:
    """Regression: only deliveries were reported, so a dropped steer was never heard of; then
    it was named by its core control id (``op-s1``) instead of the id it was sent under.

    A steer a provider took that no turn confirmed is not reported as reaching no turn.
    """
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)
    run.control.submit(SteerRequest(control_id="s1", text="look again"))
    for event in run.bridge.poll(core, 2.0):
        core = step(core, event).state
    (record,) = core.sessions.inputs
    receipt = InputDropped(
        input_id=record.input.input_id,
        target=record.input.target,
        reason=reason,
        at=3.0,
    )
    dropped = core.sessions.model_copy(
        update={"inputs": (record.model_copy(update={"receipt": receipt}),)}
    )
    core = core.model_copy(update={"sessions": dropped})

    run.bridge.poll(core, 3.0)
    run.bridge.poll(core, 4.0)

    # Named by the id the operator sent it under, as a refusal at take is.
    assert _drops(run) == [report]
    (step_,) = _steps(run, SteerDeliveryState.DROPPED)
    assert step_.control_id == "s1"
    assert step_.reason is not None
    assert report.endswith(step_.reason)


def test_a_steer_with_no_target_is_for_the_whole_run() -> None:
    assert json.loads(SteerRequest(control_id="s", text="t").model_dump_json())["target"] == {
        "kind": "run",
        "id": None,
    }


def _steps(run: _Run, state: SteerDeliveryState) -> list[SteerDelivery]:
    """The steer steps of ``state`` the run reported, in order."""
    return [
        t.delivery
        for t in run.sink.transitions
        if t.delivery is not None and t.delivery.state is state
    ]


def _guidance_ends(run: _Run) -> list[str]:
    return [
        t.text for t in run.sink.transitions if t.kind is RunControlTransitionKind.GUIDANCE_ENDED
    ]


def _dropped_as(reason: InputDropReason) -> Callable[[CoreState], CoreState]:
    def end(core: CoreState) -> CoreState:
        (record,) = core.sessions.inputs
        receipt = InputDropped(
            input_id=record.input.input_id, target=record.input.target, reason=reason, at=3.0
        )
        inputs = (record.model_copy(update={"receipt": receipt}),)
        return core.model_copy(
            update={"sessions": core.sessions.model_copy(update={"inputs": inputs})}
        )

    return end


def _reopened(core: CoreState) -> CoreState:
    attempts = tuple(
        row.model_copy(update={"generation": row.generation + 1})
        if row.attempt_id.root == "live"
        else row
        for row in core.attempts.attempts
    )
    return core.model_copy(
        update={"attempts": core.attempts.model_copy(update={"attempts": attempts})}
    )


@pytest.mark.parametrize(
    ("end", "reason"),
    [
        (_dropped_as(InputDropReason.WITHDRAWN), "withdrawn"),
        (_dropped_as(InputDropReason.OWNER_TERMINAL), "owner-terminal"),
        (_dropped_as(InputDropReason.RUN_TERMINAL), "run-terminal"),
        (_reopened, "its attempt was reopened"),
    ],
)
def test_the_end_of_standing_guidance_is_reported_once_as_an_end_not_a_drop(
    end: Callable[[CoreState], CoreState], reason: str
) -> None:
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)
    target = SteerTarget(kind=SteerTargetKind.ATTEMPT, id="live")
    run.control.submit(
        SteerRequest(control_id="keep", text="keep bf16", target=target, mode=SteerMode.STANDING)
    )
    for event in run.bridge.poll(core, 2.0):
        core = step(core, event).state
    assert _guidance_ends(run) == []

    core = end(core)
    run.bridge.poll(core, 3.0)
    run.bridge.poll(core, 4.0)

    assert _guidance_ends(run) == [f"standing guidance keep ended: {reason}"]
    assert _drops(run) == []
    (step_,) = _steps(run, SteerDeliveryState.ENDED)
    assert (step_.control_id, step_.reason) == ("keep", reason)


def _guided(run: _Run) -> CoreState:
    """A run holding standing guidance for the attempt ``live``, as its bridge recorded it."""
    core = _core()
    run.bridge.poll(core, 1.0)
    target = SteerTarget(kind=SteerTargetKind.ATTEMPT, id="live")
    run.control.submit(
        SteerRequest(control_id="keep", text="keep bf16", target=target, mode=SteerMode.STANDING)
    )
    for event in run.bridge.poll(core, 2.0):
        core = step(core, event).state
    return core


@pytest.mark.parametrize(
    "end",
    [_dropped_as(InputDropReason.WITHDRAWN), _dropped_as(InputDropReason.RUN_TERMINAL), _reopened],
)
def test_a_restarted_bridge_does_not_report_an_end_the_run_already_holds(
    end: Callable[[CoreState], CoreState],
) -> None:
    ended = end(_guided(_run()))

    restarted = _run()
    restarted.bridge.poll(ended, 3.0)
    restarted.bridge.poll(ended, 4.0)

    assert _guidance_ends(restarted) == []


def test_guidance_whose_attempt_reopened_and_then_the_run_ended_is_reported_once() -> None:
    run = _run()
    core = _reopened(_guided(run))
    run.bridge.poll(core, 3.0)
    core = _dropped_as(InputDropReason.RUN_TERMINAL)(core)
    run.bridge.poll(core, 4.0)

    assert _guidance_ends(run) == ["standing guidance keep ended: its attempt was reopened"]


def test_a_withdrawal_of_nothing_standing_is_dropped_under_its_own_id() -> None:
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)
    run.control.submit(WithdrawRequest(control_id="w1", withdraws="never-sent"))
    run.bridge.poll(core, 2.0)

    (queued,) = _steps(run, SteerDeliveryState.WITHDRAW_QUEUED)
    assert (queued.control_id, queued.withdraws, queued.action) == (
        "w1",
        "never-sent",
        ControlAction.WITHDRAW,
    )
    (dropped,) = _steps(run, SteerDeliveryState.DROPPED)
    assert (dropped.control_id, dropped.action) == ("w1", ControlAction.WITHDRAW)


def test_a_steer_queued_names_its_mode_and_target() -> None:
    run = _run()
    target = SteerTarget(kind=SteerTargetKind.ATTEMPT, id="live")
    run.control.submit(
        SteerRequest(control_id="keep", text="keep bf16", target=target, mode=SteerMode.STANDING)
    )

    (queued,) = _steps(run, SteerDeliveryState.QUEUED)
    assert (queued.control_id, queued.mode, queued.target) == ("keep", SteerMode.STANDING, target)


@given(target=_TARGETS)
def test_the_known_targets_refuse_exactly_what_the_run_cannot_resolve(target: SteerTarget) -> None:
    """The read model a reader of the run asks and the run's own resolution agree on every target."""
    refusal = known_steer_targets(_core()).refusal(target)

    assert (refusal is None) == (not isinstance(_expected(target), str))


def test_the_known_targets_list_what_a_steer_may_name_and_a_refusal_offers_them() -> None:
    known = known_steer_targets(_core())

    assert known.roles == _DECLARED
    assert known.ids(SteerTargetKind.ATTEMPT) == ("live", "parked")  # `done` has ended
    assert known.items == tuple(f"item-{name}" for name in _ATTEMPTS)
    assert known.ids(SteerTargetKind.RUN) == ()
    for kind, bad in [
        (SteerTargetKind.ROLE, "fake-judge"),
        (SteerTargetKind.ATTEMPT, "unknown"),
        (SteerTargetKind.ATTEMPT, "done"),
        (SteerTargetKind.ITEM, "item-unknown"),
    ]:
        refusal = known.refusal(SteerTarget(kind=kind, id=bad))
        assert refusal is not None
        assert repr(bad) in refusal.reason
        assert all(name in refusal.reason for name in known.ids(kind))


def test_a_refusal_bounds_the_ids_it_offers() -> None:
    attempts = {f"a{index}": AttemptPhase.ACTIVE for index in range(40)}
    known = known_steer_targets(_core(attempts))

    refusal = known.refusal(SteerTarget(kind=SteerTargetKind.ATTEMPT, id="missing"))

    assert refusal is not None
    assert "and 28 more" in refusal.reason
    assert "a39" not in refusal.reason


def _turn_of(
    core: CoreState, *, attempt: str, input_id: InputId | None, mid_turn: bool
) -> CoreState:
    """``core`` after a turn of the attempt received the one steer input (or only the guidance).

    The input is delivered to the turn, in its prompt or while it ran (``mid_turn``), and a
    standing steer is among the guidance the turn was given.
    """
    (record,) = core.sessions.inputs
    ref = InvocationRef(session_id=SESSION, invocation_id=InvocationId(root="inv-7"), generation=0)
    scope = Scope(owner=RunId(root=attempt), generation=0)
    invocation = Invocation(
        invocation=ref,
        scope=scope,
        turn=turn_spec("inv-7"),
        phase=SessionPhase.EXECUTING,
        until=Until(at=60.0),
        guidance_ids=() if input_id is None else (input_id,),
    )
    observation = Observation(
        event_id=EventId(root="e1"),
        request_id=RequestId(root="dispatch"),
        scope=scope,
        sequence=1,
        observed_at=3.0,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
    )
    receipt = (
        None
        if input_id is not None
        else InputDelivered(
            input_id=record.input.input_id,
            invocation=ref,
            observation=observation,
            mid_turn=mid_turn,
        )
    )
    return core.model_copy(
        update={
            "sessions": core.sessions.model_copy(
                update={
                    "inputs": (record.model_copy(update={"receipt": receipt}),),
                    "invocations": (*core.sessions.invocations, invocation),
                }
            )
        }
    )


def _sent(run: _Run, mode: SteerMode, *, attempt: str = "live") -> CoreState:
    """A run whose bridge recorded one steer ``s1`` of ``mode`` for the attempt, not yet delivered."""
    core = _core()
    run.bridge.poll(core, 1.0)
    target = SteerTarget(kind=SteerTargetKind.ATTEMPT, id=attempt)
    run.control.submit(SteerRequest(control_id="s1", text="look again", target=target, mode=mode))
    for event in run.bridge.poll(core, 2.0):
        core = step(core, event).state
    return core


@pytest.mark.parametrize(
    ("mode", "mid_turn", "state", "note"),
    [
        (SteerMode.NEXT_TURN, False, SteerDeliveryState.REACHED, ""),
        (SteerMode.INTERRUPT, True, SteerDeliveryState.REACHED_MID_TURN, ""),
        (
            SteerMode.INTERRUPT,
            False,
            SteerDeliveryState.REACHED,
            "interrupt steer s1 reached the next turn: no running turn kept it",
        ),
    ],
)
def test_a_steer_a_turn_received_is_reported_once_with_the_turn_it_reached(
    mode: SteerMode, *, mid_turn: bool, state: SteerDeliveryState, note: str
) -> None:
    run = _run()
    core = _turn_of(_sent(run, mode), attempt="live", input_id=None, mid_turn=mid_turn)

    run.bridge.poll(core, 3.0)
    run.bridge.poll(core, 4.0)

    (reached,) = _steps(run, state)
    assert (reached.control_id, reached.role, reached.attempt_id) == ("s1", "worker", "live")
    reports = [
        t for t in run.sink.transitions if t.delivery is not None and t.delivery.state is state
    ]
    assert [(t.round_label, t.execution_id, t.text) for t in reports] == [("live", "inv-7", note)]
    assert _drops(run) == []


def test_standing_guidance_reports_the_first_turn_it_reached_once() -> None:
    run = _run()
    sent = _sent(run, SteerMode.STANDING)
    (record,) = sent.sessions.inputs
    core = _turn_of(sent, attempt="live", input_id=record.input.input_id, mid_turn=False)

    run.bridge.poll(core, 3.0)
    run.bridge.poll(core, 4.0)

    (reached,) = _steps(run, SteerDeliveryState.IN_FORCE)
    assert (reached.control_id, reached.role, reached.attempt_id) == ("s1", "worker", "live")


def test_a_restarted_bridge_does_not_report_a_turn_a_steer_already_reached() -> None:
    run = _run()
    core = _turn_of(_sent(run, SteerMode.NEXT_TURN), attempt="live", input_id=None, mid_turn=False)

    restarted = _run()
    restarted.bridge.poll(core, 3.0)
    restarted.bridge.poll(core, 4.0)

    assert _steps(restarted, SteerDeliveryState.REACHED) == []
