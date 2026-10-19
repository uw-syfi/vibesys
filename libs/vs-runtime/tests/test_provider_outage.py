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
    ProviderSwitch,
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
    WaitFor,
    create_run_control_channel,
    decide_outage,
    decide_quota,
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
        self.abandoned_waits: list[float] = []
        self.switched: list[ProviderSwitch] = []
        self.ended: dict[str | None, str] = {}

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del where
        self.paused.append((error, plan))

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del where
        self.resumed.append((error.provider, reason, waited_seconds))

    def quota_abandoned(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        self.abandoned.append((error, reason, self._now()))
        self.abandoned_waits.append(waited_seconds)
        self.ended[where.invocation_id] = reason

    def provider_switched(self, switch: ProviderSwitch, where: Attribution) -> None:
        del where
        self.switched.append(switch)


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

    def client(
        self,
        provider: str,
        *answers: AgentQuotaError | str,
        model: str = "m",
        send_seconds: float = 0.0,
    ) -> AgentClient:
        """A client on ``provider`` whose sends answer ``answers`` in order, behind the run's gate.

        Each send takes ``send_seconds`` of virtual time, as agentshim's own retries do
        before a failing send reaches the gate.
        """

        def count(request: TurnRequest) -> None:
            del request
            self.sends[provider] = self.sends.get(provider, 0) + 1
            if send_seconds:
                self.threads.sleep(send_seconds)

        client = AgentClient(
            FakeProvider(
                script=FakeTurnScript(answers=answers), on_turn=count, threads=self.threads
            ),
            provider=provider,
            model_name=model,
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


@pytest.mark.parametrize("action", [QuotaAction.PAUSE, QuotaAction.WAIT], ids=str)
def test_under_pause_and_wait_an_outage_is_waited_out(action: QuotaAction) -> None:
    """``pause`` is about limits an operator can act on; an outage never waits for one."""
    run = _Run(policy=QuotaPolicy(action))
    client = run.client(OUTAGE, _outage(), _outage(), "ok")

    assert run.threads.run(lambda: _turn(client)) == "ok"
    assert [reason for _, reason, _ in run.sink.resumed] == ["wait_elapsed"] * 2
    assert run.kinds() == []


def test_under_fail_an_outage_is_waited_out_for_one_backoff_step_only() -> None:
    run = _Run(policy=QuotaPolicy(QuotaAction.FAIL))
    blip = run.client(OUTAGE, _outage(), "ok")
    down = run.client(OUTAGE, _outage(), _outage(), "ok")

    assert run.threads.run(lambda: _turn(blip)) == "ok"

    def failing() -> None:
        with pytest.raises(AgentQuotaError):
            _turn(down, "inv-2")

    run.threads.run(failing)

    first = PROVIDER_OUTAGE.first_wait_seconds
    assert [waited for _, _, waited in run.sink.resumed] == [first, first]
    [(_, reason, _)] = run.sink.abandoned
    assert "1 min outage budget" in reason
    assert run.sink.switched == []
    assert run.kinds() == []


def test_under_fallback_an_outage_switches_providers_after_one_backoff_step() -> None:
    target = FallbackTarget("codex", "gpt-6-luna")
    run = _Run(policy=QuotaPolicy(QuotaAction.FALLBACK, fallback=target))
    client = run.client(OUTAGE, _outage(), _outage(), "ok")

    def failing() -> None:
        with pytest.raises(AgentQuotaError):
            _turn(client)

    run.threads.run(failing)

    assert [waited for _, _, waited in run.sink.resumed] == [PROVIDER_OUTAGE.first_wait_seconds]
    assert [(switch.from_provider, switch.to_provider) for switch in run.sink.switched] == [
        (OUTAGE, "codex")
    ]
    assert run.handling.fallback.replaced(OUTAGE)
    assert run.now() == START + PROVIDER_OUTAGE.first_wait_seconds


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
    """No turn waits past the run's deadline, and none is sent again once it passed.

    Past the outage budget the run pauses for the operator instead (S179), and a paused
    run ends at its deadline by the run loop's stop, modelled here one second after it.
    """
    run = _Run()
    run.control.set_run_deadline(START + left)
    client = run.client(OUTAGE, *[_outage()] * 60, "ok")

    def stopper() -> None:
        run.threads.sleep(left + 1.0)
        run.control.request_stop(StopMode.DRAIN)

    def main() -> None:
        run.threads.spawn(stopper, name="deadline-stop", daemon=True)
        with pytest.raises((AgentQuotaError, RunStopped)):
            _turn(client)

    run.threads.run(main)

    if left <= BUDGET:
        [(_, reason, at)] = run.sink.abandoned
        assert "deadline" in reason
        assert at == START + left
    else:
        assert run.sink.abandoned == []
        assert RunControlTransitionKind.PAUSED in [t.kind for t in run.transitions]
        assert run.now() == START + left + 1.0


@given(failures=st.integers(min_value=0, max_value=60))
def test_an_outage_is_waited_out_only_within_its_budget(failures: int) -> None:
    """Within its budget an outage holds only the turn; once spent, the run pauses for the
    operator rather than fail the turn (S179: before, the gate gave the turn up here)."""
    run = _Run()
    client = run.client(OUTAGE, *[_outage()] * failures, "ok")
    waits = _waits(failures)

    if len(waits) == failures:
        assert run.threads.run(lambda: _turn(client)) == "ok"
        assert RunControlTransitionKind.PAUSED not in [t.kind for t in run.transitions]
    else:
        # Nobody resumes the paused run; a stop well after the budget ends the test.
        def stopper() -> None:
            run.threads.sleep(BUDGET + 3600.0)
            run.control.request_stop(StopMode.DRAIN)

        def paused() -> None:
            run.threads.spawn(stopper, name="stopper", daemon=True)
            with pytest.raises(RunStopped):
                _turn(client)

        run.threads.run(paused)
        assert RunControlTransitionKind.PAUSED in [t.kind for t in run.transitions]
        [(error, plan)] = [(e, p) for e, p in run.sink.paused if p.policy == "pause"]
        assert error.condition is QuotaCondition.UNAVAILABLE
        assert plan.resumes_at is None

    assert run.sink.abandoned == []
    assert [waited for _, _, waited in run.sink.resumed] == waits[: len(run.sink.resumed)]


class _Capture(FakeAgentClient):
    """The fake client, keeping the gate it was given so a test can call it for several turns."""

    gate: CapacityGate | None = None

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        super().set_capacity_gate(gate)
        self.gate = gate


def _gate(
    policy: QuotaPolicy | None = None, threads: SimThreads | None = None
) -> tuple[CapacityGate, FakeCapacityTimer, _Sink]:
    """The run's gate over a virtual timer, called directly with each turn's record.

    A test whose stops pause the run (a quota) calls the gate inside ``threads.run``.
    """
    threads = threads or SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    timer = FakeCapacityTimer(control, start=START)
    sink = _Sink(timer.now)
    capture = _Capture(threads=threads)
    install_capacity_gate(
        capture,
        control=control,
        events=sink,
        handling=CapacityHandling.for_policy(policy or QuotaPolicy(), threads, timer),
        clock=ManualClock(START),
    )
    assert capture.gate is not None
    return capture.gate, timer, sink


def _stop(
    gate: CapacityGate, stops: CapacityStops, error: AgentQuotaError, turn: str = "inv-1"
) -> bool:
    """One capacity stop of a turn; whether the gate let the turn be sent again."""
    stops.count += 1
    try:
        gate.wait_for_capacity(
            error, AgentTurnRequest(message="m", invocation_id=turn), role=ROLE, stops=stops
        )
    except AgentQuotaError:
        return False
    return True


def _record(model: str = "m") -> CapacityStops:
    return CapacityStops(SimThreads(), model=model)


@given(order=st.lists(st.sampled_from(("a", "b")), min_size=1, max_size=80))
def test_turns_that_stop_together_keep_their_own_outage_budgets(order: list[str]) -> None:
    """However the stops of two turns interleave, each turn waits within its own budget."""
    gate, timer, sink = _gate()
    stops = {"a": _record(), "b": _record()}
    first_stop: dict[str, float] = {}
    gave_up: dict[str, float] = {}

    for turn in order:
        if turn in gave_up:
            continue
        first_stop.setdefault(turn, timer.now())
        if not _stop(gate, stops[turn], _outage(), turn):
            gave_up[turn] = timer.now()

    for turn, at in gave_up.items():
        # A turn gives up only once the outage outlasted its budget since its own first stop.
        assert "outage budget" in sink.ended[turn]
        assert at - first_stop[turn] >= BUDGET


@given(
    kinds=st.lists(st.sampled_from(("outage", "quota")), min_size=1, max_size=40),
    quota_budget=st.floats(min_value=1.0, max_value=4 * 3600.0, allow_nan=False),
)
def test_quota_and_outage_stops_of_one_turn_never_spend_each_others_budget(
    kinds: list[str], quota_budget: float
) -> None:
    """Each stop is decided on its own condition's history only.

    A quota stop is decided on the time the turn waited on quotas alone, and ends the
    turn's outage streak; an outage stop is decided on its streak alone (its stops and
    the time since its first). Before, both shared one record: an outage after a quota
    inherited the quota's start time and stop count, and an outage wait spent the quota
    budget.
    """
    policy = QuotaPolicy(QuotaAction.WAIT, wait_seconds=quota_budget)
    threads = SimThreads()
    gate, timer, _ = _gate(policy, threads)
    record = _record()
    threads.run(lambda: _stop_in_turn(gate, timer, record, policy, kinds))


def _stop_in_turn(
    gate: CapacityGate,
    timer: FakeCapacityTimer,
    record: CapacityStops,
    policy: QuotaPolicy,
    kinds: list[str],
) -> None:
    """Send one turn's stops of ``kinds`` to the gate, checking each against its decision."""
    quota_waited = 0.0
    streak: list[float] = []  # the start time of each stop in the current outage streak

    for kind in kinds:
        now = timer.now()
        if kind == "quota":
            streak = []
            expected = decide_quota(policy, resets_at=None, now=now, waited=quota_waited)
            error = _quota(OUTAGE)
        else:
            streak.append(now)
            expected = decide_outage(
                PROVIDER_OUTAGE, stop=len(streak), elapsed=now - streak[0], run_left=None
            )
            error = _outage()

        sent_again = _stop(gate, record, error)

        assert sent_again == isinstance(expected, WaitFor)
        if not isinstance(expected, WaitFor):
            return
        waited = timer.now() - now
        assert waited == pytest.approx(expected.seconds, abs=1e-6)
        if kind == "quota":
            quota_waited += waited


_ROUTE_EVENTS = st.lists(
    st.sampled_from(("a", "b", "served", "served-other-model")), min_size=1, max_size=30
)


@dataclass
class _TurnModel:
    """What the rule says about one turn: its record, and the route as it last let it go."""

    record: CapacityStops = field(default_factory=_record)
    released: tuple[int, int] | None = None


@given(events=_ROUTE_EVENTS)
def test_a_stop_is_an_outage_unless_the_route_served_others_while_none_of_them_failed(
    events: list[str],
) -> None:
    """A route (provider and model) that served turns while this turn's latest send was
    failing, and saw no other turn stop on an outage then, is up: the failure is the turn's
    own, and the turn fails at once, as before the gate waited out outages.

    Two turns, ``a`` and ``b``, stop on one route; ``served`` is a turn that route served,
    ``served-other-model`` one the same provider served on another model, which says
    nothing about this one. Only what lands between a turn's release and its next stop
    counts, so a success long ago, or during the turn's own wait, never fails it. Sends
    take no time here, so each event lands after the turn's previous release.
    """
    gate, _, sink = _gate()
    turns = {"a": _TurnModel(), "b": _TurnModel()}
    served = 0
    outage_stops = 0

    for event in events:
        if event == "served":
            gate.turn_served(OUTAGE, "m")
            served += 1
            continue
        if event == "served-other-model":
            gate.turn_served(OUTAGE, "other-model")
            continue
        turn = turns[event]
        up = turn.released is not None and (
            served > turn.released[0] and outage_stops == turn.released[1]
        )

        sent_again = _stop(gate, turn.record, _outage(), event)

        assert sent_again == (not up)
        if up:
            assert "served other turns" in sink.ended[event]
            turns[event] = _TurnModel()  # the caller's next turn has a fresh record
            continue
        outage_stops += 1
        turn.released = (served, outage_stops)


def test_one_model_down_while_another_on_the_same_provider_is_served_is_still_an_outage() -> None:
    """The live incident's shape: a gateway served one model while the other had no accounts.

    Turns on the served model succeed throughout; the turn on the model that is down waits
    the outage out instead of failing at its second stop.
    """
    run = _Run()
    down = run.client(OUTAGE, *[_outage()] * 4, "ok", model="down", send_seconds=900.0)
    up = run.client(OUTAGE, "fine", model="up")
    replies: list[str] = []

    def served_model() -> None:
        for n in range(40):
            run.threads.sleep(120.0)
            replies.append(_turn(up, f"up-{n}"))

    def main() -> str:
        worker = run.threads.spawn(served_model, name="served-model", daemon=False)
        reply = _turn(down)
        worker.join(BUDGET)
        return reply

    assert run.threads.run(main) == "ok"
    assert run.sink.abandoned == []
    assert replies == ["fine"] * 40


def test_one_success_during_a_partial_outage_does_not_fail_the_turns_still_failing() -> None:
    """A route that serves one turn while others keep stopping on it is still down for them.

    Turn ``a`` and turn ``c`` stop on the same route; one turn on it succeeds while ``a``'s
    second send is failing, but ``c`` stopped in that window too, so ``a`` keeps waiting.
    Before, the gate compared with its count at ``a``'s first stop, once, and the one
    success failed every turn still waiting.
    """
    run = _Run()
    a = run.client(OUTAGE, *[_outage()] * 3, "a-ok", send_seconds=900.0)
    c = run.client(OUTAGE, *[_outage()] * 3, "c-ok", send_seconds=900.0)
    b = run.client(OUTAGE, "b-ok")
    replies: dict[str, str] = {}

    def turn_c() -> None:
        run.threads.sleep(300.0)
        replies["c"] = _turn(c, "inv-c")

    def turn_b() -> None:
        # ``a`` stops at 900 s, waits 60 s, and its second send fails from 960 s to 1860 s;
        # ``c`` stops at 1200 s, inside that window.
        run.threads.sleep(1500.0)
        replies["b"] = _turn(b, "inv-b")

    def main() -> None:
        workers = [
            run.threads.spawn(turn_c, name="turn-c", daemon=False),
            run.threads.spawn(turn_b, name="turn-b", daemon=False),
        ]
        replies["a"] = _turn(a)
        for worker in workers:
            worker.join(BUDGET)

    run.threads.run(main)

    assert replies == {"a": "a-ok", "b": "b-ok", "c": "c-ok"}
    assert run.sink.abandoned == []


def test_a_success_while_only_this_turn_fails_ends_it_as_its_own_failure() -> None:
    """The route served another turn during this turn's failing send, and nothing else
    failed: the failure is the turn's own, and the turn ends at that stop."""
    run = _Run()
    a = run.client(OUTAGE, *[_outage()] * 3, "a-ok", send_seconds=900.0)
    b = run.client(OUTAGE, "b-ok")

    def turn_b() -> None:
        run.threads.sleep(1500.0)
        assert _turn(b, "inv-b") == "b-ok"

    def main() -> None:
        worker = run.threads.spawn(turn_b, name="turn-b", daemon=False)
        with pytest.raises(AgentQuotaError):
            _turn(a)
        worker.join(BUDGET)

    run.threads.run(main)

    [(_, reason, at)] = run.sink.abandoned
    assert "served other turns" in reason
    assert at == START + 900.0 + 60.0 + 900.0


@pytest.mark.parametrize("action", [QuotaAction.PAUSE, QuotaAction.WAIT], ids=str)
@given(cancel_after=st.floats(min_value=1.0, max_value=290.0, allow_nan=False))
def test_a_cancel_ends_a_quota_wait_at_once(action: QuotaAction, cancel_after: float) -> None:
    """The gate's contract: a cancellation of the turn ends its wait, for a quota as for an
    outage. Under ``pause`` the cancelled turn was the only owner of the quota pause, so the
    run resumes itself at once, as a restart would resume it; before, it stayed paused with
    no turn, no timer and no request to end the pause. Under ``wait`` the run stays paused
    until its timed resume (here a 300 s retry), as the wait's own end would have resumed it."""
    run = _Run(policy=QuotaPolicy(action))
    client = run.client(OTHER, _quota(), "later")
    ended: list[float] = []

    def canceller() -> None:
        run.threads.sleep(cancel_after)
        client.cancel()

    def main() -> None:
        run.threads.spawn(canceller, name="canceller", daemon=False)
        with pytest.raises(AgentQuotaError):
            _turn(client)
        ended.append(run.now())

    run.threads.run(main)

    assert ended == [START + cancel_after]
    assert run.sends[OTHER] == 1
    [(_, reason, _)] = run.sink.abandoned
    assert "cancelled" in reason
    assert run.sink.abandoned_waits == [pytest.approx(cancel_after)]
    assert run.control.pause_requested() is (action is QuotaAction.WAIT)


@given(
    cancelled=st.lists(st.booleans(), min_size=1, max_size=4),
    cancel_after=st.floats(min_value=1.0, max_value=290.0, allow_nan=False),
)
def test_a_cancel_leaves_the_runs_timed_resume_to_the_other_turns(
    cancelled: list[bool], cancel_after: float
) -> None:
    """Turns stopped on one quota under ``wait``: cancelling any of them ends only that turn.

    The run stays paused, the other turns stay parked, and the run resumes itself once, at
    the retry the first stop armed, even when every turn that armed it was cancelled (the run
    loop's poll fires it then). Before, the first cancelled turn resumed the run at once.
    """
    run = _Run(policy=QuotaPolicy(QuotaAction.WAIT))
    retry = run.policy.retry_seconds
    clients = [run.client(OTHER, _quota(), f"later-{i}") for i in range(len(cancelled))]
    ended: dict[int, tuple[str, float]] = {}

    def turn(index: int) -> None:
        try:
            ended[index] = (_turn(clients[index], f"inv-{index}"), run.now())
        except AgentQuotaError:
            ended[index] = ("cancelled", run.now())

    def main() -> tuple[bool, dict[int, tuple[str, float]], bool]:
        workers = [
            run.threads.spawn(lambda i=i: turn(i), name=f"turn-{i}", daemon=False)
            for i in range(len(cancelled))
        ]
        run.threads.sleep(cancel_after)
        for client, cancel in zip(clients, cancelled, strict=True):
            if cancel:
                client.cancel()
        run.threads.sleep(1.0)
        during = (run.control.pause_requested(), dict(ended))
        run.threads.sleep(START + retry + 1.0 - run.now())
        paused_at_end = run.control.pause_requested()
        for worker in workers:
            worker.join(BUDGET)
        return (*during, paused_at_end)

    paused_after_cancel, ended_after_cancel, paused_at_end = run.threads.run(main)

    survivors = {i for i, cancel in enumerate(cancelled) if not cancel}
    assert paused_after_cancel
    assert ended_after_cancel == {
        i: ("cancelled", pytest.approx(START + cancel_after))
        for i in range(len(cancelled))
        if i not in survivors
    }
    assert ended == {
        i: (f"later-{i}", pytest.approx(START + retry))
        if i in survivors
        else ("cancelled", pytest.approx(START + cancel_after))
        for i in range(len(cancelled))
    }
    assert not paused_at_end
    assert run.kinds().count(RunControlTransitionKind.RESUMED) == 1
    assert [reason for _, reason, _ in run.sink.resumed] == ["wait_elapsed"] * len(survivors)


def test_a_cancelled_turn_leaves_the_operators_capacity_wait_to_the_others() -> None:
    """Two turns parked on a quota for the operator; one is cancelled. The operator's request to
    wait for capacity still reaches the other, which resumes the run when capacity returns.
    Before, the cancelled turn's exit refused every later request for the whole run."""
    run = _Run(policy=QuotaPolicy(QuotaAction.PAUSE))
    retry = run.policy.retry_seconds
    a = run.client(OTHER, _quota(), "a-later")
    b = run.client(OTHER, _quota(), "b-later")
    replies: dict[str, tuple[str, float]] = {}
    accepted: list[bool] = []

    def turn_a() -> None:
        with pytest.raises(AgentQuotaError):
            _turn(a, "inv-a")
        replies["a"] = ("cancelled", run.now())

    def main() -> None:
        worker = run.threads.spawn(turn_a, name="turn-a", daemon=False)
        operator = run.threads.spawn(operate, name="operator", daemon=False)
        replies["b"] = (_turn(b, "inv-b"), run.now())
        worker.join(BUDGET)
        operator.join(BUDGET)

    def operate() -> None:
        run.threads.sleep(10.0)
        a.cancel()
        run.threads.sleep(10.0)
        accepted.append(run.control.resume_when_capacity_returns())

    run.threads.run(main)

    assert accepted == [True]
    assert replies == {
        "a": ("cancelled", pytest.approx(START + 10.0)),
        "b": ("b-later", pytest.approx(START + 20.0 + retry)),
    }
    assert not run.control.pause_requested()
