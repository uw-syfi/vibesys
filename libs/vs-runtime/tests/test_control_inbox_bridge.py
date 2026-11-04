"""The run applies each request in its control inbox exactly once, across crashes and resumes.

A generated history interleaves posting requests, control polls whose events core commits,
polls whose events are lost (the process died before core committed them), and restarts (a
new bridge and channel over the same inbox and core state). Whatever the history, once the
run settles every request is either recorded by core under ``op-<control id>`` exactly once
or set aside with a reason, never both, and nothing stays pending. Requests apply in the
order they were posted, and the operator is told of each one set aside, once.

The run holds a parked attempt and an ended one, so an operator action (a cancel, a new
parallel limit) may apply or be refused (a cancel that would stop a running attempt is the
orchestration tests' business: it needs a run that started one), and a request naming an id core's types refuse (whitespace)
is set aside like any request the run cannot apply: it never ends the run.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_core.api import (
    ArtifactRef,
    AttemptBudget,
    AttemptId,
    AttemptPhase,
    AttemptView,
    CoreState,
    ItemId,
    RunControlEvent,
    RunStatus,
    SessionInputReceived,
    WorkspaceMode,
    WorkspacePlan,
    initial_state,
    step,
)
from vs_project.api import (
    InboxClosedError,
    InboxEntry,
    ProjectStateError,
    RequestState,
    RequestStatus,
    ended_unapplied,
)
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    CONTROL_REQUEST,
    CancelAttemptRequest,
    ChannelRunControl,
    ControlAck,
    ControlRequest,
    ControlStatus,
    PauseNowRequest,
    PauseRequest,
    ResumeRequest,
    RunControlTransitionKind,
    SteerRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import CoreEvent


@dataclass
class _Texts:
    held: dict[str, str] = field(default_factory=dict)

    def put(self, ref: ArtifactRef, text: str) -> None:
        self.held[ref.artifact_id.root] = text


def _wired(
    inbox: FakeControlInbox, texts: _Texts, sink: FakeRunControlEventSink | None = None
) -> tuple[RunControlBridge, ChannelRunControl]:
    """A bridge over ``inbox`` and the run control a socket would submit to."""
    threads = SimThreads()
    channel = create_run_control_channel(sink or FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    return RunControlBridge(channel, texts, inbox=OperatorInbox(inbox, control)), control


def _bridge(inbox: FakeControlInbox, texts: _Texts) -> RunControlBridge:
    return _wired(inbox, texts)[0]


def _payload(request: dict[str, object] | bytes, control_id: str) -> bytes:
    """The file a sender writes: a request named by its file name, or raw bytes."""
    if isinstance(request, bytes):
        return request
    return json.dumps({"control_id": control_id, **request}).encode()


_PAYLOADS: st.SearchStrategy[dict[str, object] | bytes] = st.one_of(
    st.builds(lambda text: {"action": "steer", "text": text}, st.text(min_size=1, max_size=12)),
    st.just({"action": "pause", "expected_epoch": 0}),
    st.just({"action": "resume"}),
    st.just({"action": "resume", "fallback": True}),
    st.just({"action": "resume", "when": "capacity_returns"}),
    st.just({"action": "pause_now", "expected_epoch": 0}),
    st.builds(
        lambda attempt: {"action": "cancel_attempt", "attempt": attempt},
        st.sampled_from(["A-parked", "A-done", "A-none"]),
    ),
    st.builds(
        lambda item: {"action": "cancel_item", "item": item},
        st.sampled_from(["I-done", "I-none"]),
    ),
    st.builds(
        lambda limit: {"action": "set_max_parallel", "max_parallel": limit},
        st.integers(min_value=1, max_value=6),
    ),
    st.sampled_from(
        [
            b"",
            b"{}",
            b'{"control_id": "x", "action": "rewrite_checker"}',
            b'{"control_id": "x", "action": "pause", "extra": 1}',
            b'{"control_id": "elsewhere", "action": "pause"}',
        ]
    ),
)

_SUBJECTS_CORE_REFUSES = st.sampled_from(
    [
        {"action": "cancel_attempt", "attempt": "A done"},
        {"action": "cancel_item", "item": "I\ndone"},
        {"action": "steer", "text": "t", "target": {"kind": "role", "id": "a b"}},
        {"action": "set_max_parallel", "max_parallel": "2"},
    ]
)
"""Requests a sender may write that name an id core's types refuse, or a lax number."""

