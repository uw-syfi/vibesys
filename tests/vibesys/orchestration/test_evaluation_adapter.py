"""Public semantic evaluation behavior over the production runtime."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.events import (
    CoreEventType,
    EventStatus,
    GateFinishedData,
    GateKind,
    GateStartedData,
)
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.host import open_product_run_host
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
from vs_sandbox.api import SandboxExecutionResult, SandboxKind
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from typing import TypeVar

    _Result = TypeVar("_Result")


_PLUGIN = capability_plugin("evaluation")


class _BlockingEvaluationSandbox(FakeSandbox):
    """Hold trusted execution until its lifecycle event is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, command: str, *, timeout: int | None = None) -> SandboxExecutionResult:
        if not self.started.is_set():
            self.started.set()
            self.release.wait()
        return super().execute(command, timeout=timeout)


def _write_project(
    root: Path,
    *,
    accuracy_command: str = "true",
    benchmark_command: tuple[str, ...] = ("python", "benchmark.py"),
    protocol_row: dict[str, float] | None = None,
) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    if protocol_row is None:
        benchmark_body = "    json.dump({'throughput': 42.0}, output)\n"
        result_contract = (
            '[benchmark.result]\njson_argument = "--output-json"\nmetric = "throughput"\n'
        )
    else:
        declarations = {
            name: {"direction": "min" if name == "latency" else "max"} for name in protocol_row
        }
        records = "\n".join(
            (
                json.dumps({"kind": "hello", "protocol": 2, "metrics": declarations}),
                json.dumps({"kind": "result", "values": protocol_row}),
            )
        )
        benchmark_body = f"    output.write({records!r})\n"
        result_contract = "result_protocol = 2\n"
    (root / "benchmark.py").write_text(
        f"import json, sys\nwith open(sys.argv[2], 'w') as output:\n{benchmark_body}"
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
        f"[benchmark]\ncommand = {json.dumps(benchmark_command)}\n"
        f"{result_contract}"
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
    return _run_project(project_root, body, agent_backend=agent_backend)


def _run_project(
    project_root: Path,
    body: Callable[[Run], Awaitable[_Result]],
    *,
    agent_backend: str | None = None,
) -> tuple[_Result, LocalRunIntegration]:
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


def test_single_metric_without_objectives_uses_scalar_contract_direction(tmp_path: Path) -> None:
    async def body(ctx: Run) -> BenchmarkEvaluation:
        return await ctx.evaluation.benchmark(ctx.workspaces.root)

    result, integration = _run(tmp_path, body)
    try:
        assert result.passed
        assert result.metric_name == "throughput"
        assert result.metric_value == 42.0
        assert result.metric_direction is MetricDirection.MAXIMIZE
    finally:
        integration.close()


def test_multiple_metrics_without_objectives_have_no_implicit_headline(tmp_path: Path) -> None:
    async def body(ctx: Run) -> BenchmarkEvaluation:
        return await ctx.evaluation.benchmark(ctx.workspaces.root)

    row = {"latency": 8.0, "throughput": 42.0}
    project_root = tmp_path / "project"
    _write_project(project_root, protocol_row=row)
    result, integration = _run_project(project_root, body)
    try:
        assert not result.passed
        assert result.row == row
        assert result.metric_name is None
        assert result.feedback is not None
        assert "no headline metric is defined" in result.feedback
        assert "objectives.toml" in result.feedback
    finally:
        integration.close()


def test_executed_benchmark_failure_has_policy_feedback(tmp_path: Path) -> None:
    async def body(ctx: Run) -> BenchmarkEvaluation:
        return await ctx.evaluation.benchmark(ctx.workspaces.root)

    project_root = tmp_path / "project"
    _write_project(project_root, benchmark_command=("false",))
    result, integration = _run_project(project_root, body)
    try:
        assert result.executed
        assert not result.passed
        assert result.feedback is not None
        assert result.feedback.startswith("Framework benchmark failed.")
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


def test_adapter_pairs_foreign_receipt_exception_with_terminal_gate(tmp_path: Path) -> None:
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
    try:
        gate_events = [
            event
            for event in integration.events.read()
            if event.type in {CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED}
        ]
        assert [type(event.data) for event in gate_events] == [
            GateStartedData,
            GateFinishedData,
        ]
        assert gate_events[0].status is EventStatus.ACTIVE
        assert gate_events[1].status is EventStatus.FAILED
        finished = gate_events[1].data
        assert isinstance(finished, GateFinishedData)
        assert finished.output_tail is not None
        assert "RuntimeContractError" in finished.output_tail
        assert "another run" in finished.output_tail
    finally:
        integration.close()


@pytest.mark.parametrize("gate", [GateKind.ACCURACY, GateKind.BENCHMARK])
def test_gate_starts_before_execution_and_finishes_on_cancellation(
    tmp_path: Path,
    gate: GateKind,
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    sandbox = _BlockingEvaluationSandbox()
    backend = FakeComputeBackend()
    backend.script_sandbox(SandboxKind.LOCAL, str(project_root), sandbox)

    async def exercise() -> None:
        async with open_product_run_host(
            _request(project_root),
            integration,
            plugin=_PLUGIN,
            backend_factory=lambda *_args, **_kwargs: backend,
        ) as ctx:
            operation = (
                ctx.evaluation.accuracy(ctx.workspaces.root)
                if gate is GateKind.ACCURACY
                else ctx.evaluation.benchmark(ctx.workspaces.root)
            )
            evaluation = asyncio.create_task(operation)
            await asyncio.to_thread(sandbox.started.wait)
            try:
                gate_events = [
                    event
                    for event in integration.events.read()
                    if event.type in {CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED}
                ]
                assert [type(event.data) for event in gate_events] == [GateStartedData]
                started = gate_events[0].data
                assert isinstance(started, GateStartedData)
                assert started.gate is gate
                assert evaluation.cancel()
            finally:
                sandbox.release.set()
            with pytest.raises(asyncio.CancelledError):
                await evaluation

    try:
        asyncio.run(exercise())
        gate_events = [
            event
            for event in integration.events.read()
            if event.type in {CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED}
        ]
        assert [type(event.data) for event in gate_events] == [
            GateStartedData,
            GateFinishedData,
        ]
        assert gate_events[0].status is EventStatus.ACTIVE
        assert gate_events[1].status is EventStatus.FAILED
        finished = gate_events[1].data
        assert isinstance(finished, GateFinishedData)
        assert finished.gate is gate
        assert finished.output_tail == "evaluation cancelled"
    finally:
        sandbox.release.set()
        integration.close()
