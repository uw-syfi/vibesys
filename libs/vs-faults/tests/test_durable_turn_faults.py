"""The fault wrapper covers the durable-session boundary production runs use.

Dynamic agents run their turns through ``ClientAgentSessions``, which dispatches
raw keyed turns (``AgentTurnExecutor.run``), not ``invoke``. A wrapper that only
covers ``invoke`` is not a pass-through there, and its faults never fire there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentSessions
from vs_faults.api import AgentFault, Boundary, FaultPlan, FaultRule, FaultyAgentClient

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
