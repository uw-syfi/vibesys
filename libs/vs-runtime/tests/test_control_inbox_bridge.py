"""The run applies each request in its control inbox exactly once, across crashes and resumes.

A generated history interleaves posting requests, control polls whose events core commits,
polls whose events are lost (the process died before core committed them), and restarts (a
new bridge and channel over the same inbox and core state). Whatever the history, once the
run settles every request is either recorded by core under ``op-<control id>`` exactly once
or set aside with a reason, never both, and nothing stays pending.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    ArtifactRef,
    CoreState,
    RunControlEvent,
    RunStatus,
    SessionInputReceived,
    StopMode,
    initial_state,
    step,
)
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    PauseRequest,
    ResumeRequest,
    SteerRequest,
    StopRequest,
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


def _bridge(inbox: FakeControlInbox, texts: _Texts) -> RunControlBridge:
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    return RunControlBridge(
        channel, texts, inbox=OperatorInbox(inbox, ChannelRunControl(channel, threads))
    )


def _payload(request: dict[str, object] | bytes, control_id: str) -> bytes:
    """The file a sender writes: a request named by its file name, or raw bytes."""
    if isinstance(request, bytes):
        return request
    return json.dumps({"control_id": control_id, **request}).encode()


_PAYLOADS: st.SearchStrategy[dict[str, object] | bytes] = st.one_of(
    st.builds(lambda text: {"action": "steer", "text": text}, st.text(min_size=1, max_size=12)),
    st.just({"action": "pause"}),
    st.just({"action": "resume"}),
    st.just({"action": "resume", "fallback": True}),
    st.just({"action": "resume", "when": "capacity_returns"}),
    st.builds(lambda mode: {"action": "stop", "mode": mode.value}, st.sampled_from(StopMode)),
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

_STEPS = st.lists(
    st.one_of(
        st.tuples(st.just("post"), _PAYLOADS),
        st.tuples(st.just("poll")),
        st.tuples(st.just("lost poll")),
        st.tuples(st.just("restart")),
    ),
    max_size=24,
)


def _json(request: BaseModel) -> bytes:
    return request.model_dump_json().encode()


def _commit(core: CoreState, events: tuple[CoreEvent, ...]) -> CoreState:
    for event in events:
        core = step(core, event).state
    return core


@given(_STEPS)
def test_every_inbox_request_is_recorded_once_or_set_aside(
    history: list[tuple[object, ...]],
) -> None:
    inbox = FakeControlInbox()
    texts = _Texts()
    core = initial_state()
    bridge = _bridge(inbox, texts)
    posted: list[str] = []
    for index, operation in enumerate(history):
        match operation:
            case ("post", dict() | bytes() as payload):
                request_id = f"r{index:03d}"
                inbox.post(request_id, _payload(payload, request_id))
                posted.append(request_id)
            case ("poll",):
                core = _commit(core, bridge.poll(core, float(index)))
            case ("lost poll",):
                bridge.poll(core, float(index))
                bridge = _bridge(inbox, texts)
            case ("restart",):
                bridge = _bridge(inbox, texts)
    for settle in range(3):
        core = _commit(core, bridge.poll(core, 1000.0 + settle))

    recorded = [control.control_id.root for control in core.run.controls]
    rejected = {entry.request_id for entry in inbox.rejected()}
    assert inbox.pending() == ()
    assert len(recorded) == len(set(recorded))
    for request_id in posted:
        assert (f"op-{request_id}" in recorded) != (request_id in rejected), request_id
    steer_controls = {
        control.control_id.root for control in core.run.controls if control.action == "steer"
    }
    steer_inputs = {row.input.input_id.root for row in core.sessions.inputs}
    assert {f"steer-{identity}" for identity in steer_controls} == steer_inputs


def test_a_request_that_is_not_a_control_is_set_aside_with_its_reason() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", b'{"control_id": "r1", "action": "pause", "extra": 1}')
    bridge = _bridge(inbox, _Texts())

    assert bridge.poll(initial_state(), 1.0) == ()
    (entry,) = inbox.rejected()
    assert entry.request_id == "r1"
    assert "extra" in entry.reason


def test_a_capacity_wait_with_no_capacity_pause_is_set_aside() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(ResumeRequest(control_id="r1", when="capacity_returns")))

    assert _bridge(inbox, _Texts()).poll(initial_state(), 1.0) == ()
    assert [entry.reason for entry in inbox.rejected()] == ["refused: not_paused_on_capacity"]


def test_inbox_requests_apply_in_id_order_as_their_actions_say() -> None:
    inbox = FakeControlInbox()
    inbox.post("r2", _json(SteerRequest(control_id="r2", text="prefer latency")))
    inbox.post("r1", _json(PauseRequest(control_id="r1")))
    inbox.post("r3", _json(ResumeRequest(control_id="r3")))
    texts = _Texts()
    bridge = _bridge(inbox, texts)

    core = _commit(initial_state(), bridge.poll(initial_state(), 1.0))

    assert [(c.control_id.root, c.action) for c in core.run.controls] == [
        ("op-r1", "pause"),
        ("op-r2", "steer"),
        ("op-r3", "resume"),
    ]
    assert core.run.status == RunStatus.RUNNING
    assert list(texts.held.values()) == ["prefer latency"]
    (steer,) = core.sessions.inputs
    assert steer.input.input_id.root == "steer-op-r2"
    assert steer.input.sequence == 2
    # Core holds all three, so the next poll settles them and applies nothing again.
    assert bridge.poll(core, 2.0) == ()
    assert inbox.pending() == ()


def test_a_pause_after_a_stop_is_set_aside() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(StopRequest(control_id="r1")))
    inbox.post("r2", _json(PauseRequest(control_id="r2")))

    events = _bridge(inbox, _Texts()).poll(initial_state(), 1.0)

    assert [
        event.control.control_id.root for event in events if isinstance(event, RunControlEvent)
    ] == ["op-r1"]
    assert [entry.request_id for entry in inbox.rejected()] == ["r2"]
    assert not any(isinstance(event, SessionInputReceived) for event in events)


def test_a_request_whose_control_id_is_not_its_name_is_set_aside() -> None:
    inbox = FakeControlInbox()
    inbox.post("r1", _json(PauseRequest(control_id="r2")))

    assert _bridge(inbox, _Texts()).poll(initial_state(), 1.0) == ()
    assert [entry.request_id for entry in inbox.rejected()] == ["r1"]
