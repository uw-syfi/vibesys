"""The run's pause has two owners, and neither undoes the other (issue #3079).

The operator holds the run with ``request_pause`` and only an operator resume lifts the hold.
A turn stopped on a quota pauses the run through the capacity gate, which may arm a timed
resume. Before, both called ``request_pause``, so the channel could not tell them apart: a
quota deadline lifted an operator pause, and any quota stop dropped the operator's request to
resume when capacity returns.

The gate runs on its production timer over simulated threads, so every wait is the production
wait on virtual time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentQuotaError,
    AgentTurnRequest,
    Attribution,
    CapacityGate,
    CapacityStops,
    NullAgentEventSink,
    QuotaCondition,
)
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api.infrastructure import (
    CapacityHandling,
    PauseWake,
    QuotaAction,
    QuotaPolicy,
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import HANG_GUARD_S, SimThreads, VirtualClock

if TYPE_CHECKING:
    from vs_agent.api import QuotaWaitCancelReason

ROLE = "dynamic-orchestrator"
START = 1_000_000.0
END = 2_000.0
"""Virtual seconds after ``START`` by which every deadline in these tests has passed."""


class _Sink(NullAgentEventSink):
    """Remembers how each turn's quota stop ended, and every cancelled wait for capacity."""

    def __init__(self) -> None:
        self.resumed: dict[str | None, str] = {}
        self.wait_cancelled: list[QuotaWaitCancelReason] = []

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, waited_seconds
        self.resumed[where.invocation_id] = reason

    def quota_wait_cancelled(
        self, provider: str, reason: QuotaWaitCancelReason, where: Attribution
    ) -> None:
        del provider, where
        self.wait_cancelled.append(reason)


class _Capture(FakeAgentClient):
    """The fake client, keeping the gate the run gave it so the test can stop turns on it."""

    gate: CapacityGate | None = None

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        super().set_capacity_gate(gate)
        self.gate = gate


@dataclass
class _Run:
    """One run's control channel and capacity gate on simulated threads, seeded schedule."""

    policy: QuotaPolicy
    seed: int
    clock: VirtualClock = field(default_factory=lambda: VirtualClock(START, limit=START + 4 * END))
    transitions: list[RunControlTransition] = field(default_factory=list)
    sink: _Sink = field(default_factory=_Sink)
    threads: SimThreads = field(init=False)
    control: RunControlChannel = field(init=False)
    gate: CapacityGate = field(init=False)

    def __post_init__(self) -> None:
        self.threads = SimThreads(self.clock, schedule_seed=self.seed)
        self.control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self.transitions.append), threads=self.threads
        )
        capture = _Capture(threads=self.threads)
        install_capacity_gate(
            capture,
            control=self.control,
            events=self.sink,
            handling=CapacityHandling.for_policy(self.policy, self.threads),
            clock=self.clock,
        )
        assert capture.gate is not None
        self.gate = capture.gate

    def stop(self, turn: str, stops: CapacityStops, resets_in: float | None) -> None:
        """One quota stop of ``turn``; returns when the turn may be sent again, or it ended."""
        resets_at = None if resets_in is None else self.clock.now() + resets_in
        error = AgentQuotaError(
            "claude", QuotaCondition.QUOTA_EXHAUSTED, "You've hit your limit", resets_at
        )
        stops.count += 1
        try:
            self.gate.wait_for_capacity(
                error, AgentTurnRequest(message="m", invocation_id=turn), role=ROLE, stops=stops
            )
        except (AgentQuotaError, RunStopped):
            return

    def resumed_since(self, index: int) -> int:
        """How many times the run resumed after the first ``index`` transitions."""
        kinds = [t.kind for t in self.transitions[index:]]
        return kinds.count(RunControlTransitionKind.RESUMED)


@dataclass(frozen=True)
class _QuotaTurn:
    """A turn that stops on a quota ``at`` seconds in, cancelled ``cancel_after`` later."""

    at: float
    resets_in: float | None
    cancel_after: float | None


_TURNS = st.lists(
    st.builds(
        _QuotaTurn,
        at=st.integers(0, 60).map(float),
        resets_in=st.none() | st.integers(1, 400).map(float),
        cancel_after=st.none() | st.integers(0, 120).map(float),
    ),
    min_size=1,
    max_size=4,
)


