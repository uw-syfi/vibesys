"""An operator action reaches core through the run's control path, and the operator hears its fate.

Core's ``control_refusal`` is the one rule for whether an action is taken. A socket action
is answered when it is submitted against a state that may be a poll behind, so its ack refuses
only what no later state could take: an attempt that has ended, an item already cancelled, a
limit above the run's launch limit. Anything else is judged when the run takes it; a refusal
then reaches the operator as ``control_refused`` with core's wording, and an action core
records is reported applied once the shell commits it. A request naming an id core's types
refuse is refused when it is read, at every ingress, and never ends the run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import (
    ACTION_REFUSAL_TEXT,
    ActionRefusal,
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptReopenRequest,
    AttemptView,
    ControlId,
    ControlRefusedError,
    CoreState,
    DecisionId,
    ItemId,
    RequestId,
    RunControlEvent,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    step,
)
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    CONTROL_REQUEST,
    REFUSAL_TEXT,
    CancelAttemptRequest,
    CancelItemRequest,
    ChannelRunControl,
    ControlStatus,
    RefusalReason,
    RunControlTransitionKind,
    SetMaxParallelRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

_PHASES = {
    "A-live": AttemptPhase.ACTIVE,
    "A-parked": AttemptPhase.PARKED,
    "A-blocked": AttemptPhase.BLOCKED,
    "A-done": AttemptPhase.TERMINAL,
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

    def told(self, kind: RunControlTransitionKind) -> list[str]:
        return [t.text for t in self.sink.transitions if t.kind is kind]


def _run() -> _Run:
    threads = SimThreads()
    sink = FakeRunControlEventSink()
    channel = create_run_control_channel(sink, threads=threads)
    control = ChannelRunControl(channel, threads)
    inbox = FakeControlInbox()
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    return _Run(bridge, control, inbox, sink)


def _core(*, cancelled: str | None = None) -> CoreState:
    """A run holding one attempt in each phase, each at item ``I-<attempt>``.

    ``cancelled`` names an item the operator already cancelled.
    """
    base = initial_state()
    rows = tuple(
        AttemptView(
            attempt_id=AttemptId(root=name),
            generation=1,
            item_id=ItemId(root=f"I-{name}"),
            phase=phase,
            workspace=WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=base.run.facts.baseline
            ),
            budget=AttemptBudget(),
        )
        for name, phase in _PHASES.items()
    )
    core = base.model_copy(update={"attempts": base.attempts.model_copy(update={"attempts": rows})})
    if cancelled is None:
        return core
    run = _run()
    run.control.submit(CancelItemRequest(control_id="earlier", item=cancelled))
    for event in run.bridge.poll(core, 0.5):
        core = step(core, event).state
    return core


def _final(request: CancelAttemptRequest | CancelItemRequest | SetMaxParallelRequest) -> object:
    """What no later state of ``_core(cancelled="I-A-done")`` could take: the ack's reason."""
    match request:
        case CancelAttemptRequest(attempt="A-done"):
            return RefusalReason.ATTEMPT_ENDED
        case CancelItemRequest(item="I-A-done"):
            return RefusalReason.ALREADY_CANCELLED
        case SetMaxParallelRequest(max_parallel=limit) if limit > _LAUNCH_LIMIT:
            return RefusalReason.ABOVE_LAUNCH_LIMIT
        case _:
            return None


_LAUNCH_LIMIT = initial_state().run.limits.max_parallel
_REQUESTS = st.one_of(
    st.builds(
        CancelAttemptRequest,
        control_id=st.just("c1"),
        attempt=st.sampled_from([*_PHASES, "A-none"]),
    ),
    st.builds(
        CancelItemRequest,
        control_id=st.just("c1"),
        item=st.sampled_from([*(f"I-{name}" for name in _PHASES), "I-none"]),
    ),
    st.builds(
        SetMaxParallelRequest,
        control_id=st.just("c1"),
        max_parallel=st.integers(min_value=1, max_value=_LAUNCH_LIMIT + 2),
    ),
)


