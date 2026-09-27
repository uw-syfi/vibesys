"""Public semantic evaluation behavior over the production runtime."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.plugin import capability_plugin
from vibesys.run.host import open_product_run_host

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.events import CoreEventType, EventStatus, GateFinishedData, GateStartedData
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    BenchmarkEvaluation,
    BenchmarkObjective,
    LocalValidationEvaluation,
    MetricDirection,
    Run,
    RuntimeContractError,
)
from vs_runtime.api.testing import FakeWorkspace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from typing import TypeVar

    _Result = TypeVar("_Result")


_PLUGIN = capability_plugin("evaluation")


def _write_project(root: Path, *, accuracy_command: str = "true") -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "benchmark.py").write_text(
        "import json, sys\n"
        "with open(sys.argv[2], 'w') as output:\n"
        "    json.dump({'throughput': 42.0}, output)\n"
    )
    (root / "validation").mkdir()
    (root / "validation" / "recipes.json").write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": "queue-contract",
                        "command": "python -m py_compile queue.py",
                        "input_paths": ["queue.py"],
                        "timeout_seconds": 30,
                        "purpose": "check the edited queue module",
                    }
                ],
            }
        )
    )
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        f'[accuracy]\ncommand = ["{accuracy_command}"]\n'
        '[benchmark]\ncommand = ["python", "benchmark.py"]\n'
        '[benchmark.result]\njson_argument = "--output-json"\nmetric = "throughput"\n'
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
    body: Callable[[Run], Awaitable[_Result]],
    *,
    agent_backend: str | None = None,
    accuracy_command: str = "true",
) -> tuple[_Result, LocalRunIntegration]:
    project_root = tmp_path / "project"
    _write_project(project_root, accuracy_command=accuracy_command)
    integration = LocalRunIntegration()

    async def exercise() -> _Result:
        async with open_product_run_host(
            _request(project_root, agent_backend=agent_backend),
            integration,
            plugin=_PLUGIN,
        ) as ctx:
            return await body(ctx)

    return asyncio.run(exercise()), integration


def test_adapter_returns_semantic_results_and_events(tmp_path: Path) -> None:
    async def body(ctx: Run) -> tuple[AccuracyEvaluation, BenchmarkEvaluation]:
        accuracy = await ctx.evaluation.accuracy(ctx.workspaces.root)
        benchmark = await ctx.evaluation.benchmark(
            ctx.workspaces.root,
            objectives=(BenchmarkObjective(name="throughput", direction=MetricDirection.MAXIMIZE),),
        )
        return accuracy, benchmark

    (accuracy, benchmark), integration = _run(tmp_path, body)
    try:
        assert accuracy.passed
        assert accuracy.receipt is not None
        assert benchmark.passed
        assert benchmark.metric_name == "throughput"
        assert benchmark.metric_value == 42.0
        assert benchmark.metric_direction is MetricDirection.MAXIMIZE
        gate_events = [
            event
            for event in integration.events.read()
            if event.type in {CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED}
        ]
        assert [type(event.data) for event in gate_events] == [
            GateStartedData,
            GateFinishedData,
            GateStartedData,
            GateFinishedData,
        ]
    finally:
        integration.close()


def test_accuracy_receipt_round_trips_across_framework_snapshot(tmp_path: Path) -> None:
    async def body(ctx: Run) -> tuple[AccuracyEvaluation, AccuracyEvaluation]:
        first = await ctx.evaluation.accuracy(ctx.workspaces.root)
        assert first.receipt is not None
        restored = AccuracyReceipt.model_validate_json(first.receipt.model_dump_json())
        note = ctx.workspaces.root.path / ".vibesys" / "receipt-test.json"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("{}\n")
        await ctx.workspaces.root.snapshot("incidental framework metadata")
        reused = await ctx.evaluation.accuracy(ctx.workspaces.root, reuse=restored)
        return first, reused

    (first, reused), integration = _run(tmp_path, body)
    try:
        assert first.executed
        assert not reused.executed
        assert reused.receipt == first.receipt
    finally:
        integration.close()


def test_adapter_preserves_failure_feedback(tmp_path: Path) -> None:
    async def body(ctx: Run) -> AccuracyEvaluation:
        return await ctx.evaluation.accuracy(ctx.workspaces.root)

    result, integration = _run(tmp_path, body, accuracy_command="false")
    try:
        assert not result.passed
        assert result.feedback is not None
        assert result.feedback.startswith("Framework accuracy gate failed.")
    finally:
        integration.close()


def test_local_validation_maps_runtime_result_and_gate_events(tmp_path: Path) -> None:
    async def body(ctx: Run) -> LocalValidationEvaluation:
        return await ctx.evaluation.validate_local(
            ctx.workspaces.root,
            recipe_artifact="validation/recipes.json",
            report_location="validation/report.json",
        )

    result, integration = _run(tmp_path, body)
    try:
        assert result.passed
        assert result.report_location == "validation/report.json"
        events = [
            event
            for event in integration.events.read()
            if event.type in {CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED}
        ]
        assert [type(event.data) for event in events] == [GateStartedData, GateFinishedData]
        started = events[0].data
        finished = events[1].data
        assert isinstance(started, GateStartedData)
        assert started.recipe == "queue-contract"
        assert isinstance(finished, GateFinishedData)
        assert finished.recipe == "queue-contract"
        assert events[1].status is EventStatus.COMPLETED
    finally:
        integration.close()


def test_stub_backend_skips_trusted_execution(tmp_path: Path) -> None:
    async def body(ctx: Run) -> tuple[AccuracyEvaluation, BenchmarkEvaluation]:
        return (
            await ctx.evaluation.accuracy(ctx.workspaces.root),
            await ctx.evaluation.benchmark(ctx.workspaces.root),
        )

    (accuracy, benchmark), integration = _run(tmp_path, body, agent_backend="stub")
    try:
        assert not accuracy.executed
        assert not benchmark.executed
    finally:
        integration.close()


def test_adapter_rejects_duplicate_objectives_before_execution(tmp_path: Path) -> None:
    async def body(ctx: Run) -> None:
        objective = BenchmarkObjective(name="latency", direction=MetricDirection.MINIMIZE)
        with pytest.raises(ValueError, match="objective names must be unique"):
            await ctx.evaluation.benchmark(
                ctx.workspaces.root,
                objectives=(objective, objective),
            )

    _, integration = _run(tmp_path, body)
    integration.close()


def test_adapter_rejects_foreign_receipt_and_workspace(tmp_path: Path) -> None:
    async def body(ctx: Run) -> None:
        with pytest.raises(TypeError, match="live handle"):
            await ctx.evaluation.accuracy(FakeWorkspace(path=ctx.workspaces.root.path))
        with pytest.raises(RuntimeContractError, match="another run"):
            await ctx.evaluation.accuracy(
                ctx.workspaces.root,
                reuse=AccuracyReceipt(
                    run_id="foreign",
                    workspace_id=None,
                    revision="revision",
                ),
            )

    _, integration = _run(tmp_path, body)
    integration.close()