_IDS = st.sampled_from(["k0", "k1", "k2"])
"""Ids shared by both ingresses, so a resend through the socket, the inbox or both collides."""

_STEPS = st.lists(
    st.one_of(
        st.tuples(st.just("post"), _IDS, _PAYLOADS),
        st.tuples(st.just("post"), st.none(), _PAYLOADS),
        st.tuples(st.just("post"), st.none(), _SUBJECTS_CORE_REFUSES),
        st.tuples(st.just("socket"), _IDS, _PAYLOADS.filter(lambda p: isinstance(p, dict))),
        st.tuples(st.just("poll")),
        st.tuples(st.just("lost poll")),
        st.tuples(st.just("restart")),
    ),
    max_size=24,
)


def _live() -> str | None:
    """A poster's view of a run that has not ended."""
    return None


_LIVE = _live


def _json(request: BaseModel) -> bytes:
    return request.model_dump_json().encode()


def _with_attempts() -> CoreState:
    """A run holding a parked attempt, ``A-parked``, and an ended one, ``A-done`` at ``I-done``."""
    base = initial_state()

    def attempt(name: str, item: str, phase: AttemptPhase) -> AttemptView:
        return AttemptView(
            attempt_id=AttemptId(root=name),
            generation=1,
            item_id=ItemId(root=item),
            phase=phase,
            workspace=WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=base.run.facts.baseline
            ),
            budget=AttemptBudget(),
        )

    rows = (
        attempt("A-parked", "I-parked", AttemptPhase.PARKED),
        attempt("A-done", "I-done", AttemptPhase.TERMINAL),
    )
    return base.model_copy(update={"attempts": base.attempts.model_copy(update={"attempts": rows})})


def _commit(core: CoreState, events: tuple[CoreEvent, ...]) -> CoreState:
    for event in events:
        core = step(core, event).state
    return core


_STEER_AGAIN_AFTER_RESTART = [
    ("socket", "k0", {"action": "steer", "text": "t"}),
    ("poll",),
    ("restart",),
    ("poll",),
    ("socket", "k0", {"action": "steer", "text": "t"}),
]
_PAUSE_AGAIN_AFTER_RESUME_AND_RESTART = [
    ("socket", "k0", {"action": "pause", "expected_epoch": 0}),
    ("poll",),
    ("socket", "k1", {"action": "resume"}),
    ("poll",),
    ("restart",),
    ("poll",),
    ("socket", "k0", {"action": "pause", "expected_epoch": 0}),
]
# Pause parks the epoch before the steer's delivery is committed.
_PAUSE_THEN_STEER = [
    ("post", None, {"action": "pause", "expected_epoch": 0}),
    ("post", None, {"action": "steer", "text": "t"}),
    ("poll",),
]
_PAUSE_THEN_STEER_AFTER_A_POLL = [
    ("post", None, {"action": "pause", "expected_epoch": 0}),
    ("poll",),
    ("post", None, {"action": "steer", "text": "t"}),
    ("poll",),
]
_OTHER_CONTROL_UNDER_A_RECORDED_ID = [
    ("post", "k0", {"action": "pause", "expected_epoch": 0}),
    ("poll",),
    ("restart",),
    ("poll",),
    ("socket", "k0", {"action": "pause_now", "expected_epoch": 0}),
]

