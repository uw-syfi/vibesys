"""A provider outage pauses the run, which tries the same turn again with backoff until it ends.

The regression these guard: a gateway answering ``503 No available accounts`` for hours made
every agent turn fail once agentshim's own retries ran out, and six failed planner turns ended a
live run. Now such a failure is a capacity stop (``QuotaCondition.UNAVAILABLE``) that the run's
capacity gate waits out under the outage budget, whatever the quota policy says, and only an
outage that outlasts the budget ends the turn.

The provider is the scripted one behind a real ``AgentClient``, so the failure travels the
production path: a transient ``agentshim`` failure with no exhausted window, classified by the
client. Waiting is a virtual timer, so nothing sleeps or reads real time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_runtime.api.infrastructure import (
    PROVIDER_OUTAGE,
    CapacityHandling,
    FallbackTarget,
    ProviderFallback,
    QuotaAction,
    QuotaPolicy,
    RunControlTransition,
    RunControlTransitionKind,
    create_run_control_channel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeCapacityTimer, FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads

ROLE = "dynamic-orchestrator"
START = 1_000_000.0
BUDGET = PROVIDER_OUTAGE.budget_seconds


def _outage(detail: str = "API Error: 503 No available accounts") -> AgentQuotaError:
    return AgentQuotaError("mock", QuotaCondition.UNAVAILABLE, detail)


class _Sink(NullAgentEventSink):
    """Remembers the quota events the gate published."""

    def __init__(self) -> None:
        self.paused: list[tuple[AgentQuotaError, QuotaPlan]] = []
        self.resumed: list[str] = []
        self.abandoned: list[tuple[AgentQuotaError, str]] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del where
        self.paused.append((error, plan))

    def quota_resumed(self, provider: str, reason: str, where: Attribution) -> None:
        del provider, where
        self.resumed.append(reason)

    def quota_abandoned(self, error: AgentQuotaError, reason: str, where: Attribution) -> None:
        del where
        self.abandoned.append((error, reason))


@dataclass
class _Run:
    """A run with no operator: only the policy and the virtual timer can end a pause."""

    policy: QuotaPolicy = field(default_factory=QuotaPolicy)
    sink: _Sink = field(default_factory=_Sink)
    transitions: list[RunControlTransition] = field(default_factory=list)
    threads: SimThreads = field(default_factory=SimThreads)
    timer: FakeCapacityTimer | None = None

    def gate(self, client: object) -> FakeCapacityTimer:
        control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self.transitions.append), threads=self.threads
        )
        self.timer = FakeCapacityTimer(control, start=START)
        install_capacity_gate(
            client,  # type: ignore[arg-type]  # any CapacityGated object takes a gate
            control=control,
            events=self.sink,
            handling=CapacityHandling(
                self.policy, ProviderFallback(None, self.threads), self.timer
            ),
            clock=ManualClock(START),
        )
        return self.timer

    def turn(self, failures: int) -> str:
        """Send one turn to a provider that is unavailable for its first ``failures`` tries."""
        script = FakeTurnScript(answers=(*[_outage() for _ in range(failures)], "ok"))
        client = AgentClient(
            FakeProvider(script=script, threads=self.threads),
            provider="mock",
            model_name="m",
            clock=ManualClock(START),
            threads=self.threads,
        )
        self.gate(client)
        return self.threads.run(
            lambda: client.invoke_text(
                kind=ROLE,
                workspace=Path(),
                system_prompt="Plan.",
                user_prompt="plan the next workstreams",
                round_label="dynamic-orchestrator-session-turn",
                invocation_id="inv-1",
            )
        )


def _kinds(run: _Run) -> list[RunControlTransitionKind]:
    return [transition.kind for transition in run.transitions]


def test_a_turn_survives_a_provider_outage_under_the_default_policy() -> None:
    """The live run's case: the default ``pause`` policy, nobody watching, the gateway down."""
    run = _Run()

    reply = run.turn(failures=6)

    assert reply == "ok"
    assert run.timer is not None
    assert run.timer.waits == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    # Each stop pauses the run and resumes it by itself: no operator is needed.
    assert _kinds(run) == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ] * 6
    assert run.sink.resumed == ["wait_elapsed"] * 6
    assert run.sink.abandoned == []
    assert [(error.condition, plan.policy) for error, plan in run.sink.paused] == [
        (QuotaCondition.UNAVAILABLE, "wait")
    ] * 6
    first = run.sink.paused[0][1]
    assert first.resumes_at == START + 60.0


@pytest.mark.parametrize("action", list(QuotaAction), ids=lambda action: action.value)
def test_the_quota_policy_does_not_decide_about_an_outage(action: QuotaAction) -> None:
    """``fail`` and ``pause`` are about limits an operator can act on; an outage is waited out."""
    target = FallbackTarget("codex", "gpt-6-luna") if action is QuotaAction.FALLBACK else None
    run = _Run(policy=QuotaPolicy(action, fallback=target))

    assert run.turn(failures=2) == "ok"
    assert run.sink.resumed == ["wait_elapsed"] * 2


@given(failures=st.integers(min_value=0, max_value=60))
def test_an_outage_is_waited_out_only_within_its_budget(failures: int) -> None:
    run = _Run()
    waits = _expected_waits(failures)

    if len(waits) == failures:
        assert run.turn(failures) == "ok"
        assert run.sink.abandoned == []
    else:
        with pytest.raises(AgentQuotaError) as raised:
            run.turn(failures)
        assert raised.value.condition is QuotaCondition.UNAVAILABLE
        [(_, reason)] = run.sink.abandoned
        assert "outage budget" in reason

    assert run.timer is not None
    assert run.timer.waits == waits
    assert sum(run.timer.waits) <= BUDGET


def _expected_waits(failures: int) -> list[float]:
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


@dataclass
class _Capture:
    """A client that only takes the gate, so a test can call it for several turns at once."""

    gate: CapacityGate | None = None

    def set_capacity_gate(self, gate: CapacityGate) -> None:
        self.gate = gate


@given(order=st.lists(st.sampled_from(("a", "b")), min_size=1, max_size=80))
def test_turns_that_stop_together_keep_their_own_outage_budgets(order: list[str]) -> None:
    """However the stops of two turns interleave, each turn waits within its own budget."""
    run = _Run()
    capture = _Capture()
    timer = run.gate(capture)
    assert capture.gate is not None
    gate = capture.gate
    stops = {"a": CapacityStops(), "b": CapacityStops()}
    waited = {"a": 0.0, "b": 0.0}
    gave_up: set[str] = set()

    def stop(turn: str) -> None:
        record = stops[turn]
        record.count += 1
        before = len(timer.waits)
        try:
            gate.wait_for_capacity(
                _outage(), AgentTurnRequest(message=turn), role=ROLE, stops=record
            )
        except AgentQuotaError:
            gave_up.add(turn)
        waited[turn] += sum(timer.waits[before:])

    run.threads.run(lambda: [stop(turn) for turn in order if turn not in gave_up])

    for turn, record in stops.items():
        assert waited[turn] <= BUDGET
        # A turn gives up only once the outage outlasted its budget since its own first stop.
        if turn in gave_up:
            assert record.first_at is not None
            assert timer.now() - record.first_at >= BUDGET
