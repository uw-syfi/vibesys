"""Core records the run's pause as the channel holds it: who owns it and what ends it.

The bridge turns the channel's pause into pause and resume controls. After every poll whose
events core commits, ``run_pause`` over core's record equals the channel's ``pause_state``,
and an inbox request that changes the pause settles only once core holds it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import CoreState, PauseOwner, RunPause, initial_state, run_pause, step
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    ChannelRunControl,
    ResumeRequest,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from vs_core.api import ArtifactRef, CoreEvent
    from vs_runtime.api.infrastructure import RunControlChannel


@dataclass
class _Texts:
    held: dict[str, str] = field(default_factory=dict)

    def put(self, ref: ArtifactRef, text: str) -> None:
        self.held[ref.artifact_id.root] = text


@dataclass
class _Run:
    channel: RunControlChannel
    control: ChannelRunControl
    bridge: RunControlBridge
    inbox: FakeControlInbox
    core: CoreState

    def poll(self, *, commit: bool = True) -> tuple[CoreEvent, ...]:
        """Poll the bridge; ``commit=False`` leaves the events queued, as the shell may."""
        events: tuple[CoreEvent, ...] = self.bridge.poll(self.core, 1.0)
        if commit:
            self.commit(events)
        return events

    def commit(self, events: tuple[CoreEvent, ...]) -> None:
        for event in events:
            self.core = step(self.core, event).state


def _run() -> _Run:
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    inbox = FakeControlInbox()
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    return _Run(channel, control, bridge, inbox, initial_state())


type _Op = Literal["quota", "operator", "resume", "fallback", "capacity", "poll"]
_OPS: tuple[_Op, ...] = ("quota", "operator", "resume", "fallback", "capacity", "poll")


def _apply(run: _Run, op: _Op, control_id: str) -> None:
    match op:
        case "quota":
            run.channel.pause_for_quota()
        case "operator":
            run.channel.request_pause(control_id)
        case "resume":
            run.channel.resume(control_id)
        case "fallback":
            run.channel.resume_with_fallback(control_id)
        case "capacity":
            run.channel.resume_when_capacity_returns(control_id)
        case "poll":
            run.poll()


@given(st.lists(st.sampled_from(_OPS), max_size=24))
def test_after_each_committed_poll_core_holds_the_channels_pause(ops: list[_Op]) -> None:
    run = _run()
    # One turn waits on a capacity limit throughout, so a capacity wait can be asked for.
    run.channel.enter_capacity()
    for index, op in enumerate(ops):
        _apply(run, op, f"c{index}")
        if op == "poll":
            assert run_pause(run.core.run) == run.channel.pause_state()
    run.poll()
    assert run_pause(run.core.run) == run.channel.pause_state()


def test_a_capacity_wait_from_the_inbox_settles_only_once_core_records_it() -> None:
    run = _run()
    run.channel.enter_capacity()
    run.channel.pause_for_quota()
    run.poll()
    request = ResumeRequest(control_id="w1", when="capacity_returns")
    run.inbox.post("w1", request.model_dump_json().encode())

    queued = run.poll(commit=False)  # applied, but core has not committed it yet
    run.poll()
    assert [entry.request_id for entry in run.inbox.pending()] == ["w1"]

    run.commit(queued)
    run.poll()

    assert run.inbox.pending() == ()
    assert run.inbox.rejected() == ()
    assert run_pause(run.core.run) == RunPause(owner=PauseOwner.QUOTA, capacity_wait=True)


def test_a_fallback_resume_reusing_a_plain_resumes_id_is_rejected_not_settled() -> None:
    run = _run()
    run.channel.pause_for_quota()
    run.poll()
    run.control.submit(ResumeRequest(control_id="x"))
    run.poll()
    run.channel.pause_for_quota()
    run.poll()

    payload = {"control_id": "x", "action": "resume", "fallback": True}
    run.inbox.post("x", json.dumps(payload).encode())
    run.poll()

    assert [entry.request_id for entry in run.inbox.rejected()] == ["x"]
    assert run.channel.consume_fallback_request() is False


def _restarted(run: _Run) -> _Run:
    """A new process over the same inbox and committed core state."""
    fresh = _run()
    return _Run(fresh.channel, fresh.control, _bridge_over(fresh, run.inbox), run.inbox, run.core)


def _bridge_over(fresh: _Run, inbox: FakeControlInbox) -> RunControlBridge:
    return RunControlBridge(fresh.channel, _Texts(), inbox=OperatorInbox(inbox, fresh.control))


@given(ops=st.lists(st.sampled_from(_OPS), max_size=16), capacity_turn=st.booleans())
def test_a_restart_keeps_an_operator_pause_and_resumes_only_a_quota_pause(
    *, ops: list[_Op], capacity_turn: bool
) -> None:
    """Regression (#3080): a run the operator paused came back running after a restart."""
    run = _run()
    if capacity_turn:
        run.channel.enter_capacity()
    for index, op in enumerate(ops):
        _apply(run, op, f"c{index}")
    run.poll()
    before = run_pause(run.core.run)

    restarted = _restarted(run)
    restarted.poll()
    restarted.poll()

    held = before is not None and before.owner is PauseOwner.OPERATOR and not before.capacity_wait
    expected = RunPause(owner=PauseOwner.OPERATOR) if held else None
    assert restarted.channel.pause_state() == expected
    assert run_pause(restarted.core.run) == expected
    if before is not None and before.capacity_wait:
        # The operator asked to wait for capacity: the next capacity stop waits, unasked.
        restarted.channel.enter_capacity()
        restarted.channel.pause_for_quota()
        assert isinstance(restarted.channel.park_for_capacity(), int)
