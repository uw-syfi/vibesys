"""However a provider turn ends, the lifecycle reports one start and one matching finish."""

from __future__ import annotations

import asyncio
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
from vs_core.api import CancelOwnedJob, RunId, Scope
from vs_evaluation.api import EvaluationState, PollPhase
from vs_runtime.api import AccuracyEvaluation, PollingEvaluationExecutor
from vs_runtime.api.core import report_turns
from vs_runtime.api.infrastructure import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
    create_run_control_channel,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeEvaluation,
    FakeRunControlEventSink,
    FakeWorkspace,
    FakeWorkspaces,
)
from vs_sim.api.testing import (
    HANG_GUARD_S,
    Gate,
    ManualClock,
    SimBlockingRunner,
    SimThreads,
    loop_threads,
)
from vs_slurm.api import SlurmJobStatus

from .test_evaluation_requests import query, resource_of, run, settled, submit, world
from .test_polling_stage_output import _HANDLE, _request, _terminal

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


async def test_pause_drain_allows_the_evaluation_an_executing_turn_is_waiting_for(
    tmp_path: Path,
) -> None:
    threads = loop_threads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    entered = Gate()
    evaluated = threads.event()
    owner = Scope(owner=RunId(root="run"), generation=0)

    def on_turn(_turn: AgentTurnRequest) -> None:
        entered.open()
        assert evaluated.wait(HANG_GUARD_S)

    client = report_turns(
        AgentClient(
            FakeProvider(answer={"value": 1}, on_turn=on_turn, threads=threads),
            clock=ManualClock(),
            threads=threads,
        ),
        FakeAgentExecutionLifecycleSink(),
        control=control,
    )
    control.prepare_execution("waiting-turn", owner)
    agent = asyncio.create_task(
        SimBlockingRunner(threads).run(
            client.run,
            session_spec=_spec(tmp_path),
            turn=AgentTurnRequest(
                message="evaluate my candidate", invocation_id="waiting-turn", output_schema=_Reply
            ),
        )
    )
    await entered.wait(agent)
    control.land_park()
    workspaces = FakeWorkspaces(FakeWorkspace(path=tmp_path), supports_parallel_candidates=True)
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    executor = PollingEvaluationExecutor(
        evaluation, workspaces, clock=ManualClock(), control=control
    )
    snapshot = await workspaces.root.snapshot("candidate")
    request = _request(snapshot).model_copy(
        update={"owner_scope": "run:run", "owner_generation": 0}
    )
    control.prepare_evaluation(request.key, owner)
    await executor.submit(request, handle_id=_HANDLE)
    assert (await _terminal(executor)).state is EvaluationState.SUCCEEDED
    assert len(evaluation.benchmark_calls) == 1
    evaluated.set()
    await agent
    assert not control.has_executing_owner("run:run", 0)
    await executor.close()


async def test_pause_drain_cancels_scheduler_queued_work_before_confirming_application() -> None:
    control = create_run_control_channel(FakeRunControlEventSink(), threads=loop_threads())
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    async with world(cluster) as w:
        w.control = control
        accepted = await submit(w)
        resource = resource_of(accepted)
        await settled(w, resource)
        assert (await w.stack.executor.poll(resource.root)).phase is PollPhase.QUEUED
        control.land_park((resource.root,))
        assert not control.drain_ready
        drain = query(CancelOwnedJob, "pause-drain", resource).model_copy(update={"for_park": True})
        released = await run(w, drain)
        assert released.observation.observation.released
        assert control.drain_ready
        assert resource.root in cluster.cancelled
        assert (await w.stack.executor.poll(resource.root)).phase is PollPhase.ENDED


def test_pause_drain_job_confirmation_never_requests_a_stop() -> None:
    channel = create_run_control_channel(FakeRunControlEventSink(), threads=SimThreads())
    stops: list[bool] = []
    channel.on_stop_requested(lambda: stops.append(True))
    channel.land_park(("queued-job",))
    assert not channel.drain_ready
    channel.settle_drain_job("queued-job")
    assert channel.drain_ready
    assert not stops
    assert not channel.stop_requested()
