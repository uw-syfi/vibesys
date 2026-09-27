from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, Json, ValidationError

from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentBinding,
    AgentCapability,
    AgentRole,
    AgentTool,
    AgentTurnTimeoutError,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CommandResult,
    LocalValidationEvaluation,
    MetricDirection,
    OrchestrationPlugin,
    PluginProjection,
    ProfileExecution,
    ProjectedRound,
    RunFacts,
    RunHost,
    RunStatus,
    RuntimeContractError,
    SessionClosedError,
    SkillCatalogError,
    SkillResourceRequest,
    StateModelError,
    UnknownAgentRoleError,
    WorkspaceAccess,
    WorkspaceRestoreError,
    WorkspaceSourceFact,
    validate_workspace_writable_paths,
)
from vs_runtime.api.testing import FakeRunHost, FakeWorkspace


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


def _workspace() -> FakeWorkspace:
    return FakeWorkspace(workspace_id="workspace-a", path=Path("/workspace-a"))


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


def test_agent_turn_timeout_error_preserves_the_policy_budget() -> None:
    error = AgentTurnTimeoutError(12.5)

    assert error.timeout_seconds == 12.5
    assert str(error) == "agent turn timed out after 12.5 seconds"
    assert isinstance(error, RuntimeError)


def test_run_facts_are_strict_immutable_and_configurable_on_fake_host() -> None:
    facts = RunFacts(
        domain_id="llm_serving",
        objective="Increase serving throughput.",
        environment_notes="Run the service through its public endpoint.",
        profile_execution=ProfileExecution.REMOTE,
        workspace_sources=(WorkspaceSourceFact(name="runtime", dest="src/runtime"),),
    )
    host = FakeRunHost(_plugin(), facts=facts)

    assert host.facts is facts
    with pytest.raises(ValidationError):
        host.facts.__setattr__("domain_id", "generic")
    with pytest.raises(ValidationError):
        RunFacts.model_validate({"domain_id": "generic", "backend": "modal"})
    with pytest.raises(ValidationError):
        RunFacts.model_validate({"domain_id": "generic"})
    with pytest.raises(ValidationError):
        facts.workspace_sources[0].__setattr__("dest", "other")
    with pytest.raises(ValidationError):
        RunFacts(domain_id="generic", objective="Improve the candidate.", objective_location="")


def test_fake_run_facts_have_a_policy_neutral_default() -> None:
    host = FakeRunHost(_plugin())

    assert host.facts == RunFacts(domain_id="generic", objective="Test objective.")
    assert host.facts.objective == "Test objective."
    assert host.facts.objective_location == "OBJECTIVE.md"
    assert host.facts.reference_location == "."
    assert host.facts.profiler_id == "none"


def test_limited_session_grants_are_validated_and_fixed() -> None:
    async def scenario() -> None:
        role = AgentRole(
            id="writer",
            system_prompt="Write only in the granted paths.",
            workspace_access=WorkspaceAccess.LIMITED,
        )
        regular_role = _role()
        host = FakeRunHost(_plugin(role, regular_role))
        session = await host.agents.create_session(
            role,
            workspace=_workspace(),
            writable_paths=("memory", "evidence/report.json"),
        )

        assert session.writable_paths == ("memory", "evidence/report.json")
        writable_paths_attribute = "writable_paths"
        with pytest.raises(AttributeError):
            setattr(session, writable_paths_attribute, ("elsewhere",))

        with pytest.raises(ValueError, match="requires writable_paths"):
            await host.agents.create_session(role, workspace=_workspace())
        with pytest.raises(ValueError, match="cannot declare writable_paths"):
            await host.agents.create_session(
                regular_role,
                workspace=_workspace(),
                writable_paths=("memory",),
            )
        await host.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("", id="empty"),
        pytest.param(".", id="workspace-root"),
        pytest.param("./notes", id="dot-component"),
        pytest.param("../notes", id="parent-component"),
        pytest.param("memory/../notes", id="nested-parent"),
        pytest.param("/workspace/notes", id="absolute-posix"),
        pytest.param("C:/notes", id="absolute-windows"),
        pytest.param(r"memory\notes", id="windows-separator"),
        pytest.param("memory//notes", id="duplicate-separator"),
    ],
)
def test_writable_path_grants_reject_noncanonical_or_escaping_paths(
    path: str,
) -> None:
    with pytest.raises(ValueError, match="writable path"):
        validate_workspace_writable_paths(WorkspaceAccess.LIMITED, (path,))


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
        workspace = _workspace()
        first = await host.agents.create_session(role, workspace=workspace, member_id="candidate-1")
        assert await first.turn("one") == "one"
        assert await first.turn("two", response=_Reply) == _Reply(answer="two")
        second = await host.agents.create_session(role, workspace=_workspace())
        assert await second.turn("fresh") == "fresh"
        assert first.role == role
        assert first.workspace is workspace
        assert first.member_id == "candidate-1"
        assert first.binding == AgentBinding(backend="fake", driver="fake", provider="fake")
        await host.close()

    asyncio.run(scenario())
    assert observed_history_lengths == [0, 1, 0]