_POSTED_IN_REVERSE_ID_ORDER = [
    ("post", None, {"action": "pause", "expected_epoch": 0}),
    ("post", None, {"action": "resume"}),
    ("poll",),
]
"""The pause gets the larger id; posting order, not id order, must apply it first."""
_ACTIONS_IN_POSTING_ORDER = [
    ("post", None, {"action": "cancel_item", "item": "I-done"}),
    ("post", None, {"action": "steer", "text": "I-done is cancelled; move on"}),
    ("post", None, {"action": "cancel_item", "item": "I-done"}),
    ("post", None, {"action": "cancel_attempt", "attempt": "A-parked"}),
    ("post", None, {"action": "set_max_parallel", "max_parallel": 1}),
    ("lost poll",),
    ("poll",),
]
"""The cancel applies before the steer posted after it; the same cancel again, and a cancel of
a parked attempt, are set aside with core's reason; a lost poll applies none of them twice."""
_A_SUBJECT_CORE_REFUSES_IS_SET_ASIDE = [
    ("post", None, {"action": "cancel_attempt", "attempt": "A done"}),
    ("poll",),
    ("restart",),
    ("post", None, {"action": "pause", "expected_epoch": 0}),
    ("poll",),
]
"""Regression (#3123): a whitespace id ended the run at every restart; now it is set aside."""
_REJECTED_AND_POLLED_AGAIN_AFTER_RESTART = [
    ("post", None, {"action": "pause_now", "expected_epoch": 0}),
    ("poll",),
    ("post", None, {"action": "pause", "expected_epoch": 0}),
    ("poll",),
    ("restart",),
    ("poll",),
]
"""A pause after a stop is set aside: reported once, though polled again after a restart."""
_REFUSED_ON_THE_SOCKET_THEN_POSTED = [
    ("socket", "k0", {"action": "cancel_attempt", "attempt": "A-none"}),
    ("post", "k0", {"action": "cancel_attempt", "attempt": "A-none"}),
    ("poll",),
]
"""The socket's copy is refused when taken; its inbox twin is set aside with the same reason."""


@given(_STEPS)
@example(_REFUSED_ON_THE_SOCKET_THEN_POSTED)
@example(_POSTED_IN_REVERSE_ID_ORDER)
@example(_ACTIONS_IN_POSTING_ORDER)
@example(_A_SUBJECT_CORE_REFUSES_IS_SET_ASIDE)
@example(_REJECTED_AND_POLLED_AGAIN_AFTER_RESTART)
@example(_STEER_AGAIN_AFTER_RESTART)
@example(_PAUSE_AGAIN_AFTER_RESUME_AND_RESTART)
@example(_OTHER_CONTROL_UNDER_A_RECORDED_ID)
@example(_PAUSE_THEN_STEER)
@example(_PAUSE_THEN_STEER_AFTER_A_POLL)
def test_every_control_is_recorded_once_whichever_way_it_was_sent(
    history: list[tuple[object, ...]],
) -> None:
    """Regression: a control sent through the socket and the inbox under one id applied twice.

    Each inbox request ends settled or set aside; a settled one is recorded as
    ``op-<id>``; each ``op-<id>`` is recorded at most once; and a control is recorded under
    a fresh ``control-<n>`` only if the socket sent it after a restart, before the restarted
    run's first poll told it which ids the run records. A restart records nothing by itself:
    the new channel starts from the pause core recorded.
    """
    inbox = FakeControlInbox()
    texts = _Texts()
    sink = FakeRunControlEventSink()
    core = _with_attempts()
    bridge, control = _wired(inbox, texts, sink)
    posted: set[str] = set()
    first_posts: list[str] = []
    sent: set[tuple[str, bytes]] = set()
    fresh = True
    unrecalled = 0
    restarts = 0
    for index, operation in enumerate(history):
        match operation:
            case ("socket", str(control_id), dict(payload)):
                request = CONTROL_REQUEST.validate_json(_payload(payload, control_id))
                control.submit(request)
                sent.add((control_id, _json(request)))
                unrecalled += fresh
            case ("post", str() | None as shared, dict() | bytes() as payload):
                # Later posts get ids that sort first, so id order is never posting order.
                request_id = shared or f"r{999 - index:03d}"
                contents = _payload(payload, request_id)
                with contextlib.suppress(ProjectStateError):  # an id holds one request
                    inbox.post(request_id, contents, ended=_LIVE)
                    if request_id not in posted:
                        first_posts.append(request_id)
                    posted.add(request_id)
                    sent.add((request_id, contents))
            case ("poll",):
                core = _commit(core, bridge.poll(core, float(index)))
                fresh = False
            case ("lost poll",):
                bridge.poll(core, float(index))
                bridge, control = _wired(inbox, texts, sink)
                fresh, restarts = True, restarts + 1
            case ("restart",):
                bridge, control = _wired(inbox, texts, sink)
                fresh, restarts = True, restarts + 1
    for settle in range(3):
        core = _commit(core, bridge.poll(core, 1000.0 + settle))

    recorded = [control.control_id.root for control in core.run.controls]
    rejected = {entry.request_id for entry in inbox.rejected()}
    assert inbox.pending() == ()
    assert len(recorded) <= len(sent) + restarts
    operator_ids = [identity for identity in recorded if identity.startswith("op-")]
    assert [i for i, n in Counter(operator_ids).items() if n > 1] == []
    assert len(recorded) - len(operator_ids) <= unrecalled
    for request_id in posted - rejected:
        assert f"op-{request_id}" in recorded, request_id
    _check_inputs(core)
    _check_posting_order_and_reports(core, inbox, sink, first_posts)


