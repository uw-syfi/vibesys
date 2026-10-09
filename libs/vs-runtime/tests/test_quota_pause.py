"""A turn that hits a provider capacity limit pauses the run and finishes after it resumes.

The scenarios drive the public runtime with a scripted client: the client raises
``AgentQuotaError`` for its first calls. The operator is simulated from inside the
control channel's own PAUSED transition (the resume or stop is issued there), and
waiting is a virtual clock, so nothing sleeps or reads real time.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.runtime_agent_sessions import (
    _client,
    _ClientFactory,
    _environment,
    _EnvironmentOpener,
    _runtime,
    _RuntimeEffects,
)

from vs_agent.api import (
    AgentQuotaError,
    Attribution,
    NullAgentEventSink,
    ProviderSwitch,
    QuotaCondition,
    QuotaPlan,
)
from vs_runtime.api import AgentRole
from vs_runtime.api.infrastructure import (
    CapacityHandling,
    QuotaAction,
    QuotaPolicy,
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeCapacityTimer,
    FakeRunControlEventSink,
)

if TYPE_CHECKING:
    from vs_agent.api.testing import FakeAgentClient

ROLE = AgentRole(id="worker", system_prompt="Work.")
START = 1_000_000.0


class _RecordingSink(NullAgentEventSink):
    """An event sink that remembers the quota events it was given."""

    def __init__(self) -> None:
        self.paused: list[tuple[AgentQuotaError, QuotaPlan, str | None]] = []
        self.resumed: list[tuple[str, str]] = []
        self.abandoned: list[tuple[AgentQuotaError, str]] = []
        self.switched: list[tuple[ProviderSwitch, str | None]] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        self.paused.append((error, plan, where.agent_kind))

    def quota_resumed(self, provider: str, reason: str, where: Attribution) -> None:
        del where
        self.resumed.append((provider, reason))

    def quota_abandoned(self, error: AgentQuotaError, reason: str, where: Attribution) -> None:
        del where
        self.abandoned.append((error, reason))

    def provider_switched(self, switch: ProviderSwitch, where: Attribution) -> None:
        self.switched.append((switch, where.agent_kind))


@dataclass
class _Run:
    """One scripted run: what the operator does at PAUSED, what the run reported."""

    policy: QuotaPolicy = field(default_factory=QuotaPolicy)
    operator: str = "resume"
    operator_resumes_after: float | None = None
    sink: _RecordingSink = field(default_factory=_RecordingSink)
    transitions: list[RunControlTransition] = field(default_factory=list)
    timer: FakeCapacityTimer | None = None
    control: RunControlChannel | None = None

    def kinds(self) -> list[RunControlTransitionKind]:
        return [transition.kind for transition in self.transitions]

    def _on_transition(self, transition: RunControlTransition) -> None:
        self.transitions.append(transition)
        # Under a waiting policy the virtual timer settles the pause; otherwise the
        # operator does, at the moment the run reports it is paused.
        if transition.kind is not RunControlTransitionKind.PAUSED or self.timer is not None:
            return
        assert self.control is not None
        if self.operator == "resume":
            self.control.resume()
        else:
            self.control.request_stop()

    def play(self, client: FakeAgentClient, prompts: tuple[str, ...]) -> list[str]:
        control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self._on_transition)
        )
        self.control = control
        if self.policy.action is QuotaAction.WAIT:
            self.timer = FakeCapacityTimer(
                control, start=START, operator_resumes_after=self.operator_resumes_after
            )
        replies: list[str] = []

        async def scenario() -> None:
            runtime = _runtime(
                ROLE,
                _RuntimeEffects(
                    _ClientFactory(client),
                    _EnvironmentOpener(_environment()),
                    FakeAgentExecutionLifecycleSink(),
                    agent_events=self.sink,
                    capacity=CapacityHandling(self.policy, self.timer),
                ),
                control=control,
            )
            try:
                session = await runtime.agents.create_session(
                    ROLE, workspace=runtime.workspaces.root
                )
                for prompt in prompts:
                    reply = await session.turn(prompt)
                    replies.append(str(reply))
            finally:
                await runtime.workspaces.close()

        asyncio.run(scenario())
        return replies


def _quota(
    condition: QuotaCondition = QuotaCondition.QUOTA_EXHAUSTED, resets_at: float | None = None
) -> AgentQuotaError:
    return AgentQuotaError("claude", condition, "You've hit your limit", resets_at)


def test_a_quota_stop_pauses_the_run_and_the_turn_finishes_on_resume() -> None:
    client = _client(responses=("done",)).fail("worker", _quota(resets_at=1_900_000_000.0), times=1)
    run = _Run()

    replies = run.play(client, ("one",))

    assert replies == ["done"]
    # The paused turn is the same invocation, held in place and not issued anew.
    assert [call.user_prompt for call in client.calls_for("worker")] == ["one"]
    assert run.kinds() == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]
    [(error, plan, agent_kind)] = run.sink.paused
    assert (error.provider, error.resets_at, plan, agent_kind) == (
        "claude",
        1_900_000_000.0,
        QuotaPlan(),
        "worker",
    )
    assert run.sink.resumed == [("claude", "operator")]


def test_a_stop_while_paused_on_quota_ends_the_turn_without_sending_it_again() -> None:
    client = _client(responses=("done",)).fail("worker", _quota(), times=1)
    run = _Run(operator="stop")

    with pytest.raises(RunStopped):
        run.play(client, ("one",))

    assert len(run.sink.paused) == 1
    assert run.sink.resumed == []


@given(
    stops=st.integers(min_value=1, max_value=4),
    condition=st.sampled_from(QuotaCondition),
    resets_at=st.none() | st.floats(min_value=1.0, max_value=4e9, allow_nan=False),
)
def test_any_run_of_quota_stops_pauses_once_each_and_loses_no_turn(
    stops: int, condition: QuotaCondition, resets_at: float | None
) -> None:
    client = _client(responses=("a", "b")).fail("worker", _quota(condition, resets_at), times=stops)
    run = _Run()

    replies = run.play(client, ("one", "two"))

    assert replies == ["a", "b"]
    assert [call.user_prompt for call in client.calls_for("worker")] == ["one", "two"]
    assert [(e.condition, e.resets_at) for e, _, _ in run.sink.paused] == [
        (condition, resets_at)
    ] * stops
    assert len(run.sink.resumed) == stops
    assert run.kinds().count(RunControlTransitionKind.PAUSED) == stops


def test_the_wait_policy_resumes_by_itself_when_the_reported_reset_arrives() -> None:
    client = _client(responses=("done",)).fail("worker", _quota(resets_at=START + 600), times=1)
    run = _Run(policy=QuotaPolicy(QuotaAction.WAIT))

    replies = run.play(client, ("one",))

    assert replies == ["done"]
    assert run.timer is not None
    assert run.timer.waits == [605.0]  # the reset plus the policy's margin
    assert run.kinds() == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]
    assert [plan.resumes_at for _, plan, _ in run.sink.paused] == [START + 605]
    assert run.sink.resumed == [("claude", "wait_elapsed")]


def test_an_operator_who_resumes_early_ends_a_wait() -> None:
    client = _client(responses=("done",)).fail("worker", _quota(resets_at=START + 600), times=1)
    run = _Run(policy=QuotaPolicy(QuotaAction.WAIT), operator_resumes_after=10.0)

    run.play(client, ("one",))

    assert run.sink.resumed == [("claude", "operator")]


@pytest.mark.parametrize(
    "policy",
    [QuotaPolicy(QuotaAction.FAIL), QuotaPolicy(QuotaAction.WAIT, wait_seconds=60)],
    ids=["fail", "reset-beyond-the-budget"],
)
def test_a_policy_that_will_not_wait_ends_the_turn_with_the_quota_error_and_never_pauses(
    policy: QuotaPolicy,
) -> None:
    client = _client(responses=("done",)).fail("worker", _quota(resets_at=START + 600), times=1)
    run = _Run(policy=policy)

    with pytest.raises(AgentQuotaError):
        run.play(client, ("one",))

    assert run.transitions == []
    assert run.sink.paused == []
    [(_error, reason)] = run.sink.abandoned
    assert reason


@given(
    stops=st.integers(min_value=1, max_value=8),
    retry=st.integers(min_value=1, max_value=50),
    budget=st.integers(min_value=1, max_value=200),
)
def test_a_wait_budget_bounds_the_total_time_a_turn_spends_waiting(
    stops: int, retry: int, budget: int
) -> None:
    client = _client(responses=("done",)).fail("worker", _quota(), times=stops)
    run = _Run(policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=budget, retry_seconds=retry))
    allowed_waits = -(-budget // retry)  # ceil: the last wait is cut to what remains

    if stops <= allowed_waits:
        assert run.play(client, ("one",)) == ["done"]
    else:
        with pytest.raises(AgentQuotaError):
            run.play(client, ("one",))

    assert run.timer is not None
    assert sum(run.timer.waits) <= budget
    assert len(run.timer.waits) == min(stops, allowed_waits)
