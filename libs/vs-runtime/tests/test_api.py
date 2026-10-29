from __future__ import annotations

import asyncio
import importlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, Json, ValidationError

from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentCapability,
    AgentRole,
    AgentTurnTimeoutError,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CommandResult,
    CorePlan,
    CorePolicy,
    CoreRunContext,
    LocalValidationEvaluation,
    MetricDirection,
    OrchestrationPlugin,
    ProfileExecution,
    RunFacts,
    RuntimeContractError,
    WorkspaceAccess,
    WorkspaceRestoreError,
    WorkspaceSourceFact,
    validate_workspace_writable_paths,
)
from vs_runtime.api.testing import FakeCommands, FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api import DISCARD


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


class _AbstractModel(BaseModel, ABC):
    @abstractmethod
    def apply(self) -> None:
        """Apply model-specific behavior."""


def _role(role_id: str = "implementer") -> AgentRole:
    return AgentRole(
        id=role_id,
        system_prompt="Improve the candidate.",
        workspace_access=WorkspaceAccess.READ_WRITE,
        required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
    )


def _policy() -> CorePolicy:
    def plan(_context: CoreRunContext) -> CorePlan:
        raise AssertionError

    return CorePolicy(plan=plan, operations=(), retention_label="selected", prompt_templates=Path())


def _plugin(*agents: AgentRole) -> OrchestrationPlugin:
    return OrchestrationPlugin(id="test-plugin", agents=agents, options=_Options, core=_policy())


class _Run:
    """The fake capabilities a contract test drives, over one fake root workspace."""

    def __init__(
        self,
        *,
        run_id: str = "test-run",
        project_root: Path = Path(),
        supports_parallel_candidates: bool = False,
    ) -> None:
        self.run_id = run_id
        self.workspaces = FakeWorkspaces(
            FakeWorkspace(path=project_root),
            supports_parallel_candidates=supports_parallel_candidates,
        )
        self.commands = FakeCommands()
        self.evaluation = FakeEvaluation(run_id=run_id)

    async def close(self) -> None:
        self.workspaces.begin_close()
        await self.workspaces.close()


def _workspace() -> FakeWorkspace:
    return FakeWorkspace(workspace_id="workspace-a", path=Path("/workspace-a"))


def test_role_is_a_strict_complete_declaration() -> None:
    role = _role()

    assert role.id == "implementer"
    assert role.system_prompt == "Improve the candidate."
    assert role.extra_tools == ()
    for unknown_field in ("config_profile", "skills", "tools"):
        with pytest.raises(ValidationError, match=unknown_field):
            AgentRole.model_validate(
                {
                    "id": "implementer",
                    "system_prompt": "prompt",
                    unknown_field: ("unsupported",),
                }
            )
    with pytest.raises(ValidationError, match="id"):
        _role("Bad Role")
    assert len(_role("a" * 128).id) == 128
    with pytest.raises(ValidationError, match="id"):
        _role("a" * 129)


def test_agent_turn_timeout_error_preserves_the_policy_budget() -> None:
    error = AgentTurnTimeoutError(12.5)

    assert error.timeout_seconds == 12.5
    assert str(error) == "agent turn timed out after 12.5 seconds"
    assert isinstance(error, RuntimeError)