def test_fake_read_write_turns_snapshot_input_and_completed_output() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        workspace = host.workspaces.root
        initial = workspace.revision
        session = await host.agents.create_session(role, workspace=workspace)

        assert await session.turn("write") == "write"
        assert workspace.revision != initial
        assert workspace.revision == "fake-revision-2"

    asyncio.run(scenario())


def test_fake_binding_is_explicitly_configurable_and_immutable() -> None:
    async def scenario() -> None:
        role = _role()
        binding = AgentBinding(
            backend="cli",
            driver="agentshim",
            provider="codex",
            model="gpt-6-sol",
            reasoning_effort="high",
        )
        host = FakeRunHost(_plugin(role), agent_bindings={role.id: binding})
        session = await host.agents.create_session(role, workspace=_workspace())

        assert session.binding is binding
        with pytest.raises(ValidationError):
            binding.__setattr__("model", "other")

    asyncio.run(scenario())


def test_fake_session_creation_failures_are_scriptable_without_leaking_sessions() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        host.agents.script_creation(RuntimeError("construction failed"), None)

        with pytest.raises(RuntimeError, match="construction failed"):
            await host.agents.create_session(role, workspace=host.workspaces.root)
        assert host.agents.sessions == ()

        session = await host.agents.create_session(role, workspace=host.workspaces.root)
        assert host.agents.sessions == (session,)

    asyncio.run(scenario())


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


def test_fake_workspace_models_root_revision_operations() -> None:
    async def scenario() -> None:
        workspace = FakeWorkspace(
            path=Path("/workspace"),
            revision="input-revision",
            trusted_input_baseline="trusted-baseline",
        )
        assert workspace.id is None
        assert workspace.path == Path("/workspace")
        assert workspace.revision == "input-revision"
        assert workspace.trusted_input_baseline == "trusted-baseline"

        candidate = await workspace.snapshot("implemented plan")
        assert candidate == workspace.revision
        assert await workspace.retain(candidate, label="selected-round-0004") is None
        assert workspace.retained == {"selected-round-0004": candidate}

        await workspace.restore("trusted-baseline", clean=False)
        assert workspace.revision == candidate
        assert await workspace.try_restore(candidate)
        assert not await workspace.try_restore("missing")
        with pytest.raises(WorkspaceRestoreError, match="missing"):
            await workspace.restore("missing")

    asyncio.run(scenario())


def test_fake_candidate_workspaces_are_isolated_retained_and_run_owned() -> None:
    async def scenario() -> None:
        host = FakeRunHost(
            _plugin(),
            project_root=Path("/project"),
            supports_parallel_candidates=True,
        )
        root_revision = host.workspaces.root.revision
        assert root_revision is not None
        assert host.workspaces.supports_parallel_candidates

        await asyncio.gather(
            host.workspaces.create_candidate(root_revision),
            host.workspaces.create_candidate(root_revision),
        )
        first, second = host.workspaces.candidates
        assert first.id == "candidate-1"
        assert second.id == "candidate-2"
        assert first.path != second.path
        assert first.path != host.workspaces.root.path

        first_revision = await first.snapshot("candidate one")
        second_revision = await second.snapshot("candidate two")
        assert first_revision != second_revision
        host.workspaces.set_patch(first_revision, "diff --git a/queue.py b/queue.py")
        assert (
            await host.workspaces.export_patch(first_revision) == "diff --git a/queue.py b/queue.py"
        )

        await first.discard()
        await first.discard()
        assert first.discarded
        with pytest.raises(RuntimeContractError, match="closed"):
            await first.snapshot("too late")

        await host.workspaces.adopt(first_revision)
        assert await host.workspaces.root.try_restore(first_revision)
        with pytest.raises(RuntimeContractError, match="not retained"):
            await host.workspaces.adopt("other-run-revision")

        await host.close()
        assert second.discarded

    asyncio.run(scenario())