@given(
    turns=_TURNS,
    pause_at=st.integers(0, 60).map(float),
    seed=st.integers(0, 2**16),
)
def test_an_operator_pause_holds_under_any_mix_of_quota_waits_cancels_and_deadlines(
    turns: list[_QuotaTurn], pause_at: float, seed: int
) -> None:
    """From the operator's pause until the operator resumes, the run stays paused.

    Turns stop on a quota before or after the pause, under a policy that waits for each
    turn's reset (or its retry interval), and some are cancelled mid-wait. The run loop
    polls the channel throughout, as it does, so every timed resume gets its chance to fire.
    Before, the first quota deadline to come due resumed the run under the operator's pause.
    """
    run = _Run(QuotaPolicy(QuotaAction.WAIT, retry_seconds=90.0), seed)
    threads, control = run.threads, run.control
    held_from: list[int] = []
    polls_while_held: list[bool] = []

    def turn(index: int, spec: _QuotaTurn) -> None:
        stops = CapacityStops(threads)
        threads.sleep(spec.at)
        if spec.cancel_after is not None:
            threads.spawn(lambda: _cancel_later(spec.cancel_after, stops), name=f"cancel-{index}")
        run.stop(f"turn-{index}", stops, spec.resets_in)

    def _cancel_later(after: float | None, stops: CapacityStops) -> None:
        threads.sleep(after or 0.0)
        stops.cancel()

    def operator() -> None:
        threads.sleep(pause_at)
        control.request_pause()
        held_from.append(len(run.transitions))

    def run_loop() -> None:
        while threads.now() < START + END:
            paused = control.pause_requested()
            if held_from:
                polls_while_held.append(paused)
            threads.sleep(7.0)

    def main() -> tuple[bool, int]:
        workers = [
            threads.spawn(lambda i=i, s=s: turn(i, s), name=f"turn-{i}", daemon=False)
            for i, s in enumerate(turns)
        ]
        workers.append(threads.spawn(operator, name="operator", daemon=False))
        workers.append(threads.spawn(run_loop, name="run-loop", daemon=False))
        threads.sleep(END + 1.0)
        held_at_end = control.pause_requested()
        resumed_under_hold = run.resumed_since(held_from[0])
        control.resume()
        for worker in workers:
            worker.join(HANG_GUARD_S)
        return held_at_end, resumed_under_hold

    held_at_end, resumed_under_hold = threads.run(main)

    assert held_at_end
    assert resumed_under_hold == 0
    assert all(polls_while_held)
    # No turn still waiting under the hold was released by its own deadline.
    assert "wait_elapsed" not in {
        reason
        for index, spec in enumerate(turns)
        if (reason := run.sink.resumed.get(f"turn-{index}")) and spec.at >= pause_at
    }


@given(
    resets_in=st.integers(200, 1_000).map(float),
    others=st.lists(st.integers(2, 150).map(float), min_size=1, max_size=3),
    seed=st.integers(0, 2**16),
)
def test_an_operator_capacity_request_survives_other_turns_quota_stops(
    resets_in: float, others: list[float], seed: int
) -> None:
    """Under the ``pause`` policy the operator asks the run to resume when capacity returns.

    Other turns then stop on the quota while the first turn waits. Each joins the wait under
    the operator's request, and the run resumes itself once, when the first turn's capacity
    returns. Before, each later stop dropped the request, so the waiting turn reported its
    wait cancelled and went back to waiting for the operator.
    """
    run = _Run(QuotaPolicy(QuotaAction.PAUSE, retry_seconds=10_000.0), seed)
    threads, control = run.threads, run.control
    asked: list[bool] = []

    def turn(name: str, at: float, resets: float | None) -> None:
        threads.sleep(at)
        run.stop(name, CapacityStops(threads), resets)

    def operator() -> None:
        threads.sleep(1.0)
        asked.append(control.resume_when_capacity_returns())

    def main() -> tuple[bool, int]:
        workers = [threads.spawn(lambda: turn("first", 0.0, resets_in), name="first")]
        workers += [
            threads.spawn(lambda i=i, at=at: turn(f"other-{i}", at, None), name=f"other-{i}")
            for i, at in enumerate(others)
        ]
        workers.append(threads.spawn(operator, name="operator"))
        threads.sleep(END + 1.0)
        paused = control.pause_requested()
        resumed = run.resumed_since(0)
        control.resume()
        for worker in workers:
            worker.join(HANG_GUARD_S)
        return paused, resumed

    paused_at_end, resumed = threads.run(main)

    assert asked == [True]
    assert run.sink.wait_cancelled == []
    assert not paused_at_end
    assert run.sink.resumed["first"] == "wait_elapsed"
    assert set(run.sink.resumed.values()) == {"wait_elapsed"}
    assert resumed == 1