def _check_inputs(core: CoreState) -> None:
    """Each recorded steer has its delivery, and each recorded action its notice; no other."""
    steer_controls = {
        control.control_id.root for control in core.run.controls if control.action == "steer"
    }
    notices = {
        control.control_id.root
        for control in core.run.controls
        if control.action in ("cancel_attempt", "cancel_item", "set_max_parallel")
    }
    inputs = {row.input.input_id.root for row in core.sessions.inputs}
    assert {f"steer-{identity}" for identity in steer_controls} | {
        f"notice-{identity}" for identity in notices
    } == inputs


def _check_posting_order_and_reports(
    core: CoreState, inbox: FakeControlInbox, sink: FakeRunControlEventSink, posts: list[str]
) -> None:
    """Inbox-only requests are recorded in posting order; each set aside is reported once."""
    recorded = [control.control_id.root for control in core.run.controls]
    inbox_only = [f"op-{request_id}" for request_id in posts if request_id.startswith("r")]
    assert [identity for identity in recorded if identity in inbox_only] == [
        identity for identity in inbox_only if identity in recorded
    ]
    reported = Counter(
        t.summary for t in sink.transitions if t.kind is RunControlTransitionKind.REQUEST_REJECTED
    )
    assert reported == Counter(
        f"control request {entry.request_id} rejected: {entry.reason}" for entry in inbox.rejected()
    )


def test_an_inbox_request_whose_socket_copy_was_refused_gets_the_same_answer() -> None:
    """One control id, one answer: the socket's refusal reason, not a second, different one."""
    inbox = FakeControlInbox()
    sink = FakeRunControlEventSink()
    bridge, control = _wired(inbox, _Texts(), sink)
    request = CancelAttemptRequest(control_id="k0", attempt="A-none")
    control.submit(request)
    inbox.post("k0", _json(request), ended=_LIVE)

    _commit(_with_attempts(), bridge.poll(_with_attempts(), 1.0))

    [refused] = [
        t.text for t in sink.transitions if t.kind is RunControlTransitionKind.CONTROL_REFUSED
    ]
    label, reason = refused.split(": ", 1)
    assert label == "cancel_attempt k0"
    [entry] = inbox.rejected()
    assert (entry.request_id, entry.reason) == ("k0", f"refused by the run: {reason}")


def test_a_pause_sent_through_the_socket_and_the_inbox_is_recorded_once() -> None:
    inbox = FakeControlInbox()
    bridge, control = _wired(inbox, _Texts())
    control.submit(PauseRequest(control_id="k1", expected_epoch=0))
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))

    inbox.post("k1", _json(PauseRequest(control_id="k1", expected_epoch=0)), ended=_LIVE)
    for at in (2.0, 3.0):
        core = _commit(core, bridge.poll(core, at))

    assert [(c.control_id.root, c.action) for c in core.run.controls] == [("op-k1", "pause")]
    assert inbox.pending() == ()
    assert inbox.rejected() == ()


