from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, Json, ValidationError

from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentCapability,
    AgentRole,
    AgentTool,
    BenchmarkEvaluation,
    BenchmarkObjective,
    MetricDirection,
    OrchestrationPlugin,
    RunHost,
    RunStatus,
    SessionClosedError,
    StateModelError,
    UnknownAgentRoleError,
    WorkspaceAccess,
    WorkspaceRef,
)
from vs_runtime.api.testing import FakeRunHost


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds: int = 1


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: list[int] = Field(default_factory=list)


class _OtherState(BaseModel):
    value: int


class _JsonState(BaseModel):
    value: Json[Any]


def _role(role_id: str = "implementer") -> AgentRole:
    return AgentRole(
        id=role_id,
        system_prompt="Improve the candidate.",
        tools=(AgentTool(id="shell"),),
        skills=("profiling",),
        workspace_access=WorkspaceAccess.READ_WRITE,
        required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
    )


async def _orchestrate(_host: RunHost, _options: BaseModel) -> RunStatus:
    return RunStatus.SUCCEEDED


def _plugin(*agents: AgentRole, state: type[BaseModel] | None = None) -> OrchestrationPlugin:
    return OrchestrationPlugin(
        id="test-plugin",
        agents=agents,
        options=_Options,
        orchestrate=_orchestrate,
        state=state,
    )


def _workspace() -> WorkspaceRef:
    return WorkspaceRef(id="workspace-a", path=Path("/workspace-a"))


def test_role_is_a_strict_complete_declaration() -> None:
    role = _role()

    assert role.id == "implementer"
    assert role.system_prompt == "Improve the candidate."
    assert role.tools == (AgentTool(id="shell"),)
    with pytest.raises(ValidationError):
        AgentRole.model_validate(
            {
                "id": "implementer",
                "system_prompt": "prompt",
                "config_profile": "hidden alias",
            }
        )


def test_same_session_continues_and_second_creation_is_fresh() -> None:
    observed_history_lengths: list[int] = []

    def respond(
        _role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        observed_history_lengths.append(len(history))
        return message if response is None else {"answer": message}

    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role), responder=respond)
        first = await host.agents.create_session(
            role, workspace=_workspace(), member_id="candidate-1"
        )
        assert await first.turn("one") == "one"
        assert await first.turn("two", response=_Reply) == _Reply(answer="two")
        second = await host.agents.create_session(role, workspace=_workspace())
        assert await second.turn("fresh") == "fresh"
        assert first.role == role
        assert first.workspace == _workspace()
        assert first.member_id == "candidate-1"
        await host.close()

    asyncio.run(scenario())
    assert observed_history_lengths == [0, 1, 0]


def test_factory_rejects_role_not_declared_by_plugin() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        with pytest.raises(UnknownAgentRoleError, match="reviewer"):
            await host.agents.create_session(_role("reviewer"), workspace=_workspace())

    asyncio.run(scenario())


def test_run_owner_closes_sessions_and_closed_turn_fails() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        session = await host.agents.create_session(role, workspace=_workspace())
        await host.close()
        await host.close()
        assert session.closed
        with pytest.raises(SessionClosedError):
            await session.turn("late")

    asyncio.run(scenario())


def test_session_can_close_early_idempotently() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        session = await host.agents.create_session(role, workspace=_workspace())
        await session.close()
        await session.close()
        assert session.closed
        await host.close()

    asyncio.run(scenario())


def test_plugin_agents_tuple_is_unique_source_of_truth() -> None:
    role = _role()
    plugin = _plugin(role)

    assert plugin.id == "test-plugin"
    assert plugin.agents == (role,)
    assert plugin.options is _Options
    with pytest.raises(ValueError, match="duplicate agent role IDs"):
        _plugin(role, role)


def test_session_rejects_invalid_member_id_before_creation() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        with pytest.raises(ValueError, match="invalid agent member ID"):
            await host.agents.create_session(
                role, workspace=_workspace(), member_id="invalid member"
            )
        assert host.agents.sessions == ()

    asyncio.run(scenario())