def test_fake_candidate_creation_rejects_unsupported_runs() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(), supports_parallel_candidates=False)
        assert not host.workspaces.supports_parallel_candidates
        with pytest.raises(RuntimeContractError, match="does not support"):
            await host.workspaces.create_candidate()

    asyncio.run(scenario())


def test_fake_candidate_discard_invalidates_bound_sessions() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role), supports_parallel_candidates=True)
        candidate = await host.workspaces.create_candidate()
        candidate_sessions = (
            await host.agents.create_session(role, workspace=candidate),
            await host.agents.create_session(role, workspace=candidate),
        )
        root_session = await host.agents.create_session(role, workspace=host.workspaces.root)

        await candidate.discard()
        await candidate.discard()

        assert not root_session.closed
        for session in candidate_sessions:
            assert session.closed
            with pytest.raises(SessionClosedError):
                await session.turn("too late")

    asyncio.run(scenario())


def test_fake_commands_are_argv_based_scriptable_and_recorded() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        workspace = host.workspaces.root
        expected = CommandResult(output="profile data", exit_code=7, truncated=True)
        host.commands.script(expected)

        result = await host.commands.run(
            ("profiler", "--label", "value with spaces"),
            workspace=workspace,
            timeout_seconds=30,
        )

        assert result is expected
        assert host.commands.calls[0].argv == (
            "profiler",
            "--label",
            "value with spaces",
        )
        assert host.commands.calls[0].workspace is workspace
        assert host.commands.calls[0].timeout_seconds == 30
        assert host.commands.calls[0].output_argument is None

        captured = CommandResult(output='{"version":1}', exit_code=0)
        host.commands.script(captured)
        result = await host.commands.capture_output(
            ("profiler", "--mode", "summary"),
            workspace=workspace,
            output_argument="--result-file",
            timeout_seconds=45,
        )
        assert result is captured
        assert host.commands.calls[1].argv == ("profiler", "--mode", "summary")
        assert host.commands.calls[1].output_argument == "--result-file"
        assert host.commands.calls[1].timeout_seconds == 45

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("argv", "timeout_seconds"),
    [
        ((), None),
        (("",), None),
        (("command", "bad\0argument"), None),
        (("command",), 0),
    ],
)
def test_fake_commands_reject_invalid_requests_before_recording(
    argv: tuple[str, ...], timeout_seconds: int | None
) -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        with pytest.raises(ValueError, match="command"):
            await host.commands.run(
                argv,
                workspace=host.workspaces.root,
                timeout_seconds=timeout_seconds,
            )
        assert host.commands.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("member_id", ["", "   ", "member\nline"])
def test_session_rejects_invalid_member_id_before_creation(member_id: str) -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        with pytest.raises(ValueError, match="invalid agent member ID"):
            await host.agents.create_session(role, workspace=_workspace(), member_id=member_id)
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


def test_plugin_projection_contract_is_strict_and_immutable() -> None:
    projection = PluginProjection(
        payload={"summary": "complete"},
        rounds=(ProjectedRound(number=1, status="completed", attempts=2),),
        experiment_revision=4,
    )

    assert projection.rounds[0].attempts == 2
    with pytest.raises(ValidationError):
        projection.__setattr__("experiment_revision", 5)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PluginProjection.model_validate({"payload": None, "unknown": True})
    with pytest.raises(ValidationError):
        ProjectedRound.model_validate({"number": "1", "status": "completed", "attempts": 2})


