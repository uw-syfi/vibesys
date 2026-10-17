"""A steer reaches the target it names, or is refused with the reason; never someone else.

The run resolves a steer's target against core state: a role the search declares, an attempt
or invocation core holds that has not ended, or an item core holds. The steer control core
records carries the resolved target, and so does the session input that delivers it, so core
hands the message to a matching turn only. A target the run cannot resolve is refused: an
inbox request is set aside with the reason, and a socket steer is reported refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptView,
    CoreState,
    InputDropped,
    InputDropReason,
    InputTarget,
    Invocation,
    InvocationId,
    InvocationInputTarget,
    InvocationRef,
    ItemId,
    ItemInputTarget,
    RoleId,
    RoleInputTarget,
    RunControlEvent,
    SchemaRef,
    Scope,
    ScopeInputTarget,
    SessionId,
    SessionInputReceived,
    SessionPhase,
    SessionSpec,
    TurnSpec,
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
    ControlStatus,
    RefusalReason,
    RunControlTransitionKind,
    SteerRequest,
    SteerTarget,
    SteerTargetKind,
    create_run_control_channel,
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
_INVOCATIONS = {
    "acquiring": SessionPhase.ACQUIRING,
    "running": SessionPhase.EXECUTING,
    "waiting": SessionPhase.SUSPENDED,
    "finished": SessionPhase.TERMINAL,
}
"""Core gives an invocation its inputs as its turn is dispatched, so only one still acquiring
can take a steer."""


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


def _invocation(name: str, phase: SessionPhase, scope: Scope) -> Invocation:
    session = SessionSpec(
        session_id=SessionId(root=f"session-{name}"),
        role_id=RoleId(root=_DECLARED[0]),
        policy="reuse",
        lifetime="owner",
        access=Access.READ_ONLY,
    )
    turn = TurnSpec(
        session=session,
        invocation_id=InvocationId(root=name),
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="plan", version=1),
        charge_class="free",
        max_turns=1,
    )
    return Invocation(
        until=Until(at=100.0),
        invocation=InvocationRef(
            session_id=session.session_id, invocation_id=turn.invocation_id, generation=0
        ),
        scope=scope,
        turn=turn,
        phase=phase,
    )


def _core() -> CoreState:
    base = initial_state()
    run_scope = Scope(owner=base.run.run_id, generation=base.run.generation)
    declaration = base.run.declaration.model_copy(
        update={"roles": tuple(RoleId(root=role) for role in _DECLARED)}
    )
    return base.model_copy(
        update={
            "run": base.run.model_copy(update={"declaration": declaration}),
            "attempts": base.attempts.model_copy(
                update={
                    "attempts": tuple(
                        _attempt(name, phase, base) for name, phase in _ATTEMPTS.items()
                    )
                }
            ),
            "sessions": base.sessions.model_copy(
                update={
                    "invocations": tuple(
                        _invocation(name, phase, run_scope) for name, phase in _INVOCATIONS.items()
                    )
                }
            ),
        }
    )


def _expected(core: CoreState, target: SteerTarget) -> InputTarget | None | str:
    """What the run must make of ``target``: core's input target, or a refusal (a str)."""
    if target.id is None:
        return None
    name = target.id
    live_attempt = _ATTEMPTS.get(name) not in (None, AttemptPhase.TERMINAL)
    live_invocation = _INVOCATIONS.get(name) is SessionPhase.ACQUIRING
    invocations = {row.invocation.invocation_id.root: row for row in core.sessions.invocations}
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
        SteerTargetKind.INVOCATION: (
            live_invocation,
            lambda: InvocationInputTarget(invocation=invocations[name].invocation),
        ),
    }
    resolves, resolved = oracle[target.kind]
    return resolved() if resolves else target.kind.value


_IDS = {
    SteerTargetKind.ROLE: (*_DECLARED, "fake-judge"),
    SteerTargetKind.ATTEMPT: (*_ATTEMPTS, "unknown"),
    SteerTargetKind.ITEM: (*(f"item-{name}" for name in _ATTEMPTS), "item-unknown"),
    SteerTargetKind.INVOCATION: (*_INVOCATIONS, "unknown"),
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
    run.inbox.post("r1", _payload("r1", "prefer latency", target))

    events = run.bridge.poll(core, 1.0)

    expected = _expected(core, target)
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
            run.inbox.post("r1", _payload("r1", "prefer latency", other))
        return
    run.inbox.post("r1", _payload("r1", "prefer latency", other))
    assert _run(run.inbox).bridge.poll(core, 3.0) == ()
    assert run.inbox.pending() == ()
    assert run.inbox.rejected() == ()


@given(target=_TARGETS)
def test_a_socket_steer_to_a_target_no_turn_can_take_is_refused_in_its_ack(
    target: SteerTarget,
) -> None:
    """Regression: such a steer was acked pending and then only journaled, with no id."""
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)

    ack = run.control.submit(SteerRequest(control_id="s1", text="look again", target=target))

    expected = _expected(core, target)
    if isinstance(expected, str):
        assert ack.status is ControlStatus.REFUSED
        assert ack.reason in (RefusalReason.UNKNOWN_TARGET, RefusalReason.ENDED_TARGET)
        assert run.bridge.poll(core, 2.0) == ()
    else:
        assert ack.status is ControlStatus.PENDING
        (control, _delivery) = run.bridge.poll(core, 2.0)
        assert isinstance(control, RunControlEvent)
        assert control.control.target == expected
    assert (
        run.control.submit(SteerRequest(control_id="s1", text="look again", target=target)) == ack
    )


def test_a_steer_whose_target_ends_before_the_run_takes_it_is_reported_dropped() -> None:
    run = _run()
    target = SteerTarget(kind=SteerTargetKind.ROLE, id="fake-judge")
    ack = run.control.submit(SteerRequest(control_id="s1", text="look again", target=target))
    assert ack.status is ControlStatus.PENDING  # no poll yet: the run has not been seen

    assert run.bridge.poll(_core(), 1.0) == ()
    (dropped,) = [
        t for t in run.sink.transitions if t.kind is RunControlTransitionKind.STEER_DROPPED
    ]
    assert "s1" in dropped.text
    assert "fake-judge" in dropped.text


def test_a_steer_core_drops_is_reported_once() -> None:
    """Regression: only deliveries were reported, so a dropped steer was never heard of."""
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
        reason=InputDropReason.RUN_TERMINAL,
        at=3.0,
    )
    dropped = core.sessions.model_copy(
        update={"inputs": (record.model_copy(update={"receipt": receipt}),)}
    )
    core = core.model_copy(update={"sessions": dropped})

    run.bridge.poll(core, 3.0)
    run.bridge.poll(core, 4.0)

    reports = [
        t.text for t in run.sink.transitions if t.kind is RunControlTransitionKind.STEER_DROPPED
    ]
    assert reports == ["steer op-s1 reached no turn: run-terminal"]


def test_a_steer_with_no_target_is_for_the_whole_run() -> None:
    assert json.loads(SteerRequest(control_id="s", text="t").model_dump_json())["target"] == {
        "kind": "run",
        "id": None,
    }
