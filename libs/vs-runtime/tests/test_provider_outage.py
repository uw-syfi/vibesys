"""A provider outage holds only the turn that hit it, which tries again with backoff until it ends.

The regression these guard: a gateway answering ``503 No available accounts`` for hours made
every agent turn fail once agentshim's own retries ran out, and six failed planner turns ended a
live run. Now such a failure is a capacity stop (``QuotaCondition.UNAVAILABLE``) that the run's
capacity gate waits out under the outage budget, whatever the quota policy says. The wait is the
turn's own: it never pauses the run, so the rest of the run (measurements, other turns) goes on,
and an operator's pause, another turn's quota pause, a stop, a cancellation of the turn and the
run's deadline all keep their meaning while it lasts.

The provider is the scripted one behind a real ``AgentClient``, so the failure travels the
production path: a transient ``agentshim`` failure with no exhausted window, classified by the
client. The threads are simulated and the gate runs on its production timer over the simulated
clock, so nothing sleeps or reads real time, and the waits are the production waits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AgentQuotaError,
    AgentTurnRequest,
    Attribution,
    CapacityGate,
    CapacityStops,
    NullAgentEventSink,
    QuotaCondition,
    QuotaPlan,
)
from vs_agent.api.testing import FakeAgentClient, FakeProvider, FakeTurnScript
from vs_core.api import StopMode
from vs_runtime.api.infrastructure import (
    PROVIDER_OUTAGE,
    CapacityHandling,
    FallbackTarget,
    QuotaAction,
    QuotaPolicy,
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeCapacityTimer, FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentTurnRequest as TurnRequest

ROLE = "dynamic-orchestrator"
START = 1_000_000.0
BUDGET = PROVIDER_OUTAGE.budget_seconds
OUTAGE = "mock"
OTHER = "other"


def _outage(provider: str = OUTAGE) -> AgentQuotaError:
    return AgentQuotaError(
        provider, QuotaCondition.UNAVAILABLE, "API Error: 503 No available accounts"
    )


def _quota(provider: str = OTHER) -> AgentQuotaError:
    return AgentQuotaError(provider, QuotaCondition.QUOTA_EXHAUSTED, "You've hit your limit")


class _Sink(NullAgentEventSink):
    """Remembers the quota events the gate published, with the virtual time of each."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self.paused: list[tuple[AgentQuotaError, QuotaPlan]] = []
        self.resumed: list[tuple[str, str, float]] = []
        self.abandoned: list[tuple[AgentQuotaError, str, float]] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del where
        self.paused.append((error, plan))

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del where
        self.resumed.append((error.provider, reason, waited_seconds))

    def quota_abandoned(self, error: AgentQuotaError, reason: str, where: Attribution) -> None:
        del where
        self.abandoned.append((error, reason, self._now()))


@dataclass
class _Run:
    """One run's control channel and capacity handling on simulated threads; no operator.

    Every client the test builds shares the handling, as every client of a run does, and
    the gate runs on the production timer over the simulated clock.
    """

    policy: QuotaPolicy = field(default_factory=QuotaPolicy)
    clock: VirtualClock = field(
        default_factory=lambda: VirtualClock(START, limit=START + 4 * BUDGET)
    )
    transitions: list[RunControlTransition] = field(default_factory=list)
    threads: SimThreads = field(init=False)
    control: RunControlChannel = field(init=False)
    handling: CapacityHandling = field(init=False)
    sink: _Sink = field(init=False)
    sends: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.threads = SimThreads(self.clock)
        self.control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self.transitions.append), threads=self.threads
        )
        self.handling = CapacityHandling.for_policy(self.policy, self.threads)
        self.sink = _Sink(self.clock.now)

    def client(self, provider: str, *answers: AgentQuotaError | str) -> AgentClient:
        """A client on ``provider`` whose sends answer ``answers`` in order, behind the run's gate."""

        def count(request: TurnRequest) -> None:
            del request
            self.sends[provider] = self.sends.get(provider, 0) + 1

        client = AgentClient(
            FakeProvider(
                script=FakeTurnScript(answers=answers), on_turn=count, threads=self.threads
            ),
            provider=provider,
            model_name="m",
            clock=ManualClock(START),
            threads=self.threads,
        )
        install_capacity_gate(
            client, control=self.control, events=self.sink, handling=self.handling, clock=self.clock
        )
        return client

    def kinds(self) -> list[RunControlTransitionKind]:
        return [transition.kind for transition in self.transitions]

    def now(self) -> float:
        return self.clock.now()