def test_run_facts_are_strict_and_immutable() -> None:
    facts = RunFacts(
        agent_git_writable=True,
        domain_id="llm_serving",
        objective="Increase serving throughput.",
        environment_notes="Run the service through its public endpoint.",
        profile_execution=ProfileExecution.REMOTE,
        workspace_sources=(WorkspaceSourceFact(name="runtime", dest="src/runtime"),),
    )
    with pytest.raises(ValidationError):
        facts.__setattr__("domain_id", "generic")
    with pytest.raises(ValidationError):
        RunFacts.model_validate({"domain_id": "generic", "backend": "modal"})
    with pytest.raises(ValidationError):
        RunFacts.model_validate({"domain_id": "generic"})
    with pytest.raises(ValidationError):
        facts.workspace_sources[0].__setattr__("dest", "other")
    with pytest.raises(ValidationError):
        RunFacts(
            agent_git_writable=True,
            domain_id="generic",
            objective="Improve the candidate.",
            objective_location="",
        )


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
        run = _Run(project_root=Path("/project"), supports_parallel_candidates=True)
        root_revision = run.workspaces.root.revision
        assert root_revision is not None
        assert run.workspaces.supports_parallel_candidates

        await asyncio.gather(
            run.workspaces.create_candidate(root_revision),
            run.workspaces.create_candidate(root_revision),
        )
        first, second = run.workspaces.candidates
        assert first.id == "candidate-1"
        assert second.id == "candidate-2"
        assert first.path != second.path
        assert first.path != run.workspaces.root.path

        first_revision = await first.snapshot("candidate one")
        second_revision = await second.snapshot("candidate two")
        assert first_revision != second_revision
        run.workspaces.set_patch(first_revision, "diff --git a/queue.py b/queue.py")
        assert (
            await run.workspaces.export_patch(first_revision) == "diff --git a/queue.py b/queue.py"
        )
        run.workspaces.set_default_patch("diff --git a/base.py b/base.py")
        assert (
            await run.workspaces.export_patch(second_revision) == "diff --git a/base.py b/base.py"
        )
        assert (
            await run.workspaces.export_patch(first_revision) == "diff --git a/queue.py b/queue.py"
        )

        await first.discard()
        await first.discard()
        assert first.discarded
        with pytest.raises(RuntimeContractError, match="closed"):
            await first.snapshot("too late")

        await run.workspaces.adopt(first_revision)
        assert await run.workspaces.root.try_restore(first_revision)
        with pytest.raises(RuntimeContractError, match="not retained"):
            await run.workspaces.adopt("other-run-revision")

        await run.close()
        assert second.discarded

    asyncio.run(scenario())


def test_fake_candidate_creation_rejects_unsupported_runs() -> None:
    async def scenario() -> None:
        run = _Run(supports_parallel_candidates=False)
        assert not run.workspaces.supports_parallel_candidates
        with pytest.raises(RuntimeContractError, match="does not support"):
            await run.workspaces.create_candidate()

    asyncio.run(scenario())


def test_fake_commands_are_argv_based_scriptable_and_recorded() -> None:
    async def scenario() -> None:
        run = _Run()
        workspace = run.workspaces.root
        expected = CommandResult(
            output="profile data",
            exit_code=7,
            truncated=True,
            diagnostic_output="profile data",
        )
        run.commands.script(expected)

        result = await run.commands.run(
            ("profiler", "--label", "value with spaces"),
            workspace=workspace,
            timeout_seconds=30,
        )

        assert result is expected
        assert run.commands.calls[0].argv == (
            "profiler",
            "--label",
            "value with spaces",
        )
        assert run.commands.calls[0].workspace is workspace
        assert run.commands.calls[0].timeout_seconds == 30
        assert run.commands.calls[0].output_argument is None

        captured = CommandResult(output='{"version":1}', exit_code=0)
        run.commands.script(captured)
        result = await run.commands.capture_output(
            ("profiler", "--mode", "summary"),
            workspace=workspace,
            output_argument="--result-file",
            timeout_seconds=45,
        )
        assert result is captured
        assert run.commands.calls[1].argv == ("profiler", "--mode", "summary")
        assert run.commands.calls[1].output_argument == "--result-file"
        assert run.commands.calls[1].timeout_seconds == 45

    asyncio.run(scenario())


def test_command_result_derives_diagnostic_only_from_complete_output() -> None:
    result = CommandResult(output="API_TOKEN=runtime-secret", exit_code=1)

    assert result.output == "API_TOKEN=runtime-secret"
    assert "runtime-secret" not in result.diagnostic_output


def test_command_result_rejects_truncation_without_a_prebounded_diagnostic() -> None:
    with pytest.raises(ValidationError, match="requires diagnostic_output"):
        CommandResult(output="OKEN=runtime-secret", exit_code=1, truncated=True)


def test_command_result_derives_an_empty_diagnostic_for_empty_truncated_output() -> None:
    result = CommandResult(output="", exit_code=1, truncated=True)

    assert result.diagnostic_output == ""


def test_command_result_preserves_an_explicitly_empty_diagnostic() -> None:
    result = CommandResult(
        output="API_TOKEN=runtime-secret",
        exit_code=1,
        truncated=True,
        diagnostic_output="",
    )

    assert result.diagnostic_output == ""


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
        run = _Run()
        with pytest.raises(ValueError, match="command"):
            await run.commands.run(
                argv,
                workspace=run.workspaces.root,
                timeout_seconds=timeout_seconds,
            )
        assert run.commands.calls == []

    asyncio.run(scenario())


