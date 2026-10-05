"""However a provider turn ends, the lifecycle reports one start and one matching finish."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.drivers.fake import FakeDriver, FakeTurnScript
from vs_runtime.api.core import report_turns
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink

if TYPE_CHECKING:
    from pathlib import Path


class _Reply(BaseModel):
    value: int


_text = st.text(alphabet=st.characters(codec="ascii", exclude_categories=["Cc"]), min_size=1)


class _ProviderError(Exception):
    """A provider failure of any kind the driver does not classify."""


def _spec(workspace: Path) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="worker",
        provider="fake",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


@given(
    invocation=_text,
    label=st.none() | _text,
    message=_text,
    outcome=st.sampled_from(["answers", "schema_error", "provider_dies"]),
)
def test_every_dispatched_turn_is_one_start_and_one_finish(
    tmp_path_factory: pytest.TempPathFactory,
    invocation: str,
    label: str | None,
    message: str,
    outcome: str,
) -> None:
    workspace = tmp_path_factory.mktemp("workspace")

    def on_turn(turn: AgentTurnRequest) -> None:
        del turn
        if outcome == "provider_dies":
            raise _ProviderError(message)

    answer = AgentOutputSchemaError(message) if outcome == "schema_error" else {"value": 1}
    lifecycle = FakeAgentExecutionLifecycleSink()
    client = report_turns(
        AgentClient(FakeDriver(script=FakeTurnScript((answer,)), on_turn=on_turn)), lifecycle
    )
    turn = AgentTurnRequest(
        message=message,
        instructions="sys",
        output_schema=_Reply,
        invocation_id=invocation,
        label=label,
    )

    if outcome == "answers":
        client.run(session_spec=_spec(workspace), turn=turn)
    else:
        with pytest.raises((_ProviderError, AgentOutputSchemaError)):
            client.run(session_spec=_spec(workspace), turn=turn)

    started, finished = lifecycle.events
    assert isinstance(started, AgentExecutionStarted)
    assert isinstance(finished, AgentExecutionFinished)
    assert (started.execution_id, started.label) == (invocation, label or "worker")
    assert (finished.execution_id, finished.label) == (started.execution_id, started.label)
    assert (started.system_prompt, started.user_prompt) == ("sys", message)
    expected = {
        "answers": AgentExecutionStatus.COMPLETED,
        "schema_error": AgentExecutionStatus.FAILED,
        "provider_dies": AgentExecutionStatus.FAILED,
    }[outcome]
    assert finished.status is expected
    assert (finished.error is None) == (outcome == "answers")