@given(
    seconds=st.one_of(st.none(), st.floats(min_value=0.5, max_value=3600.0)),
    now=st.floats(min_value=1.0, max_value=1000.0),
)
@example(seconds=7.5, now=1.0)
def test_the_drain_deadline_of_a_pause_travels_from_the_request_into_the_park(
    seconds: float | None, now: float
) -> None:
    """The deadline is the pause input's own field: sent over the socket or the inbox, core keeps it.

    A Pause-now that names none waits the cleanup bound; a Pause that names none waits for its turns.
    """
    for request in (
        PauseRequest(control_id="k1", expected_epoch=0, drain_seconds=seconds),
        PauseNowRequest(control_id="k1", expected_epoch=0, drain_seconds=seconds),
    ):
        inbox = FakeControlInbox()
        bridge, control = _wired(inbox, _Texts())
        control.submit(request)
        core = _commit(initial_state(), bridge.poll(initial_state(), now))
        inbox.post("k1", _json(request), ended=_LIVE)
        core = _commit(core, bridge.poll(core, now + 1))
        park = core.run.park
        assert park is not None
        assert park.immediate == isinstance(request, PauseNowRequest)
        if seconds is not None:
            wait = seconds
        elif park.immediate:
            wait = core.run.limits.cleanup_bound
        else:
            assert park.deadline_at is None
            continue
        assert park.deadline_at == now + wait


def test_a_control_resent_after_a_restart_replays_its_ack_and_changes_nothing() -> None:
    """Regression: a pause resent after a resume and a restart paused the channel but not core."""
    inbox = FakeControlInbox()
    bridge, control = _wired(inbox, _Texts())
    first = control.submit(PauseRequest(control_id="p", expected_epoch=0))
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    control.submit(ResumeRequest(control_id="r"))
    core = _commit(core, bridge.poll(core, 2.0))
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    core = _commit(core, bridge.poll(core, 3.0))

    again = control.submit(PauseRequest(control_id="p", expected_epoch=0))
    events = bridge.poll(core, 4.0)

    assert again == first
    assert events == ()
    assert not channel.pause_requested()
    assert [c.control_id.root for c in core.run.controls] == ["op-p"]


def test_another_control_under_an_id_recorded_before_a_restart_conflicts() -> None:
    """Regression: a stop reusing a recorded pause's id after a restart stopped the run."""
    inbox = FakeControlInbox()
    bridge, control = _wired(inbox, _Texts())
    control.submit(PauseRequest(control_id="p", expected_epoch=0))
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    bridge, control = _wired(inbox, _Texts())
    core = _commit(core, bridge.poll(core, 2.0))

    ack = control.submit(PauseNowRequest(control_id="p", expected_epoch=0))
    core = _commit(core, bridge.poll(core, 3.0))

    assert ack.status is ControlStatus.CONFLICT
    assert [c.action for c in core.run.controls if c.action == "stop"] == []


def test_a_request_that_is_not_a_control_is_set_aside_with_its_reason() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", b'{"control_id": "r1", "action": "pause", "extra": 1}', ended=_LIVE)
    bridge = _bridge(inbox, _Texts())

    assert bridge.poll(initial_state(), 1.0) == ()
    (entry,) = inbox.rejected()
    assert entry.request_id == "r1"
    assert "extra" in entry.reason


def test_a_capacity_wait_with_no_capacity_pause_is_set_aside() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(ResumeRequest(control_id="r1", when="capacity_returns")), ended=_LIVE)

    assert _bridge(inbox, _Texts()).poll(initial_state(), 1.0) == ()
    assert [entry.reason for entry in inbox.rejected()] == [
        "refused by the run: only a paused run waits for capacity"
    ]


def test_inbox_requests_apply_in_posting_order_as_their_actions_say() -> None:
    """Regression: requests applied in id order, so a resume named "r1" ran before a pause."""
    inbox = FakeControlInbox()
    inbox.post("r2", _json(SteerRequest(control_id="r2", text="prefer latency")), ended=_LIVE)
    inbox.post("r3", _json(PauseRequest(control_id="r3", expected_epoch=0)), ended=_LIVE)
    inbox.post("r1", _json(ResumeRequest(control_id="r1")), ended=_LIVE)
    texts = _Texts()
    bridge = _bridge(inbox, texts)

    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))

    assert [(c.control_id.root, c.action) for c in core.run.controls] == [
        ("op-r2", "steer"),
        ("op-r3", "pause"),
    ]
    assert core.run.status == RunStatus.PARKED
    assert list(texts.held.values()) == ["prefer latency"]
    (steer,) = core.sessions.inputs
    assert steer.input.input_id.root == "steer-op-r2"
    assert steer.input.sequence == 1
    # Core holds all three, so the next poll settles them and applies nothing again.
    assert bridge.poll(core, 2.0) == ()
    assert inbox.pending() == ()