def _turn(client: AgentClient, invocation: str = "inv-1") -> str:
    return client.invoke_text(
        kind=ROLE,
        workspace=Path(),
        system_prompt="Plan.",
        user_prompt="plan the next workstreams",
        round_label="dynamic-orchestrator-session-turn",
        invocation_id=invocation,
    )


def _waits(failures: int) -> list[float]:
    """The waits the outage policy asks for over ``failures`` stops: doubling, capped, budgeted."""
    waits: list[float] = []
    for stop in range(1, failures + 1):
        left = BUDGET - sum(waits)
        if left <= 0:
            break
        wanted = min(
            PROVIDER_OUTAGE.first_wait_seconds * 2 ** (stop - 1), PROVIDER_OUTAGE.max_wait_seconds
        )
        waits.append(min(wanted, left))
    return waits


def test_a_turn_survives_a_provider_outage_without_pausing_the_run() -> None:
    """The live run's case: the default ``pause`` policy, nobody watching, the gateway down."""
    run = _Run()
    client = run.client(OUTAGE, *[_outage()] * 6, "ok")

    reply = run.threads.run(lambda: _turn(client))

    assert reply == "ok"
    assert run.now() == START + sum(_waits(6))
    assert [waited for _, _, waited in run.sink.resumed] == _waits(6)
    assert {reason for _, reason, _ in run.sink.resumed} == {"wait_elapsed"}
    # The run was never paused: core saw no pause and kept admitting work.
    assert run.kinds() == []
    assert not run.control.pause_requested()
    assert run.sink.abandoned == []
    assert [(error.condition, plan.policy) for error, plan in run.sink.paused] == [
        (QuotaCondition.UNAVAILABLE, "wait")
    ] * 6
    assert run.sink.paused[0][1].resumes_at == START + 60.0


@pytest.mark.parametrize("action", list(QuotaAction), ids=lambda action: action.value)
def test_the_quota_policy_does_not_decide_about_an_outage(action: QuotaAction) -> None:
    """``fail`` and ``pause`` are about limits an operator can act on; an outage is waited out."""
    target = FallbackTarget("codex", "gpt-6-luna") if action is QuotaAction.FALLBACK else None
    run = _Run(policy=QuotaPolicy(action, fallback=target))
    client = run.client(OUTAGE, _outage(), _outage(), "ok")

    assert run.threads.run(lambda: _turn(client)) == "ok"
    assert [reason for _, reason, _ in run.sink.resumed] == ["wait_elapsed"] * 2


def test_the_rest_of_the_run_goes_on_while_a_turn_waits_out_an_outage() -> None:
    """Another turn starts and finishes inside the outage wait; nothing waits for the pause flag."""
    run = _Run()
    stalled = run.client(OUTAGE, _outage(), "ok")
    healthy = run.client(OTHER, "done")
    finished: list[float] = []

    def other_work() -> None:
        run.threads.sleep(10.0)
        run.control.wait_while_paused()  # what core's admission amounts to
        assert _turn(healthy, "inv-2") == "done"
        finished.append(run.now())

    def main() -> str:
        worker = run.threads.spawn(other_work, name="other-work", daemon=False)
        reply = _turn(stalled)
        worker.join(BUDGET)
        return reply

    assert run.threads.run(main) == "ok"
    assert finished == [START + 10.0]
    assert run.kinds() == []


