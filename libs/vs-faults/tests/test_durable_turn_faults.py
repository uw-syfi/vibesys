"""The fault wrapper covers the durable-session boundary production runs use.

Dynamic agents run their turns through ``ClientAgentSessions``, which dispatches
raw keyed turns (``AgentTurnExecutor.run``), not ``invoke``. A wrapper that only
covers ``invoke`` is not a pass-through there, and its faults never fire there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import BaseModel, ConfigDict

from vs_agent.api import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    Completed,
    SessionScope,
    SteerOutcome,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentSessions
from vs_faults.api import (
    AgentCrashError,
    AgentFault,
    Boundary,
    FaultPlan,
    FaultRule,
    FaultyAgentClient,
)

if TYPE_CHECKING:
    from pathlib import Path

_KIND = "implementer"
_KEY = AgentSessionKey(SessionScope.HYPOTHESIS, "H-01")


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str


_ANSWER = _Reply(summary="done")


def _inner() -> FakeAgentClient:
    return FakeAgentClient(
        capabilities=AgentCapabilities(
            tool_servers=True, session_reuse=True, provider_session_resume=True
        ),
        session_reuse=True,
    ).set_response(_KIND, _ANSWER)


def _durable_turn(client: FakeAgentClient | FaultyAgentClient, workspace: Path) -> object:
    spec = AgentSessionSpec(
        role=_KIND, provider="fake", workspace=workspace, policy=AgentExecutionPolicy()
    )
    turn = AgentTurnRequest(message="Implement `H-01`.", output_schema=_Reply, invocation_id="i-1")
    sessions = FakeAgentSessions(client)
    sessions.bind(_KEY, spec, turn)
    return sessions.start(_KEY, spec, turn)


def test_an_empty_plan_passes_a_durable_turn_through(tmp_path: Path) -> None:
    unwrapped = _durable_turn(_inner(), tmp_path / "a")
    wrapped = _durable_turn(FaultyAgentClient(_inner(), FaultPlan(seed=0)), tmp_path / "b")

    assert isinstance(unwrapped, Completed)
    assert isinstance(wrapped, Completed), wrapped
    assert _Reply.model_validate_json(wrapped.result.text) == _ANSWER


@pytest.mark.parametrize("fault", list(AgentFault))
def test_an_agent_fault_fires_on_a_durable_turn(tmp_path: Path, fault: AgentFault) -> None:
    rule = FaultRule(boundary=Boundary.AGENT_TURN, target=_KIND, at=1, fault=fault)
    client = FaultyAgentClient(_inner(), FaultPlan(seed=7, rules=(rule,)))

    _durable_turn(client, tmp_path)

    assert client.injected == [(_KIND, 1, fault)]


@pytest.mark.parametrize("outcome", list(SteerOutcome))
def test_a_steer_reaches_the_inner_client_during_a_faulted_turn(
    outcome: SteerOutcome, tmp_path: Path
) -> None:
    """Steering is not a fault boundary: the wrapper answers as the inner client does."""
    inner = _inner().set_steering(outcome)
    plan = FaultPlan(
        seed=0, rules=(FaultRule(boundary=Boundary.AGENT_TURN, fault=AgentFault.CRASH, at=1),)
    )
    wrapped = FaultyAgentClient(inner, plan)
    answers: list[SteerOutcome] = []
    inner.on_invoke(
        lambda _call: answers.append(wrapped.steer("go left", on_rejected=lambda: None))
    )

    with pytest.raises(AgentCrashError):
        wrapped.invoke(
            kind=_KIND,
            workspace=tmp_path,
            system_prompt="s",
            user_prompt="u",
            response_cls=_Reply,
            round_label="r1",
        )

    assert answers == [outcome]
    assert inner.steers == (["go left"] if outcome is SteerOutcome.DELIVERED else [])


def test_a_steer_is_unsupported_when_the_inner_client_takes_none() -> None:
    class _NoSteer:
        """A client without the optional steering capability."""

    wrapped = FaultyAgentClient(cast("Any", _NoSteer()), FaultPlan(seed=0))

    assert wrapped.steer("x", on_rejected=lambda: None) is SteerOutcome.UNSUPPORTED