def test_a_pause_after_a_stop_is_set_aside() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(PauseNowRequest(control_id="r1", expected_epoch=0)), ended=_LIVE)
    inbox.post("r2", _json(PauseRequest(control_id="r2", expected_epoch=0)), ended=_LIVE)

    events = _bridge(inbox, _Texts()).poll(initial_state(), 1.0)

    assert [
        event.control.control_id.root for event in events if isinstance(event, RunControlEvent)
    ] == ["op-r1"]
    assert [entry.request_id for entry in inbox.rejected()] == ["r2"]
    assert not any(isinstance(event, SessionInputReceived) for event in events)


def test_a_request_whose_control_id_is_not_its_name_is_set_aside() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(PauseRequest(control_id="r2", expected_epoch=0)), ended=_LIVE)

    assert _bridge(inbox, _Texts()).poll(initial_state(), 1.0) == ()
    assert [entry.request_id for entry in inbox.rejected()] == ["r1"]


def test_a_settled_id_cannot_be_posted_again_with_another_request() -> None:
    """Regression: a re-post under a settled id was settled unread, so its options were lost."""
    inbox = FakeControlInbox()
    inbox.post("r1", _json(ResumeRequest(control_id="r1")), ended=_LIVE)
    bridge = _bridge(inbox, _Texts())
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    assert bridge.poll(core, 2.0) == ()
    assert inbox.pending() == ()

    with pytest.raises(ProjectStateError, match="different request"):
        inbox.post("r1", _json(ResumeRequest(control_id="r1", fallback=True)), ended=_LIVE)


def test_the_same_request_sent_again_after_it_settled_is_settled_again() -> None:
    inbox = FakeControlInbox()
    request = _json(PauseNowRequest(control_id="r1", expected_epoch=0))
    inbox.post("r1", request, ended=_LIVE)
    bridge = _bridge(inbox, _Texts())
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    bridge.poll(core, 2.0)

    inbox.post("r1", request, ended=_LIVE)
    bridge.poll(core, 3.0)

    assert inbox.pending() == ()
    assert inbox.rejected() == ()


def test_an_unreadable_entry_is_set_aside_and_the_rest_apply() -> None:
    inbox = FakeControlInbox()
    inbox.hold_unreadable("r1", "a dir")
    inbox.post("r2", _json(PauseRequest(control_id="r2", expected_epoch=0)), ended=_LIVE)

    core = _commit(initial_state(), _bridge(inbox, _Texts()).poll(initial_state(), 1.0))

    assert [c.control_id.root for c in core.run.controls] == ["op-r2"]
    assert [(e.request_id, e.reason) for e in inbox.rejected()] == [("r1", "unreadable: a dir")]
    assert [entry.request_id for entry in inbox.pending()] == ["r2"]  # settled at the next poll


class _RacingControl(ChannelRunControl):
    """A run control where a socket steer lands just after each inbox request it applies."""

    def submit(self, request: ControlRequest) -> ControlAck:
        ack = super().submit(request)
        if not request.control_id.startswith("s"):
            super().submit(SteerRequest(control_id=f"s-{request.control_id}", text="socket"))
        return ack


def test_a_socket_steer_in_the_same_poll_keeps_its_own_identity() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(SteerRequest(control_id="r1", text="from the inbox")), ended=_LIVE)
    texts = _Texts()
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    bridge = RunControlBridge(
        channel, texts, inbox=OperatorInbox(inbox, _RacingControl(channel, threads))
    )

    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    for restarted in (bridge, _bridge(inbox, texts)):
        core = _commit(core, restarted.poll(core, 2.0))

    assert sorted(c.control_id.root for c in core.run.controls) == ["op-r1", "op-s-r1"]
    assert texts.held["steer-op-r1"] == "from the inbox"
    assert inbox.pending() == ()


class _DamagedInbox(FakeControlInbox):
    """An inbox whose directory cannot be listed, as a symlinked ``inbox/`` would be."""

    def pending(self) -> tuple[InboxEntry, ...]:
        message = "VibeSys state path is a symlink"
        raise ProjectStateError(message)