@given(
    stops_at=st.lists(st.integers(0, 60).map(float), min_size=1, max_size=4),
    cancels_after=st.lists(st.integers(1, 120).map(float), min_size=4, max_size=4),
    ask_at=st.none() | st.integers(0, 90).map(float),
    seed=st.integers(0, 2**16),
)
def test_a_quota_pause_ends_with_the_last_turn_that_waited_on_it(
    stops_at: list[float], cancels_after: list[float], ask_at: float | None, seed: int
) -> None:
    """Under ``pause``, turns stop on a quota and are all cancelled while they wait.

    The operator may ask the run to resume when capacity returns (a wait far longer than
    the test) before or after some turns stop. Once the last waiting turn is cancelled, no
    turn owns the quota pause, and the run resumes itself, as a restart would resume it.
    Before, the run stayed paused with no turn, no timer and no request to end the pause.
    """
    run = _Run(QuotaPolicy(QuotaAction.PAUSE, retry_seconds=10_000.0), seed)
    threads, control = run.threads, run.control

    def turn(index: int, at: float) -> None:
        stops = CapacityStops(threads)
        threads.sleep(at)
        threads.spawn(lambda: _cancel_later(cancels_after[index], stops), name=f"cancel-{index}")
        run.stop(f"turn-{index}", stops, None)

    def _cancel_later(after: float, stops: CapacityStops) -> None:
        threads.sleep(after)
        stops.cancel()

    def operator() -> None:
        if ask_at is not None:
            threads.sleep(ask_at)
            control.resume_when_capacity_returns()

    def main() -> bool:
        workers = [
            threads.spawn(lambda i=i, at=at: turn(i, at), name=f"turn-{i}", daemon=False)
            for i, at in enumerate(stops_at)
        ]
        workers.append(threads.spawn(operator, name="operator", daemon=False))
        for worker in workers:
            worker.join(END)
        assert not any(worker.is_alive() for worker in workers), "a cancelled turn stayed parked"
        return control.pause_requested()

    paused = threads.run(main)

    assert not paused
    assert run.transitions[-1].kind is RunControlTransitionKind.RESUMED


_OPERATOR_ACTIONS = st.lists(st.sampled_from(["pause", "resume"]), min_size=2, max_size=6)


@given(actions=_OPERATOR_ACTIONS, seed=st.integers(0, 2**16))
def test_the_transition_stream_ends_in_the_state_the_channel_is_in(
    actions: list[str], seed: int
) -> None:
    """Controls sent from several threads at once publish their transitions in state order.

    The sink takes a lock of its own, as the run's event journal does. A consumer folding
    the stream (the last ``PAUSE_REQUESTED`` or ``RESUMED`` wins) must end where the channel
    is. Before, ``RESUMED`` was published after the lock was released, so a pause that
    landed meanwhile could be followed by a stale ``RESUMED``.
    """
    threads = SimThreads(schedule_seed=seed)
    journal = threads.lock()
    folded: list[RunControlTransitionKind] = []

    def sink(transition: RunControlTransition) -> None:
        with journal:
            folded.append(transition.kind)

    control = create_run_control_channel(sink, threads=threads)

    def main() -> bool:
        workers = [
            threads.spawn(
                control.request_pause if action == "pause" else control.resume,
                name=f"{action}-{index}",
                daemon=False,
            )
            for index, action in enumerate(actions)
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)
        return control.pause_requested()

    paused = threads.run(main)

    pause_kinds = [
        kind
        for kind in folded
        if kind in {RunControlTransitionKind.PAUSE_REQUESTED, RunControlTransitionKind.RESUMED}
    ]
    assert (pause_kinds[-1] is RunControlTransitionKind.PAUSE_REQUESTED) is paused


@given(seconds=st.integers(1, 500).map(float), seed=st.integers(0, 2**16))
def test_a_parked_turn_reports_the_resume_cause_the_channel_recorded(
    seconds: float, seed: int
) -> None:
    """A turn parked for the operator learns that the run's timed resume, not the operator, ended it.

    Before, the gate reported every resume that ended such a park as the operator's.
    """
    threads = SimThreads(schedule_seed=seed)
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control.enter_capacity()

    def main() -> object:
        control.pause_for_quota(seconds)
        return control.park_for_capacity()

    assert threads.run(main) is PauseWake.ELAPSED
    control.leave_capacity()


def test_leaving_capacity_more_often_than_entering_it_is_a_bug_not_a_clamp() -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    control.enter_capacity()
    control.leave_capacity()

    with pytest.raises(RuntimeError, match="without a matching enter_capacity"):
        control.leave_capacity()