def test_an_outage_wait_never_releases_a_turn_held_for_the_operator() -> None:
    """Two turns on one channel: an outage on one provider, a quota pause on another.

    The quota pause belongs to the operator under the ``pause`` policy; the outage wait
    ending must neither resume the run nor release that turn, nor report it as the operator's.
    """
    run = _Run()
    outage = run.client(OUTAGE, _outage(), "ok")
    limited = run.client(OTHER, _quota(), "later")
    replies: dict[str, str] = {}

    def held() -> None:
        replies[OTHER] = _turn(limited, "inv-2")

    def main() -> None:
        worker = run.threads.spawn(held, name="held-turn", daemon=False)
        run.threads.sleep(1.0)
        replies[OUTAGE] = _turn(outage)
        # The outage turn is done; the quota turn is still held for the operator.
        assert OTHER not in replies
        assert run.control.pause_requested()
        assert [provider for provider, _, _ in run.sink.resumed] == [OUTAGE]
        run.threads.sleep(3600.0)
        assert OTHER not in replies
        run.control.resume()
        worker.join(BUDGET)

    run.threads.run(main)

    assert replies == {OUTAGE: "ok", OTHER: "later"}
    assert [(provider, reason) for provider, reason, _ in run.sink.resumed] == [
        (OUTAGE, "wait_elapsed"),
        (OTHER, "operator"),
    ]
    assert RunControlTransitionKind.RESUMED in run.kinds()
    # Only the operator's resume ended the pause.
    assert run.kinds().count(RunControlTransitionKind.RESUMED) == 1


def test_an_operator_pause_during_an_outage_wait_stays_in_force() -> None:
    run = _Run()
    client = run.client(OUTAGE, _outage(), "ok")

    def operator() -> None:
        run.threads.sleep(30.0)
        run.control.request_pause()

    def main() -> str:
        run.threads.spawn(operator, name="operator", daemon=False)
        return _turn(client)

    assert run.threads.run(main) == "ok"
    assert run.control.pause_requested()
    assert run.kinds() == [RunControlTransitionKind.PAUSE_REQUESTED]


@given(cancel_after=st.floats(min_value=1.0, max_value=3 * 3600.0, allow_nan=False))
def test_a_turn_cancelled_while_it_waits_ends_and_is_never_sent_again(cancel_after: float) -> None:
    """However far into the outage the cancel lands, the wait ends then and no resend follows."""
    run = _Run()
    client = run.client(OUTAGE, *[_outage()] * 40, "ok")
    ended: list[float] = []

    def canceller() -> None:
        run.threads.sleep(cancel_after)
        client.cancel()

    def main() -> None:
        run.threads.spawn(canceller, name="canceller", daemon=False)
        with pytest.raises(AgentQuotaError) as raised:
            _turn(client)
        ended.append(run.now())
        assert raised.value.condition is QuotaCondition.UNAVAILABLE

    run.threads.run(main)

    assert ended == [START + cancel_after]
    # One send per wait that ended before the cancel, plus the first.
    assert run.sends[OUTAGE] == 1 + len(run.sink.resumed)
    [(_, reason, _)] = run.sink.abandoned
    assert "cancelled" in reason


def test_a_stop_request_ends_an_outage_wait_at_once() -> None:
    run = _Run()
    client = run.client(OUTAGE, _outage(), "ok")

    def stopper() -> None:
        run.threads.sleep(20.0)
        run.control.request_stop(StopMode.DRAIN)

    def main() -> float:
        run.threads.spawn(stopper, name="stopper", daemon=False)
        with pytest.raises(RunStopped):
            _turn(client)
        return run.now()

    assert run.threads.run(main) == START + 20.0
    assert run.sends[OUTAGE] == 1


@given(left=st.floats(min_value=1.0, max_value=2 * BUDGET, allow_nan=False))
def test_the_run_deadline_ends_an_outage_wait(left: float) -> None:
    """No turn waits past the run's deadline, and none is sent again once it passed."""
    run = _Run()
    run.control.set_run_deadline(START + left)
    client = run.client(OUTAGE, *[_outage()] * 60, "ok")

    def main() -> None:
        with pytest.raises(AgentQuotaError):
            _turn(client)

    run.threads.run(main)

    [(_, reason, at)] = run.sink.abandoned
    assert at <= START + max(left, BUDGET)
    if left <= BUDGET:
        assert "deadline" in reason
        assert at == START + left