@given(_REQUESTS)
@example(CancelAttemptRequest(control_id="c1", attempt="A-done"))
@example(CancelItemRequest(control_id="c1", item="I-A-done"))
@example(SetMaxParallelRequest(control_id="c1", max_parallel=_LAUNCH_LIMIT + 1))
@example(CancelAttemptRequest(control_id="c1", attempt="A-parked"))
@example(CancelAttemptRequest(control_id="c1", attempt="A-none"))
def test_a_socket_action_is_refused_in_its_ack_only_when_no_later_state_could_take_it(
    request: CancelAttemptRequest | CancelItemRequest | SetMaxParallelRequest,
) -> None:
    """The ack's refusal is the one core's rule gives against the run's real state."""
    run = _run()
    core = _core(cancelled="I-A-done")
    run.bridge.poll(core, 1.0)

    ack = run.control.submit(request)

    reason = _final(request)
    if reason is not None:
        assert (ack.status, ack.reason) == (ControlStatus.REFUSED, reason)
        assert run.bridge.poll(core, 2.0) == ()
    else:
        assert (ack.status, ack.reason) == (ControlStatus.PENDING, None)


@pytest.mark.parametrize(
    ("request_", "refusal"),
    [
        (CancelAttemptRequest(control_id="c1", attempt="A-parked"), ActionRefusal.PARKED),
        (CancelAttemptRequest(control_id="c1", attempt="A-blocked"), ActionRefusal.PARKED),
        (CancelAttemptRequest(control_id="c1", attempt="A-none"), ActionRefusal.NO_SUCH_SUBJECT),
        (CancelItemRequest(control_id="c1", item="I-none"), ActionRefusal.NO_SUCH_SUBJECT),
    ],
)
def test_an_action_core_refuses_when_taken_reaches_the_operator_in_cores_words(
    request_: CancelAttemptRequest | CancelItemRequest, refusal: ActionRefusal
) -> None:
    """Through the socket it is ``control_refused``; through the inbox, the request's rejection."""
    socket, inbox = _run(), _run()
    core = _core()
    socket.bridge.poll(core, 1.0)
    assert socket.control.submit(request_).status is ControlStatus.PENDING
    inbox.inbox.post("c1", request_.model_dump_json().encode(), ended=lambda: None)

    assert socket.bridge.poll(core, 2.0) == ()
    assert inbox.bridge.poll(core, 2.0) == ()

    words = ACTION_REFUSAL_TEXT[refusal]
    assert socket.told(RunControlTransitionKind.CONTROL_REFUSED) == [
        f"{request_.action.value} c1: {words}"
    ]
    assert [(e.request_id, e.reason) for e in inbox.inbox.rejected()] == [
        ("c1", f"refused by the run: {words}")
    ]


def test_every_ingress_words_a_final_refusal_as_core_does() -> None:
    """The ack's reason, the socket's protocol error and an inbox rejection read alike."""
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)
    request = CancelAttemptRequest(control_id="c1", attempt="A-done")
    run.inbox.post("c1", request.model_dump_json().encode(), ended=lambda: None)

    ack = run.control.submit(request.model_copy(update={"control_id": "c2"}))
    run.bridge.poll(core, 2.0)

    assert ack.reason is RefusalReason.ATTEMPT_ENDED
    words = ACTION_REFUSAL_TEXT[ActionRefusal.ENDED]
    assert REFUSAL_TEXT[ack.reason] == words
    assert [(e.request_id, e.reason) for e in run.inbox.rejected()] == [
        ("c1", f"refused by the run: {words}")
    ]


def test_an_action_core_records_is_reported_applied_once() -> None:
    """Regression: an applied action stayed journaled pending, like one the run never took."""
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)
    run.control.submit(SetMaxParallelRequest(control_id="l1", max_parallel=1))
    run.control.submit(CancelItemRequest(control_id="c1", item="I-A-done"))

    for event in run.bridge.poll(core, 2.0):
        core = step(core, event).state
    run.bridge.settled(core, running=False)
    run.bridge.poll(core, 3.0)
    run.bridge.settled(core, running=False)

    assert run.told(RunControlTransitionKind.ACTION_APPLIED) == [
        "set_max_parallel 1",
        "cancel_item I-A-done",
    ]


def test_an_action_core_refuses_at_commit_is_refused_through_the_one_path() -> None:
    """Something committed first changed the run: core's refusal reaches the operator."""
    run = _run()
    core = _core()
    run.bridge.poll(core, 1.0)
    run.control.submit(CancelItemRequest(control_id="c1", item="I-A-done"))
    (emitted,) = run.bridge.poll(core, 2.0)
    assert isinstance(emitted, RunControlEvent)
    # Another operator's cancel of the same item commits first.
    earlier = emitted.control.model_copy(update={"control_id": ControlId(root="other")})
    core = step(core, RunControlEvent(control=earlier, now_at=1.5)).state
    with pytest.raises(ControlRefusedError) as refused:
        step(core, emitted)

    run.bridge.settled(core, running=False, refused=(refused.value,))

    words = ACTION_REFUSAL_TEXT[ActionRefusal.ALREADY_CANCELLED]
    assert run.told(RunControlTransitionKind.CONTROL_REFUSED) == [f"cancel_item c1: {words}"]
    assert run.told(RunControlTransitionKind.ACTION_APPLIED) == []