def test_a_damaged_inbox_never_fails_the_run_and_is_reported_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a symlinked inbox directory raised out of the control poll."""
    inbox = _DamagedInbox()
    bridge, control = _wired(inbox, _Texts())
    control.submit(PauseRequest(control_id="k1", expected_epoch=0))

    with caplog.at_level(logging.WARNING):
        core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
        core = _commit(core, bridge.poll(core, 2.0))

    assert [c.control_id.root for c in core.run.controls] == ["op-k1"]
    assert [r.getMessage() for r in caplog.records if "control inbox" in r.getMessage()] == [
        "the run's control inbox cannot be read; its requests wait: VibeSys state path is a symlink"
    ]


def _ended(core: CoreState) -> CoreState:
    return core.model_copy(
        update={"run": core.run.model_copy(update={"status": RunStatus.TERMINAL})}
    )


def test_an_ended_run_settles_what_it_applied_and_rejects_the_rest_once() -> None:
    """A request a poster wrote before it saw the end, even one the loop never read, is set
    aside by ``end`` with one report; a post after the end is refused before it is written."""
    inbox = FakeControlInbox()
    sink = FakeRunControlEventSink()
    bridge, _control = _wired(inbox, _Texts(), sink)
    inbox.post("applied", _json(PauseRequest(control_id="applied", expected_epoch=0)), ended=_LIVE)
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    ended = _ended(core)
    # Core has committed the end; this poster read the record just before it did.
    inbox.post("late", _json(ResumeRequest(control_id="late")), ended=_LIVE)

    bridge.end(ended)
    bridge.end(ended)

    ending = "the run ended"
    assert inbox.pending() == ()
    assert inbox.status("applied", ended=ending) == RequestStatus(RequestState.APPLIED)
    late = inbox.status("late", ended=ending)
    assert late == RequestStatus(RequestState.REJECTED, ended_unapplied(ending))
    assert [
        t.summary for t in sink.transitions if t.kind is RunControlTransitionKind.REQUEST_REJECTED
    ] == [f"control request late rejected: {late.reason}"]
    with pytest.raises(InboxClosedError, match="was not posted"):
        inbox.post(
            "after", _json(PauseRequest(control_id="after", expected_epoch=0)), ended=lambda: ending
        )
    assert inbox.status("after", ended=ending) is None


def test_the_end_reports_what_a_poster_wrote_after_it_was_committed() -> None:
    """Regression guard for the sweep at the end: without it a request written between core's
    end and the sweep reads as rejected but is never reported to the run's journal."""
    inbox = FakeControlInbox()
    sink = FakeRunControlEventSink()
    bridge, _control = _wired(inbox, _Texts(), sink)
    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))
    polls = iter([None, "the run ended"])

    with pytest.raises(InboxClosedError, match="written as the run ended"):
        # The poster saw a live run, wrote, then saw the end core committed meanwhile.
        inbox.post(
            "racing",
            _json(PauseRequest(control_id="racing", expected_epoch=0)),
            ended=lambda: next(polls),
        )
    bridge.end(_ended(core))

    assert [
        t.request_id
        for t in sink.transitions
        if t.kind is RunControlTransitionKind.REQUEST_REJECTED
    ] == ["racing"]


def test_a_run_that_may_resume_keeps_its_inbox_open() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(PauseRequest(control_id="r1", expected_epoch=0)), ended=_LIVE)

    _bridge(inbox, _Texts()).end(initial_state())

    assert inbox.status("r1", ended=None) == RequestStatus(RequestState.PENDING)
    inbox.post("r2", _json(ResumeRequest(control_id="r2")), ended=_LIVE)


@pytest.mark.regression
@example(epoch=2, target=1)
@given(epoch=st.integers(1, 100), target=st.integers(0, 100))
def test_closing_requests_cannot_cross_an_epoch(epoch: int, target: int) -> None:
    inbox = FakeControlInbox()
    state = initial_state()
    state = state.model_copy(update={"records": state.records.model_copy(update={"epoch": epoch})})
    request = PauseNowRequest(control_id="close", expected_epoch=target)
    inbox.post("close", _json(request), ended=_LIVE)
    bridge = _bridge(inbox, _Texts())
    events = bridge.poll(state, 1.0)
    if epoch != target:
        assert not events
        assert len(inbox.rejected()) == 1
    else:
        assert len(events) == 1
        assert _commit(state, events).run.status is RunStatus.PARKED