@given(failures=st.integers(min_value=0, max_value=60))
def test_an_outage_is_waited_out_only_within_its_budget(failures: int) -> None:
    run = _Run()
    client = run.client(OUTAGE, *[_outage()] * failures, "ok")
    waits = _waits(failures)

    if len(waits) == failures:
        assert run.threads.run(lambda: _turn(client)) == "ok"
        assert run.sink.abandoned == []
    else:

        def failing() -> None:
            with pytest.raises(AgentQuotaError) as raised:
                _turn(client)
            assert raised.value.condition is QuotaCondition.UNAVAILABLE

        run.threads.run(failing)
        [(_, reason, _)] = run.sink.abandoned
        assert "6 h outage budget" in reason

    assert [waited for _, _, waited in run.sink.resumed] == waits[: len(run.sink.resumed)]
    assert run.now() - START <= BUDGET


class _Capture(FakeAgentClient):
    """The fake client, keeping the gate it was given so a test can call it for several turns."""

    gate: CapacityGate | None = None

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        super().set_capacity_gate(gate)
        self.gate = gate


def _gate() -> tuple[CapacityGate, FakeCapacityTimer, _Sink]:
    """The run's gate over a virtual timer, called directly with each turn's record."""
    threads = SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    timer = FakeCapacityTimer(control, start=START)
    sink = _Sink(timer.now)
    capture = _Capture(threads=threads)
    install_capacity_gate(
        capture,
        control=control,
        events=sink,
        handling=CapacityHandling.for_policy(QuotaPolicy(), threads, timer),
        clock=ManualClock(START),
    )
    assert capture.gate is not None
    return capture.gate, timer, sink


def _stop(gate: CapacityGate, stops: CapacityStops, error: AgentQuotaError) -> bool:
    """One capacity stop of a turn; whether the gate let the turn be sent again."""
    stops.count += 1
    try:
        gate.wait_for_capacity(error, AgentTurnRequest(message="m"), role=ROLE, stops=stops)
    except AgentQuotaError:
        return False
    return True


@given(order=st.lists(st.sampled_from(("a", "b")), min_size=1, max_size=80))
def test_turns_that_stop_together_keep_their_own_outage_budgets(order: list[str]) -> None:
    """However the stops of two turns interleave, each turn waits within its own budget."""
    gate, timer, _ = _gate()
    threads = SimThreads()
    stops = {"a": CapacityStops(threads), "b": CapacityStops(threads)}
    gave_up: set[str] = set()

    for turn in order:
        if turn not in gave_up and not _stop(gate, stops[turn], _outage()):
            gave_up.add(turn)

    for turn, record in stops.items():
        assert record.waited <= BUDGET
        # A turn gives up only once the outage outlasted its budget since its own first stop.
        if turn in gave_up:
            assert record.first_at is not None
            assert timer.now() - record.first_at >= BUDGET


_EVENTS = st.lists(
    st.sampled_from(("stop", f"served:{OUTAGE}", f"served:{OTHER}")), min_size=1, max_size=30
)


@given(events=_EVENTS)
def test_a_stop_is_an_outage_only_while_the_provider_serves_no_other_turn(
    events: list[str],
) -> None:
    """A provider that served another turn since this turn first stopped is up.

    agentshim calls any server error transient, so a turn that keeps failing while its
    provider answers other turns fails at once, as it did before the gate waited out
    outages. Turns served by another provider say nothing about this one.
    """
    gate, _, sink = _gate()
    stops = CapacityStops(SimThreads())
    served_since_first = False
    for event in events:
        if event.startswith("served:"):
            gate.turn_served(event.removeprefix("served:"))
            served_since_first |= stops.first_at is not None and event == f"served:{OUTAGE}"
            continue
        sent_again = _stop(gate, stops, _outage())
        if served_since_first:
            assert not sent_again
            [(_, reason, _)] = sink.abandoned
            assert "served other turns" in reason
            return
        assert sent_again