_UNHELD_IDS = st.builds(
    lambda before, space, after: before + space + after,
    st.text(max_size=4),
    st.sampled_from([" ", "\t", "\n", "\r", "\u00a0", "\u2003", "\u3000"]),
    st.text(max_size=4),
)
"""Ids with whitespace in them: none names anything core could hold."""


@given(subject=_UNHELD_IDS, field_=st.sampled_from(["attempt", "item", "target"]))
@example(subject="a b", field_="attempt")
@example(subject="x\ny", field_="item")
@example(subject="a b", field_="target")
def test_a_request_naming_an_id_core_refuses_never_ends_the_run(subject: str, field_: str) -> None:
    """Regression (#3123): such a request ended the run, and again at every restart.

    The socket refuses it when it parses the request, naming the key; the inbox sets it
    aside with the reason, and the run polls on.
    """
    payload = {
        "attempt": {"action": "cancel_attempt", "attempt": subject},
        "item": {"action": "cancel_item", "item": subject},
        "target": {"action": "steer", "text": "t", "target": {"kind": "role", "id": subject}},
    }[field_]
    run = _run()
    core = _core()
    with pytest.raises(ValidationError, match=field_ if field_ != "target" else "id"):
        CONTROL_REQUEST.validate_python({"control_id": "c1", **payload})
    run.inbox.post("c1", json.dumps({"control_id": "c1", **payload}).encode(), ended=lambda: None)

    for at in (1.0, 2.0):
        for event in run.bridge.poll(core, at):
            core = step(core, event).state

    ((request_id, reason),) = [(e.request_id, e.reason) for e in run.inbox.rejected()]
    assert request_id == "c1"
    assert reason.startswith("not a control request")
    assert core.run.controls == ()


def test_a_parallel_limit_from_a_lax_number_is_refused_when_read() -> None:
    """``"3"``, ``true`` and ``2.0`` are not limits: the request is refused, not coerced."""
    for value in ("3", True, 2.0):
        with pytest.raises(ValidationError, match="max_parallel"):
            CONTROL_REQUEST.validate_python(
                {"control_id": "l1", "action": "set_max_parallel", "max_parallel": value}
            )


def test_a_cancelled_items_notice_says_what_became_of_each_of_its_attempts() -> None:
    """D220: the notice names each attempt the cancel stopped, unqueued, left parked, or let finish."""
    base = initial_state()
    phases = {
        "A1": AttemptPhase.ACTIVE,
        "A2": AttemptPhase.PARKED,
        "A3": AttemptPhase.CLOSING,
        "A4": AttemptPhase.TERMINAL,
        "A5": AttemptPhase.PARKED,
    }
    rows = tuple(
        AttemptView(
            attempt_id=AttemptId(root=name),
            generation=1,
            item_id=ItemId(root="I-shared"),
            phase=phase,
            workspace=WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=base.run.facts.baseline
            ),
            budget=AttemptBudget(),
        )
        for name, phase in phases.items()
    )
    reopen = AttemptReopenRequest(
        decision_id=DecisionId(root="reopen-A5"),
        request_id=RequestId(root="operation:reopen-A5"),
        attempt=AttemptRef(attempt_id=AttemptId(root="A5"), generation=1),
    )
    core = base.model_copy(
        update={
            "attempts": base.attempts.model_copy(update={"attempts": rows}),
            "scheduling": base.scheduling.model_copy(update={"queue": (reopen,)}),
        }
    )
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    texts = _Texts()
    bridge = RunControlBridge(channel, texts, control=control)
    control.submit(CancelItemRequest(control_id="c1", item="I-shared"))

    bridge.poll(core, 1.0)

    assert list(texts.held.values()) == [
        "The operator cancelled work item I-shared. No new attempt at it will start; do not"
        " start or continue work on it. Attempt A1 has stopped and will not be evaluated."
        " Attempt A5 was waiting to reopen; its queued reopen is cancelled and it will not run"
        " again. Attempt A2 stays parked and will not be reopened. Attempt A3 was already finishing;"
        " it finishes and its result stands."
    ]
