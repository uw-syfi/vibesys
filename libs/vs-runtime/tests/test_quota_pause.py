"""A turn that hits a provider capacity limit pauses the run and is sent again on resume.

The scenarios drive the public runtime with a scripted client: the client raises
``AgentQuotaError`` for its first calls, the control channel's own PAUSED
transition stands in for the operator (the resume or stop is issued from inside
that transition, so nothing waits on a clock), and the assertions are about what
the run reports and what the agent was asked.
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

from vs_agent.api import AgentQuotaError, NullAgentEventSink, QuotaCondition
from vs_runtime.api import AgentRole
from vs_runtime.api.infrastructure import (
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink

if TYPE_CHECKING:
    from vs_agent.api.testing import FakeAgentClient

ROLE = AgentRole(id="worker", system_prompt="Work.")


class _RecordingSink(NullAgentEventSink):
    """An event sink that remembers the quota events it was given."""

    def __init__(self) -> None:
        self.paused: list[tuple[AgentQuotaError, str | None, str | None]] = []
        self.resumed: list[str] = []

    def quota_paused(
        self,
        error: AgentQuotaError,
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        del invocation_id
        self.paused.append((error, agent_kind, round_label))

    def quota_resumed(self, provider: str, **_context: object) -> None:
        self.resumed.append(provider)


@dataclass
class _Operator:
    """What the operator does when the run reports PAUSED."""

    action: str = "resume"
    transitions: list[RunControlTransition] = field(default_factory=list)
    control: RunControlChannel | None = None

    def __call__(self, transition: RunControlTransition) -> None:
        self.transitions.append(transition)
        if transition.kind is not RunControlTransitionKind.PAUSED:
            return
        assert self.control is not None
        if self.action == "resume":
            self.control.resume()
        else:
            self.control.request_stop()


def _run(
    client: FakeAgentClient, operator: _Operator, sink: _RecordingSink, prompts: tuple[str, ...]
) -> list[str]:
    control = create_run_control_channel(FakeRunControlEventSink(on_transition=operator))
    operator.control = control
    replies: list[str] = []

    async def scenario() -> None:
        runtime = _runtime(
            ROLE,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
                agent_events=sink,
            ),
            control=control,
        )
        try:
            session = await runtime.agents.create_session(ROLE, workspace=runtime.workspaces.root)
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


def _kinds(operator: _Operator) -> list[RunControlTransitionKind]:
    return [transition.kind for transition in operator.transitions]


def test_a_quota_stop_pauses_the_run_and_the_resumed_turn_is_sent_again() -> None:
    client = _client(responses=("done",)).fail("worker", _quota(resets_at=1_900_000_000.0), times=1)
    operator, sink = _Operator(), _RecordingSink()

    replies = _run(client, operator, sink, ("one",))

    assert replies == ["done"]
    # The paused turn is the same invocation, held in place and not issued anew.
    assert [call.user_prompt for call in client.calls_for("worker")] == ["one"]
    assert _kinds(operator) == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]
    [(error, agent_kind, label)] = sink.paused
    assert (error.provider, error.resets_at, agent_kind) == ("claude", 1_900_000_000.0, "worker")
    assert label is not None
    assert sink.resumed == ["claude"]


def test_a_stop_while_paused_on_quota_ends_the_turn_without_sending_it_again() -> None:
    client = _client(responses=("done",)).fail("worker", _quota(), times=1)
    operator, sink = _Operator(action="stop"), _RecordingSink()

    with pytest.raises(RunStopped):
        _run(client, operator, sink, ("one",))

    assert len(client.calls_for("worker")) == 1
    assert len(sink.paused) == 1
    assert sink.resumed == []


@given(
    stops=st.integers(min_value=1, max_value=4),
    condition=st.sampled_from(QuotaCondition),
    resets_at=st.none() | st.floats(min_value=1.0, max_value=4e9, allow_nan=False),
)
def test_any_run_of_quota_stops_pauses_once_each_and_loses_no_turn(
    stops: int, condition: QuotaCondition, resets_at: float | None
) -> None:
    client = _client(responses=("a", "b")).fail("worker", _quota(condition, resets_at), times=stops)
    operator, sink = _Operator(), _RecordingSink()

    replies = _run(client, operator, sink, ("one", "two"))

    assert replies == ["a", "b"]
    assert [call.user_prompt for call in client.calls_for("worker")] == ["one", "two"]
    assert [(e.condition, e.resets_at) for e, _, _ in sink.paused] == [
        (condition, resets_at)
    ] * stops
    assert len(sink.resumed) == stops
    assert _kinds(operator).count(RunControlTransitionKind.PAUSED) == stops
