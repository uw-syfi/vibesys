"""Core records the run's pause as the channel holds it: who owns it and what ends it.

The bridge turns the channel's pause into pause and resume controls. After every poll whose
events core commits, ``run_pause`` over core's record equals the channel's ``pause_state``
and ``capacity_wait_stands`` agrees with the channel's, and an inbox request that changes
the pause settles only once core holds it. Core never refuses what the bridge emits.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentQuotaError,
    AgentTurnRequest,
    CapacityGate,
    CapacityStops,
    NullAgentEventSink,
    QuotaCondition,
)
from vs_agent.api.testing import FakeAgentClient
from vs_core.api import (
    ControlId,
    ControlInput,
    ControlRefusedError,
    CoreState,
    PauseOwner,
    RunControlEvent,
    RunPause,
    RunResultProposal,
    StopMode,
    capacity_wait_stands,
    initial_state,
    run_pause,
    step,
)
from vs_project.api.testing import FakeControlInbox
from vs_runtime.api.core import OperatorInbox, RunControlBridge
from vs_runtime.api.infrastructure import (
    CapacityHandling,
    ChannelRunControl,
    ControlAck,
    ControlRequest,
    PauseRequest,
    QuotaAction,
    QuotaPolicy,
    ResumeRequest,
    create_run_control_channel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, VirtualClock

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
    queued: list[CoreEvent] = field(default_factory=list)
    """Events of polls the shell has not committed yet, in order."""
    turns: int = 0
    """Turns waiting on a quota stop, parked for the operator."""

    def poll(self, *, commit: bool = True) -> tuple[CoreEvent, ...]:
        """Poll the bridge; ``commit=False`` leaves the events queued, as the shell may.

        With ``commit``, every queued event is committed, then the bridge hears that the
        shell settled, as the run loop tells it after each settle.
        """
        events: tuple[CoreEvent, ...] = self.bridge.poll(self.core, 1.0)
        self.queued += events
        if commit:
            self.commit()
        return events

    def commit(self) -> None:
        """Commit every queued event in order; core refusing one fails the test."""
        for event in self.queued:
            self.core = step(self.core, event).state
        self.queued = []
        self.bridge.settled(self.core, running=False)

    def stop_on_quota(self) -> None:
        """A turn stops on a quota under ``pause``: it enters, then pauses, as the gate does."""
        self.channel.enter_capacity()
        self.turns += 1
        self.channel.pause_for_quota()

    def cancel_turn(self) -> None:
        """The oldest waiting turn is cancelled and stops waiting."""
        if self.turns:
            self.turns -= 1
            self.channel.leave_capacity()

    def agrees(self) -> bool:
        """Core's record of the pause is the channel's."""
        return (run_pause(self.core.run), capacity_wait_stands(self.core.run)) == (
            self.channel.pause_state(),
            self.channel.capacity_wait_stands(),
        )


def _run() -> _Run:
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    inbox = FakeControlInbox()
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    return _Run(channel, control, bridge, inbox, initial_state())


def _restarted(run: _Run) -> _Run:
    """A new process over the same inbox and committed core state; queued events are lost."""
    fresh = _run()
    bridge = RunControlBridge(
        fresh.channel, _Texts(), inbox=OperatorInbox(run.inbox, fresh.control)
    )
    return _Run(fresh.channel, fresh.control, bridge, run.inbox, run.core)


type _Op = Literal[
    "quota",
    "cancel",
    "operator",
    "resume",
    "fallback",
    "capacity",
    "inbox-operator",
    "inbox-resume",
    "inbox-fallback",
    "inbox-capacity",
    "poll",
    "poll-queued",
    "commit",
    "restart",
]
_OPS: tuple[_Op, ...] = (
    "quota",
    "cancel",
    "operator",
    "resume",
    "fallback",
    "capacity",
    "inbox-operator",
    "inbox-resume",
    "inbox-fallback",
    "inbox-capacity",
    "poll",
    "poll-queued",
    "commit",
    "restart",
)


def _request(op: _Op, control_id: str) -> ControlRequest:
    match op.removeprefix("inbox-"):
        case "operator":
            return PauseRequest(control_id=control_id)
        case "fallback":
            return ResumeRequest(control_id=control_id, fallback=True)
        case "capacity":
            return ResumeRequest(control_id=control_id, when="capacity_returns")
        case _:
            return ResumeRequest(control_id=control_id)


def _apply(run: _Run, op: _Op, control_id: str) -> _Run:
    """Apply ``op``; the run it leaves (a restart is a new process)."""
    match op:
        case "quota":
            run.stop_on_quota()
        case "cancel":
            run.cancel_turn()
        case "operator" | "resume" | "fallback" | "capacity":
            run.control.submit(_request(op, control_id))  # through the socket's entry point
        case "inbox-operator" | "inbox-resume" | "inbox-fallback" | "inbox-capacity":
            run.inbox.post(control_id, _request(op, control_id).model_dump_json().encode())
        case "poll":
            run.poll()
        case "poll-queued":
            run.poll(commit=False)
        case "commit":
            run.commit()
        case "restart":
            return _restarted(run)
    return run


@given(st.lists(st.sampled_from(_OPS), max_size=30))
# The quota pause, and the inbox's capacity wait asked for under it, reach one poll: before,
# the bridge emitted the wait first and core, still running, refused it, ending the run.
# This only pins that core is never asked to refuse; that the wait is taken, not rejected,
# is pinned by the two dedicated tests below.
@example(["quota", "inbox-capacity", "poll"])
# The only waiting turn is cancelled under a capacity wait: before, the channel stayed paused
# with no turn and core kept a capacity wait nothing would end.
@example(["quota", "capacity", "poll", "cancel", "poll"])
# Held, a fallback resume, then another quota stop, all between two polls: before, core
# recorded the resume without its fallback.
@example(["operator", "quota", "poll", "fallback", "quota", "poll"])
# A restart under a quota pause with a capacity wait, then a second restart: before, the
# first poll erased the wait from core, so the second restart lost it.
@example(["quota", "capacity", "poll", "restart", "poll", "restart", "poll"])
def test_after_each_committed_poll_core_holds_the_channels_pause(ops: list[_Op]) -> None:
    """Socket and inbox controls, quota stops and cancelled turns in any order and any mix
    of committed and queued polls, with restarts. Core never refuses what the bridge emits,
    and after every poll the shell committed, core's record of the pause is the channel's.
    In the end every inbox request is answered: settled or rejected with a reason.
    """
    run = _run()
    for index, op in enumerate(ops):
        run = _apply(run, op, f"c{index}")
        if op == "poll":
            assert run.agrees()
    run.poll()
    run.poll()
    assert run.agrees()
    assert run.inbox.pending() == ()


def test_a_capacity_wait_from_the_inbox_settles_only_once_core_records_it() -> None:
    run = _run()
    run.channel.enter_capacity()
    run.channel.pause_for_quota()
    run.poll()
    request = ResumeRequest(control_id="w1", when="capacity_returns")
    run.inbox.post("w1", request.model_dump_json().encode())

    run.poll(commit=False)  # applied, but core has not committed it yet
    run.poll(commit=False)
    assert [entry.request_id for entry in run.inbox.pending()] == ["w1"]

    run.commit()
    run.poll()

    assert run.inbox.pending() == ()
    assert run.inbox.rejected() == ()
    assert run_pause(run.core.run) == RunPause(owner=PauseOwner.QUOTA, capacity_wait=True)


def test_a_capacity_wait_asked_in_the_poll_that_first_sees_the_quota_pause_is_taken() -> None:
    """Regression (P1 of the #3091 review): the quota pause and the operator's wait for
    capacity under it reach the bridge in one poll, the ordinary flow since a pause does not
    wake the run loop. The wait is judged against the pause, not against the run as core
    last committed it; before, core refused it and the run ended.
    """
    run = _run()
    run.stop_on_quota()
    request = ResumeRequest(control_id="w1", when="capacity_returns")
    run.inbox.post("w1", request.model_dump_json().encode())

    run.poll()
    run.poll()

    assert run.inbox.rejected() == ()
    assert run.inbox.pending() == ()
    assert run_pause(run.core.run) == RunPause(owner=PauseOwner.QUOTA, capacity_wait=True)
    assert run.agrees()


def test_a_fallback_resume_of_a_hold_a_quota_stop_paused_again_keeps_its_fallback() -> None:
    """Regression (finding 5 of the #3091 review): the operator lifts a hold with
    ``--fallback`` and a quota stop pauses the run again before the next poll. Core
    records the resume with its fallback, as it does when the polls see them apart.
    """
    run = _run()
    run.control.submit(PauseRequest(control_id="hold"))
    run.stop_on_quota()
    run.poll()
    run.control.submit(ResumeRequest(control_id="go", fallback=True))
    run.stop_on_quota()
    run.poll()

    resumes = [control for control in run.core.run.controls if control.action == "resume"]
    assert [control.fallback for control in resumes] == [True]
    assert run.agrees()


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


@given(ops=st.lists(st.sampled_from(_OPS), max_size=16))
# A capacity wait asked for under the operator's hold of a quota-paused run: before (finding
# 9 of the #3091 review), the restart took the capacity wait first and the run came back running.
@example(ops=["quota", "operator", "capacity", "poll"])
def test_a_restart_keeps_an_operator_pause_and_resumes_only_a_quota_pause(
    *, ops: list[_Op]
) -> None:
    """Regression (#3080): a run the operator paused came back running after a restart.

    The operator's hold stays, with or without a capacity wait asked for under it. A quota
    pause resumes; the operator's wait for capacity on it stands, in core too, until the
    next quota stop takes it, across any number of restarts.
    """
    run = _run()
    for index, op in enumerate(ops):
        run = _apply(run, op, f"c{index}")
    run.poll()
    before = run_pause(run.core.run)
    stood = capacity_wait_stands(run.core.run) or (before is not None and before.capacity_wait)

    restarted = _restarted(run)
    restarted.poll()
    restarted = _restarted(restarted)
    restarted.poll()

    held = before is not None and before.owner is PauseOwner.OPERATOR
    expected = RunPause(owner=PauseOwner.OPERATOR) if held else None
    assert restarted.channel.pause_state() == expected
    assert restarted.agrees()
    assert capacity_wait_stands(restarted.core.run) is (stood and not held)
    if stood and not held:
        # The operator asked to wait for capacity: the next capacity stop waits, unasked.
        restarted.stop_on_quota()
        assert isinstance(restarted.channel.park_for_capacity(), int)
        restarted.poll()
        assert run_pause(restarted.core.run) == RunPause(owner=PauseOwner.QUOTA, capacity_wait=True)
        assert restarted.agrees()


def test_an_inbox_request_core_refuses_at_commit_is_answered_with_cores_reason() -> None:
    """The bridge judged an inbox pause against a running run, then the run ended first.

    An input committed ahead of the pause (the run loop's stop of a run whose recovery is
    blocked, with a result) ends the run, and core refuses the pause. The shell hands the
    refusal to ``settled``, which rejects the entry with core's reason and lifts the hold
    the request put on the channel, with no later poll: the run loop makes none after the
    run ends. The run loop's own test is ``tests/system/run_loop/test_refused_controls.py``.
    """
    run = _run()
    run.poll()
    run.inbox.post("p1", PauseRequest(control_id="p1").model_dump_json().encode())
    (pause,) = run.bridge.poll(run.core, 1.0)
    blocked = RunControlEvent(
        control=ControlInput(
            control_id=ControlId(root="recovery-blocked"), action="stop", mode=StopMode.CANCEL
        ),
        result=RunResultProposal(outcome="blocked", reason="recovery blocked"),
        now_at=1.0,
    )
    run.core = step(run.core, blocked).state
    with pytest.raises(ControlRefusedError) as refused:
        step(run.core, pause)  # what the run loop sets aside
    run.bridge.settled(run.core, running=False, refused=(refused.value,))

    assert run.inbox.pending() == ()
    assert [(entry.request_id, entry.reason) for entry in run.inbox.rejected()] == [
        ("p1", "refused by the run: terminal run rejects controls")
    ]
    assert run.channel.pause_state() is None


class _QuotaWhileRead(ChannelRunControl):
    """Run controls where a turn stops on a quota just after the first request is applied.

    It stands for a turn's thread pausing the run while the bridge reads the inbox.
    """

    def __init__(self, channel: RunControlChannel, threads: SimThreads) -> None:
        super().__init__(channel, threads)
        self.channel = channel
        self.stopped = False

    def submit(self, request: ControlRequest) -> ControlAck:
        ack = super().submit(request)
        if not self.stopped:
            self.stopped = True
            self.channel.enter_capacity()
            self.channel.pause_for_quota()
        return ack


def test_each_inbox_request_is_judged_against_the_pause_the_run_is_in_by_then() -> None:
    """A quota stop between two inbox requests of one poll reaches core before the second.

    The first is a resume of a running run, which changes nothing; the second asks to wait
    for capacity, which the run takes only while paused on the quota. Before (the per-entry half of P1 of the #3091 review), it was judged against
    the pause at the start of the poll, and rejected.
    """
    threads = SimThreads()
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = _QuotaWhileRead(channel, threads)
    inbox = FakeControlInbox()
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    run = _Run(channel, control, bridge, inbox, initial_state())
    run.inbox.post("a1", ResumeRequest(control_id="a1").model_dump_json().encode())
    wait = ResumeRequest(control_id="a2", when="capacity_returns")
    run.inbox.post("a2", wait.model_dump_json().encode())

    run.poll()

    assert run.inbox.rejected() == ()
    assert run.inbox.pending() == ()
    assert run_pause(run.core.run) == RunPause(owner=PauseOwner.QUOTA, capacity_wait=True)
    assert run.agrees()


class _GateClient(FakeAgentClient):
    """The fake client, keeping the gate the run gave it so the test can stop a turn on it."""

    gate: CapacityGate | None = None

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        super().set_capacity_gate(gate)
        self.gate = gate


@given(seed=st.integers(0, 2**16), cancel_after=st.integers(1, 600).map(float))
def test_cancelling_the_turn_that_waits_for_capacity_ends_the_pause_in_core_too(
    seed: int, cancel_after: float
) -> None:
    """The real capacity gate, channel, bridge and core, under the ``pause`` policy.

    A turn stops on a quota, the operator asks through the inbox to resume when capacity
    returns (far later), and the turn is cancelled while it waits. The run resumes itself,
    and core records that. Before, the channel stayed paused with no turn to end the pause,
    and core kept a capacity wait nothing would ever end.
    """
    clock = VirtualClock(1_000_000.0, limit=1_000_000.0 + 100_000.0)
    threads = SimThreads(clock, schedule_seed=seed)
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control = ChannelRunControl(channel, threads)
    inbox = FakeControlInbox()
    bridge = RunControlBridge(channel, _Texts(), inbox=OperatorInbox(inbox, control))
    run = _Run(channel, control, bridge, inbox, initial_state())
    client = _GateClient(threads=threads)
    policy = QuotaPolicy(QuotaAction.PAUSE, retry_seconds=50_000.0)
    install_capacity_gate(
        client,
        control=channel,
        events=NullAgentEventSink(),
        handling=CapacityHandling.for_policy(policy, threads),
        clock=clock,
    )
    gate = client.gate
    assert gate is not None
    stops = CapacityStops(threads)

    def turn() -> None:
        error = AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, "You've hit your limit")
        with contextlib.suppress(AgentQuotaError):
            gate.wait_for_capacity(
                error, AgentTurnRequest(message="m", invocation_id="t"), role="r", stops=stops
            )

    def main() -> None:
        worker = threads.spawn(turn, name="turn", daemon=False)
        threads.sleep(1.0)
        run.poll()
        assert run_pause(run.core.run) == RunPause(owner=PauseOwner.QUOTA)
        request = ResumeRequest(control_id="w1", when="capacity_returns")
        run.inbox.post("w1", request.model_dump_json().encode())
        run.poll()
        assert run_pause(run.core.run) == RunPause(owner=PauseOwner.QUOTA, capacity_wait=True)
        threads.sleep(cancel_after)
        stops.cancel()
        worker.join(HANG_GUARD_S)
        assert not worker.is_alive()
        run.poll()

    threads.run(main)

    assert channel.pause_state() is None
    assert run_pause(run.core.run) is None
    assert run.agrees()
    assert inbox.pending() == ()
    assert inbox.rejected() == ()