def test_plugin_projection_requires_declared_state() -> None:
    with pytest.raises(ValueError, match="projection requires a declared state model"):
        OrchestrationPlugin(
            id="stateless-projector",
            agents=(),
            options=_Options,
            orchestrate=_orchestrate,
            project=lambda _state: PluginProjection(payload=None),
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
        local_validation = LocalValidationEvaluation(
            passed=False,
            feedback="focused tests failed",
            report_location="progress/validation/round-1.json",
        )
        host.evaluation.script_accuracy(accuracy)
        host.evaluation.script_benchmark(benchmark)
        host.evaluation.script_local_validation(local_validation)

        accuracy_result = await host.evaluation.accuracy(workspace)
        assert accuracy_result.executed
        assert accuracy_result.receipt is not None
        assert await host.evaluation.benchmark(workspace, objectives=(objective,)) == benchmark
        assert (
            await host.evaluation.validate_local(
                workspace,
                recipe_artifact="progress/validation/recipes.json",
                report_location="progress/validation/round-1.json",
            )
            == local_validation
        )
        assert host.evaluation.accuracy_calls[0].workspace is workspace
        assert host.evaluation.benchmark_calls[0].objectives == (objective,)
        assert host.evaluation.local_validation_calls[0].recipe_artifact.endswith("recipes.json")
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


@pytest.mark.parametrize(
    "case",
    [(True, "unexpected"), (False, None)],
)
def test_local_validation_result_requires_consistent_feedback(
    case: tuple[bool, str | None],
) -> None:
    passed, feedback = case
    with pytest.raises(ValidationError, match="failure requires feedback"):
        LocalValidationEvaluation(passed=passed, feedback=feedback)


@pytest.mark.parametrize("path", ["../recipe.json", "/recipe.json", ".", "bad\\path"])
def test_fake_local_validation_rejects_noncanonical_paths(path: str) -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        with pytest.raises(ValueError, match="canonical workspace-relative"):
            await host.evaluation.validate_local(
                host.workspaces.root,
                recipe_artifact=path,
                report_location="progress/validation/round-1.json",
            )
        assert host.evaluation.local_validation_calls == []

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


def test_fake_skills_resolve_partial_selections_and_merge_in_order() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        host.skills.installed_resources = {
            "profiling": ("SKILL.md", "guide.md", "references/metrics.md"),
        }
        result = await host.skills.resolve(
            (
                SkillResourceRequest(
                    name="profiling",
                    resource_paths=(
                        "SKILL.md",
                        "guide.md",
                        "guide.md",
                        "missing.md",
                        "../outside.md",
                    ),
                    purpose="inspect the profiler",
                ),
                SkillResourceRequest(
                    name="unknown",
                    purpose="not installed",
                ),
                SkillResourceRequest(
                    name="profiling",
                    resource_paths=("references/metrics.md",),
                    purpose="later duplicate purpose is ignored",
                ),
            )
        )

        assert len(result.resolved) == 1
        resolved = result.resolved[0]
        assert resolved.name == "profiling"
        assert resolved.router_path == "profiling/SKILL.md"
        assert resolved.resource_paths == (
            "profiling/guide.md",
            "profiling/references/metrics.md",
        )
        assert resolved.purpose == "inspect the profiler"
        assert len(result.diagnostics) == 3
        assert "resource file does not exist" in result.diagnostics[0]
        assert "must be relative" in result.diagnostics[1]
        assert "unknown installed skill" in result.diagnostics[2]

    asyncio.run(scenario())


def test_fake_skills_no_catalog_and_catalog_errors_are_distinct() -> None:
    async def scenario() -> None:
        request = SkillResourceRequest(name="profiling", purpose="inspect profiling")
        host = FakeRunHost(_plugin(_role()))
        absent = await host.skills.resolve((request,))
        assert absent.resolved == ()
        assert absent.diagnostics == ("no skill sources are installed",)

        host.skills.catalog_error = "invalid catalog"
        no_requests = await host.skills.resolve(())
        assert no_requests.resolved == ()
        assert no_requests.diagnostics == ()

        with pytest.raises(SkillCatalogError, match="invalid catalog"):
            await host.skills.resolve((request,))

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


def test_fake_state_scripts_commit_failures_without_replacing_durable_value() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(state=_State))
        await host.state.commit(_State(values=[1]))
        host.state.script_commit(None, RuntimeError("storage unavailable"))

        await host.state.commit(_State(values=[2]))
        with pytest.raises(RuntimeError, match="storage unavailable"):
            await host.state.commit(_State(values=[3]))

        assert await host.state.load(_State) == _State(values=[2])
        assert [commit.value for commit in host.state.commits] == [
            _State(values=[1]),
            _State(values=[2]),
        ]

    asyncio.run(scenario())


def test_fake_control_records_checkpoints_and_propagates_stop() -> None:
    class _Stopped(BaseException):
        pass

    async def scenario() -> None:
        host = FakeRunHost(_plugin())
        await host.control.checkpoint()
        assert host.control.checkpoints == 1

        host.control.fail_with(_Stopped())
        with pytest.raises(_Stopped):
            await host.control.checkpoint()
        assert host.control.checkpoints == 2

    asyncio.run(scenario())
