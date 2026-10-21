"""However a provider turn ends, the lifecycle reports one start and one matching finish."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_agent.api import (
    AgentClient,
    AgentExecutionCancelledError,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_runtime.api.core import report_turns
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
    create_run_control_channel,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path


class _Reply(BaseModel):
    value: int


_text = st.text(alphabet=st.characters(codec="ascii", exclude_categories=["Cc"]), min_size=1)


class _ProviderError(Exception):
    """A provider failure of any kind the driver does not classify."""


def _spec(workspace: Path, reasoning_effort: str | None = None) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="worker",
        provider="fake",
        workspace=workspace,
        policy=AgentExecutionPolicy(require_enforcement=False),
        reasoning_effort=reasoning_effort,
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
        AgentClient(
            FakeProvider(script=FakeTurnScript((answer,)), on_turn=on_turn, threads=SimThreads()),
            clock=ManualClock(),
            threads=SimThreads(),
        ),
        lifecycle,
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


@given(effort=st.none() | _text)
def test_a_turns_start_names_the_provider_and_reasoning_effort_of_its_session(
    tmp_path_factory: pytest.TempPathFactory, effort: str | None
) -> None:
    lifecycle = FakeAgentExecutionLifecycleSink()
    client = report_turns(
        AgentClient(
            FakeProvider(script=FakeTurnScript(({"value": 1},)), threads=SimThreads()),
            clock=ManualClock(),
            threads=SimThreads(),
        ),
        lifecycle,
    )

    client.run(
        session_spec=_spec(tmp_path_factory.mktemp("workspace"), effort),
        turn=AgentTurnRequest(message="m", output_schema=_Reply, invocation_id="inv"),
    )

    started, _ = lifecycle.events
    assert isinstance(started, AgentExecutionStarted)
    assert (started.provider, started.reasoning_effort) == ("fake", effort)


@given(starts=st.lists(st.booleans(), min_size=1, max_size=30))
def test_no_execution_starts_after_applied_pause_drain(
    tmp_path_factory: pytest.TempPathFactory, starts: list[bool]
) -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    lifecycle = FakeAgentExecutionLifecycleSink()
    provider = FakeProvider(answer={"value": 1}, threads=SimThreads())
    client = report_turns(
        AgentClient(provider, clock=ManualClock(), threads=SimThreads()),
        lifecycle,
        control=control,
    )
    workspace = tmp_path_factory.mktemp("pause-drain")
    draining = False
    for index, drain in enumerate(starts):
        if drain:
            control.land_park()
            draining = True
        turn = AgentTurnRequest(message="m", output_schema=_Reply, invocation_id=str(index))
        before = len(lifecycle.events)
        if draining:
            with pytest.raises(AgentExecutionCancelledError, match="pause drain"):
                client.run(session_spec=_spec(workspace), turn=turn)
            assert len(lifecycle.events) == before
        else:
            client.run(session_spec=_spec(workspace), turn=turn)
            assert len(lifecycle.events) == before + 2