def test_plugin_rejects_invalid_id() -> None:
    with pytest.raises(ValueError, match="invalid orchestration plugin ID"):
        OrchestrationPlugin(
            id="Invalid Plugin",
            agents=(_role(),),
            options=_Options,
            orchestrate=_orchestrate,
        )


def test_fake_evaluation_preserves_semantic_results_and_requests() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        workspace = host.workspaces.root
        objective = BenchmarkObjective(name="tokens_per_second", direction=MetricDirection.MAXIMIZE)
        accuracy = AccuracyEvaluation(executed=True)
        benchmark = BenchmarkEvaluation(
            executed=True,
            metric_name="tokens_per_second",
            metric_value=42.0,
            metric_direction=MetricDirection.MAXIMIZE,
            row={"tokens_per_second": 42.0},
        )
        host.evaluation.script_accuracy(accuracy)
        host.evaluation.script_benchmark(benchmark)

        accuracy_result = await host.evaluation.accuracy(workspace)
        assert accuracy_result.executed
        assert accuracy_result.receipt is not None
        assert await host.evaluation.benchmark(workspace, objectives=(objective,)) == benchmark
        assert host.evaluation.accuracy_calls[0].workspace is workspace
        assert host.evaluation.benchmark_calls[0].objectives == (objective,)
        assert accuracy_result.passed
        assert benchmark.passed

        serialized = accuracy_result.receipt.model_dump_json()
        restored = AccuracyReceipt.model_validate_json(serialized)
        resumed_host = FakeRunHost(
            _plugin(_role()), run_id=host.run_id, project_root=workspace.path
        )
        reused = await resumed_host.evaluation.accuracy(
            resumed_host.workspaces.root, reuse=restored
        )
        assert reused == AccuracyEvaluation(executed=False, receipt=restored)
        assert resumed_host.evaluation.accuracy_calls[-1].reuse == restored

    asyncio.run(scenario())


def test_fake_evaluation_rejects_duplicate_objective_names() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        objective = BenchmarkObjective(name="latency", direction=MetricDirection.MINIMIZE)
        with pytest.raises(ValueError, match="objective names must be unique"):
            await host.evaluation.benchmark(
                host.workspaces.root,
                objectives=(objective, objective),
            )
        assert host.evaluation.benchmark_calls == []

    asyncio.run(scenario())


def test_fake_state_is_plugin_bound_and_stores_detached_values() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role(), state=_State))
        assert await host.state.load(_State) is None

        value = _State(values=[1])
        await host.state.commit(value, label="state only")
        value.values.append(2)

        assert await host.state.load(_State) == _State(values=[1])
        assert host.state.commits[0].value == _State(values=[1])
        assert host.state.commits[0].workspace is None

        await host.state.commit(
            _State(values=[3]),
            workspace=host.workspaces.root,
            label="with workspace",
        )
        assert host.state.commits[-1].workspace is host.workspaces.root

        with pytest.raises(StateModelError, match="requires _State, got _OtherState"):
            await host.state.load(_OtherState)
        with pytest.raises(StateModelError, match="requires _State, got _OtherState"):
            await host.state.commit(_OtherState(value=1))
        with pytest.raises(RuntimeError, match="live root workspace"):
            await host.state.commit(_State(), workspace=_workspace())

    asyncio.run(scenario())


def test_fake_state_rejects_operations_when_plugin_declares_none() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        with pytest.raises(StateModelError, match="does not declare durable state"):
            await host.state.load(_State)
        with pytest.raises(StateModelError, match="does not declare durable state"):
            await host.state.commit(_State())

    asyncio.run(scenario())


def test_fake_state_preserves_round_trip_pydantic_values() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(state=_JsonState))
        value = _JsonState(value='{"nested":[1,2]}')

        await host.state.commit(value)

        assert await host.state.load(_JsonState) == value
        assert host.state.commits[0].value == value

    asyncio.run(scenario())