def test_plugin_rejects_invalid_id() -> None:
    with pytest.raises(ValueError, match="invalid orchestration plugin ID"):
        OrchestrationPlugin(
            id="Invalid Plugin",
            agents=(_role(),),
            options=_Options,
            core=_policy(),
        )


@pytest.mark.parametrize("invalid_options", [int, _Options(), BaseModel, _AbstractModel])
def test_plugin_requires_concrete_options_model(invalid_options: object) -> None:
    with pytest.raises(ValueError, match="options must be a concrete BaseModel subclass"):
        OrchestrationPlugin(
            id="invalid-options",
            agents=(),
            options=cast("Any", invalid_options),
            core=_policy(),
        )


def test_fake_evaluation_preserves_semantic_results_and_requests() -> None:
    async def scenario() -> None:
        run = _Run()
        workspace = run.workspaces.root
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
        run.evaluation.script_accuracy(accuracy)
        run.evaluation.script_benchmark(benchmark)
        run.evaluation.script_local_validation(local_validation)

        accuracy_result = await run.evaluation.accuracy(workspace, output=DISCARD)
        assert accuracy_result.executed
        assert accuracy_result.receipt is not None
        assert (
            await run.evaluation.benchmark(workspace, objectives=(objective,), output=DISCARD)
            == benchmark
        )
        assert (
            await run.evaluation.validate_local(
                workspace,
                recipe_artifact="progress/validation/recipes.json",
                report_location="progress/validation/round-1.json",
            )
            == local_validation
        )
        assert run.evaluation.accuracy_calls[0].workspace is workspace
        assert run.evaluation.benchmark_calls[0].objectives == (objective,)
        assert run.evaluation.local_validation_calls[0].recipe_artifact.endswith("recipes.json")
        assert accuracy_result.passed
        assert benchmark.passed

        serialized = accuracy_result.receipt.model_dump_json()
        restored = AccuracyReceipt.model_validate_json(serialized)
        resumed_host = _Run(run_id=run.run_id, project_root=workspace.path)
        reused = await resumed_host.evaluation.accuracy(
            resumed_host.workspaces.root,
            reuse=restored,
            output=DISCARD,
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


@pytest.mark.parametrize(
    "path",
    [
        "../recipe.json",
        "/recipe.json",
        ".",
        "bad\\path.json",
        "recipes.txt",
        "Accuracy checker: expects cached_tokens > 0 on a repeated prompt",
    ],
)
def test_fake_local_validation_reports_unusable_recipe_references_as_input_errors(
    path: str,
) -> None:
    async def scenario() -> None:
        run = _Run()
        result = await run.evaluation.validate_local(
            run.workspaces.root,
            recipe_artifact=path,
            report_location="progress/validation/round-1.json",
        )
        assert not result.passed
        assert result.recipe_unusable
        assert result.feedback is not None
        assert "canonical workspace-relative" in result.feedback
        assert run.evaluation.local_validation_calls == []

    asyncio.run(scenario())


def test_fake_local_validation_rejects_a_noncanonical_report_location() -> None:
    async def scenario() -> None:
        run = _Run()
        with pytest.raises(ValueError, match="writable path"):
            await run.evaluation.validate_local(
                run.workspaces.root,
                recipe_artifact="progress/validation/recipes.json",
                report_location="../report.json",
            )

    asyncio.run(scenario())


def test_fake_evaluation_rejects_duplicate_objective_names() -> None:
    async def scenario() -> None:
        run = _Run()
        objective = BenchmarkObjective(name="latency", direction=MetricDirection.MINIMIZE)
        with pytest.raises(ValueError, match="objective names must be unique"):
            await run.evaluation.benchmark(
                run.workspaces.root,
                objectives=(objective, objective),
                output=DISCARD,
            )
        assert run.evaluation.benchmark_calls == []

    asyncio.run(scenario())


def test_the_runtime_facade_resolves_and_caches_every_declared_export() -> None:
    facade = importlib.import_module("vs_runtime.api")
    assert set(dir(facade)) == set(facade.__all__)
    for name in facade.__all__:
        assert getattr(facade, name) is getattr(facade, name)
    with pytest.raises(AttributeError):
        _ = facade.not_a_public_export
