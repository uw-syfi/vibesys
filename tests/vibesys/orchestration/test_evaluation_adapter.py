"""Public semantic evaluation behavior over the production run host."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.context import RunSetup
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.fake_gates import FakeGateExecutor
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    BenchmarkEvaluation,
    BenchmarkObjective,
    LocalValidationEvaluation,
    MetricDirection,
    RuntimeContractError,
)
from vs_runtime.api.testing import FakeWorkspace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from typing import TypeVar

    _Result = TypeVar("_Result")


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path, *, agent_backend: str | None = None) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="evaluation", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "evaluation"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="evaluation",
        agent_backend=agent_backend,
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _run(
    tmp_path: Path,
    executor: FakeGateExecutor,
    body: Callable[[RunContext], Awaitable[_Result]],
    *,
    agent_backend: str | None = None,
) -> _Result:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> _Result:
        async with RunContext.open(
            _request(project_root, agent_backend=agent_backend),
            integration,
            setup=RunSetup(),
            gate_executor=executor,
        ) as ctx:
            return await body(ctx)

    try:
        return asyncio.run(exercise())
    finally:
        integration.close()


def test_adapter_returns_semantic_accuracy_and_benchmark_results(tmp_path: Path) -> None:
    executor = FakeGateExecutor()
    executor.script_accuracy(
        AccuracyGateResult(
            command="true",
            passed=True,
            output="internal command output",
            feedback=None,
            executed=True,
        )
    )
    executor.script_benchmark(
        BenchmarkGateResult(
            command="true",
            output="internal command output",
            executed=True,
            outcome=FrameworkBenchmarkOutcome(
                metric_name="throughput",
                metric_value=42.0,
                metric_direction="max",
                metric_unit="requests/s",
                row={"throughput": 42.0, "latency": 3.0},
            ),
        )
    )

    async def body(ctx: RunContext) -> tuple[AccuracyEvaluation, BenchmarkEvaluation]:
        accuracy = await ctx.evaluation.accuracy(ctx.workspaces.root)
        benchmark = await ctx.evaluation.benchmark(
            ctx.workspaces.root,
            objectives=(BenchmarkObjective(name="throughput", direction=MetricDirection.MAXIMIZE),),
        )
        return accuracy, benchmark

    accuracy, benchmark = _run(tmp_path, executor, body)

    assert accuracy.executed
    assert accuracy.passed
    assert accuracy.receipt is not None
    assert benchmark == BenchmarkEvaluation(
        executed=True,
        metric_name="throughput",
        metric_value=42.0,
        metric_direction=MetricDirection.MAXIMIZE,
        metric_unit="requests/s",
        row={"throughput": 42.0, "latency": 3.0},
    )
    assert benchmark.passed
    assert len(executor.accuracy_calls) == 1
    assert len(executor.benchmark_calls) == 1


def test_accuracy_receipt_round_trips_and_reuses_without_execution(tmp_path: Path) -> None:
    executor = FakeGateExecutor()
    executor.script_accuracy(
        AccuracyGateResult(
            command="true",
            passed=True,
            output="ok",
            feedback=None,
            executed=True,
        )
    )

    async def body(ctx: RunContext) -> tuple[AccuracyEvaluation, AccuracyEvaluation]:
        first = await ctx.evaluation.accuracy(ctx.workspaces.root)
        assert first.receipt is not None
        restored = AccuracyReceipt.model_validate_json(first.receipt.model_dump_json())
        framework_note = ctx.workspaces.root.path / ".vibesys" / "receipt-test.json"
        framework_note.parent.mkdir(parents=True, exist_ok=True)
        framework_note.write_text("{}\n")
        await ctx.workspaces.root.snapshot("incidental framework metadata")
        reused = await ctx.evaluation.accuracy(ctx.workspaces.root, reuse=restored)
        return first, reused

    first, reused = _run(tmp_path, executor, body)

    assert first.executed
    assert reused == AccuracyEvaluation(executed=False, receipt=first.receipt)
    assert len(executor.accuracy_calls) == 1


def test_adapter_preserves_failure_feedback_for_policy(tmp_path: Path) -> None:
    executor = FakeGateExecutor()
    executor.script_accuracy(
        AccuracyGateResult(
            command="false",
            passed=False,
            output="wrong answer",
            feedback="Framework accuracy gate failed.\nwrong answer",
            executed=True,
        )
    )

    async def body(ctx: RunContext) -> AccuracyEvaluation:
        return await ctx.evaluation.accuracy(ctx.workspaces.root)

    result = _run(tmp_path, executor, body)

    assert not result.passed
    assert result.feedback == "Framework accuracy gate failed.\nwrong answer"


def test_stub_backend_preserves_existing_evaluation_skip(tmp_path: Path) -> None:
    executor = FakeGateExecutor()

    async def body(ctx: RunContext) -> tuple[AccuracyEvaluation, BenchmarkEvaluation]:
        return (
            await ctx.evaluation.accuracy(ctx.workspaces.root),
            await ctx.evaluation.benchmark(ctx.workspaces.root),
        )

    accuracy, benchmark = _run(tmp_path, executor, body, agent_backend="stub")

    assert accuracy == AccuracyEvaluation(executed=False)
    assert benchmark == BenchmarkEvaluation(executed=False)
    assert executor.accuracy_calls == []
    assert executor.benchmark_calls == []


def test_adapter_rejects_duplicate_objectives_before_execution(tmp_path: Path) -> None:
    executor = FakeGateExecutor()

    async def body(ctx: RunContext) -> None:
        objective = BenchmarkObjective(name="latency", direction=MetricDirection.MINIMIZE)
        with pytest.raises(ValueError, match="objective names must be unique"):
            await ctx.evaluation.benchmark(
                ctx.workspaces.root,
                objectives=(objective, objective),
            )

    _run(tmp_path, executor, body)
    assert executor.benchmark_calls == []


def test_adapter_rejects_workspace_not_owned_by_the_run(tmp_path: Path) -> None:
    executor = FakeGateExecutor()

    async def body(ctx: RunContext) -> None:
        with pytest.raises(TypeError, match="live handle"):
            await ctx.evaluation.accuracy(FakeWorkspace(path=ctx.workspaces.root.path))

    _run(tmp_path, executor, body)
    assert executor.accuracy_calls == []


def test_adapter_rejects_receipt_after_candidate_changes(tmp_path: Path) -> None:
    executor = FakeGateExecutor()
    executor.script_accuracy(
        AccuracyGateResult(
            command="true",
            passed=True,
            output="ok",
            feedback=None,
            executed=True,
        )
    )

    async def body(ctx: RunContext) -> None:
        result = await ctx.evaluation.accuracy(ctx.workspaces.root)
        assert result.receipt is not None
        (ctx.workspaces.root.path / "queue.py").write_text("VALUE = 2\n")
        with pytest.raises(RuntimeContractError, match="candidate revision"):
            await ctx.evaluation.accuracy(ctx.workspaces.root, reuse=result.receipt)

    _run(tmp_path, executor, body)
    assert len(executor.accuracy_calls) == 1


@pytest.mark.parametrize("field", ["run_id", "revision"])
def test_adapter_rejects_foreign_or_stale_accuracy_receipt(tmp_path: Path, field: str) -> None:
    executor = FakeGateExecutor()

    async def body(ctx: RunContext) -> None:
        receipt = AccuracyReceipt(
            run_id=ctx.run_id,
            workspace_id=ctx.workspaces.root.id,
            revision=ctx.workspaces.root.revision or "missing",
        )
        invalid = receipt.model_copy(update={field: "not-current"})
        with pytest.raises(RuntimeContractError, match=r"another run|revision"):
            await ctx.evaluation.accuracy(ctx.workspaces.root, reuse=invalid)

    _run(tmp_path, executor, body)
    assert executor.accuracy_calls == []


def test_local_validation_executes_then_reuses_an_exact_pass(tmp_path: Path) -> None:
    executor = FakeGateExecutor()

    async def body(
        ctx: RunContext,
    ) -> tuple[LocalValidationEvaluation, LocalValidationEvaluation, dict[str, object]]:
        recipe = ctx.workspaces.root.path / "validation" / "recipes.json"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "focused",
                            "command": "python -c 'print(\"ok\")'",
                            "input_paths": ["queue.py"],
                            "timeout_seconds": 30,
                            "purpose": "exercise the candidate queue",
                        }
                    ],
                }
            )
        )
        first = await ctx.evaluation.validate_local(
            ctx.workspaces.root,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-1.json",
        )
        second = await ctx.evaluation.validate_local(
            ctx.workspaces.root,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report-2.json",
        )
        payload = json.loads(
            (ctx.workspaces.root.path / "validation" / "report-2.json").read_text()
        )
        return first, second, payload

    first, second, payload = _run(tmp_path, executor, body)

    assert first == LocalValidationEvaluation(
        passed=True,
        report_location="validation/report-1.json",
    )
    assert second.passed
    results = payload["results"]
    assert isinstance(results, list)
    result = results[0]
    assert isinstance(result, dict)
    assert result["reused"] is True


def test_local_validation_reverts_candidate_mutation_and_reports_failure(tmp_path: Path) -> None:
    executor = FakeGateExecutor()

    async def body(ctx: RunContext) -> tuple[LocalValidationEvaluation, str]:
        recipe = ctx.workspaces.root.path / "validation" / "recipes.json"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(
            json.dumps(
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "mutating",
                            "command": (
                                "python -c 'from pathlib import Path; "
                                'Path("queue.py").write_text("VALUE = 99\\n")\''
                            ),
                            "input_paths": ["queue.py"],
                            "timeout_seconds": 30,
                            "purpose": "must not mutate the candidate",
                        }
                    ],
                }
            )
        )
        result = await ctx.evaluation.validate_local(
            ctx.workspaces.root,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )
        return result, (ctx.workspaces.root.path / "queue.py").read_text()

    result, candidate = _run(tmp_path, executor, body)

    assert not result.passed
    assert result.feedback is not None
    assert "mutated the workspace" in result.feedback
    assert candidate == "VALUE = 1\n"
