"""Public semantic evaluation behavior over the production runtime."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from tests.support.core_host_world import open_core_host
from tests.support.docker_environment import (
    fake_docker_environment,
    host_container_backend,
)
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.support.task_project import write_task

from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.events import (
    CoreEvent,
    CoreEventType,
    EventStatus,
    GateFinishedData,
    GateKind,
    GateStartedData,
)
from vibesys.inputs import load_task
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.run.contracts import RunRequest
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    BenchmarkObjective,
    MetricDirection,
    RuntimeContractError,
    Workspace,
)
from vs_runtime.api.testing import FakeWorkspace, idle_environment_system
from vs_sandbox.api import CommandResult, SandboxKind
from vs_sandbox.api.testing import FakeComputeBackend, FakeLifecycleRunner
from vs_sim.api import OsThreads
from vs_sim.api.testing import (
    ManualClock,
    SimThreads,
    wait_or_fail,
    wait_until_started,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from typing import TypeVar

    from vibesys.run.host import CoreHost
    from vs_sim.api import Event

    _Result = TypeVar("_Result")


# The held command blocks a worker thread of the sandbox runner, so its latches are the OS's.
_THREADS = OsThreads()


class _BlockingEvaluationSandbox(FakeLifecycleRunner):
    """Hold trusted execution until its lifecycle event is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.started = _THREADS.event()
        self.release = _THREADS.event()

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
    ) -> CommandResult:
        del cancel  # the test releases the held command itself
        if not self.started.is_set():
            self.started.set()
            wait_or_fail(self.release, "the held trusted command to be released")
        return super().execute(command, timeout=timeout)


def _write_project(
    root: Path,
    *,
    accuracy_command: str = "true",
    benchmark_command: tuple[str, ...] = ("python", "benchmark.py"),
    protocol_row: dict[str, float] | None = None,
) -> None:
    root.mkdir(parents=True)
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
    write_task(
        root,
        'version = 1\n[agent]\ndomain = "generic"\n'
        f'[accuracy]\ncommand = ["{accuracy_command}"]\n'
        f"[benchmark]\ncommand = {json.dumps(benchmark_command)}\n"
        f"{result_contract}",
        objective="Improve the queue.\n",
    )


@pytest.fixture(autouse=True)
def _container_cli_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agents run in a container, which needs credentials for the CLI it starts."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")


def _request(project_root: Path, *, agent_backend: str | None = None) -> RunRequest:
    return RunRequest(
        run_environment=fake_docker_environment(),
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id="evaluation", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "evaluation"}}),
        input_bundle=load_task(project_root),
        objective="Improve the queue.",
        exp_name="evaluation",
        agent_backend=agent_backend,
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def _run(
    tmp_path: Path,
    body: Callable[[CoreHost], Awaitable[_Result]],
    *,
    agent_backend: str | None = None,
    accuracy_command: str = "true",
) -> tuple[_Result, LocalRunIntegration]:
    project_root = tmp_path / "project"
    _write_project(project_root, accuracy_command=accuracy_command)
    return _run_project(project_root, body, agent_backend=agent_backend)


def _run_project(
    project_root: Path,
    body: Callable[[CoreHost], Awaitable[_Result]],
    *,
    agent_backend: str | None = None,
) -> tuple[_Result, LocalRunIntegration]:
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> _Result:
        async with open_core_host(
            _request(project_root, agent_backend=agent_backend),
            integration,
            backend_factory=host_container_backend,
        ) as ctx:
            return await body(ctx)

    return asyncio.run(exercise()), integration


def test_adapter_preserves_failure_feedback(tmp_path: Path) -> None:
    async def body(ctx: CoreHost) -> AccuracyEvaluation:
        return await ctx.evaluation.accuracy(ctx.workspaces.root)

    result, integration = _run(tmp_path, body, accuracy_command="false")
    try:
        assert not result.passed
        assert result.feedback is not None
        assert result.feedback.startswith("Framework accuracy gate failed.")
    finally:
        integration.close()


def test_multiple_metrics_without_objectives_have_no_implicit_headline(tmp_path: Path) -> None:
    async def body(ctx: CoreHost) -> BenchmarkEvaluation:
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
    async def body(ctx: CoreHost) -> BenchmarkEvaluation:
        return await ctx.evaluation.benchmark(ctx.workspaces.root)

    project_root = tmp_path / "project"
    _write_project(project_root, benchmark_command=("false",))
    result, integration = _run_project(project_root, body)
    try:
        assert result.executed
        assert not result.passed
        assert result.feedback is not None
        assert result.feedback.startswith("Framework benchmark failed.")
        assert result.failure_kind is BenchmarkFailureKind.AMBIGUOUS
    finally:
        integration.close()


_UNUSABLE_RECIPES = (
    "Accuracy checker: expects cached_tokens > 0 on a repeated prompt",
    "validation/missing.json",
    "validation/recipes.txt",
)


def _gate_events(integration: LocalRunIntegration) -> list[CoreEvent]:
    return [
        event
        for event in integration.events.read()
        if event.type in {CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED}
    ]


class _Gates:
    """The gate events a run has emitted since they were last taken."""

    def __init__(self, integration: LocalRunIntegration) -> None:
        self._integration = integration
        self._seen: list[CoreEvent] = []

    def new(self) -> list[CoreEvent]:
        events = _gate_events(self._integration)
        fresh, self._seen = events[len(self._seen) :], events
        return fresh


async def _accuracy_and_benchmark(
    ctx: CoreHost, root: Workspace, gates: _Gates
) -> AccuracyEvaluation:
    # Accuracy and a benchmark with a declared objective: results and a started/finished pair each.
    accuracy = await ctx.evaluation.accuracy(root)
    benchmark = await ctx.evaluation.benchmark(
        root,
        objectives=(BenchmarkObjective(name="throughput", direction=MetricDirection.MAXIMIZE),),
    )
    assert accuracy.passed
    assert accuracy.receipt is not None
    assert benchmark.passed
    assert (benchmark.metric_name, benchmark.metric_value) == ("throughput", 42.0)
    assert benchmark.metric_direction is MetricDirection.MAXIMIZE
    assert [type(event.data) for event in gates.new()] == [
        GateStartedData,
        GateFinishedData,
        GateStartedData,
        GateFinishedData,
    ]
    return accuracy


async def _receipt_reuse(
    ctx: CoreHost, root: Workspace, gates: _Gates, accuracy: AccuracyEvaluation
) -> None:
    # A receipt survives a JSON round trip and an incidental framework snapshot.
    assert accuracy.receipt is not None
    restored = AccuracyReceipt.model_validate_json(accuracy.receipt.model_dump_json())
    note = root.path / ".vibesys" / "receipt-test.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("{}\n")
    await root.snapshot("incidental framework metadata")
    reused = await ctx.evaluation.accuracy(root, reuse=restored)
    assert accuracy.executed
    assert not reused.executed
    assert reused.receipt == accuracy.receipt
    gates.new()


async def _scalar_benchmark(ctx: CoreHost, root: Workspace, gates: _Gates) -> None:
    # A single metric without objectives takes the scalar contract's direction.
    scalar = await ctx.evaluation.benchmark(root)
    assert scalar.passed
    assert (scalar.metric_name, scalar.metric_value) == ("throughput", 42.0)
    assert scalar.metric_direction is MetricDirection.MAXIMIZE
    gates.new()


async def _local_validation(ctx: CoreHost, root: Workspace, gates: _Gates) -> None:
    # Local validation maps the runtime result and reports one gate.
    validated = await ctx.evaluation.validate_local(
        root,
        recipe_artifact="validation/recipes.json",
        report_location="validation/report.json",
    )
    assert validated.passed
    assert validated.report_location == "validation/report.json"
    started, finished = gates.new()
    assert isinstance(started.data, GateStartedData)
    assert isinstance(finished.data, GateFinishedData)
    assert started.data.recipe == finished.data.recipe == "queue-contract"
    assert finished.status is EventStatus.COMPLETED


async def _unusable_recipes(ctx: CoreHost, root: Workspace, gates: _Gates) -> None:
    # A recipe reference that is no recipe is the agent's input error, never the framework's.
    for artifact in _UNUSABLE_RECIPES:
        unusable = await ctx.evaluation.validate_local(
            root, recipe_artifact=artifact, report_location="validation/report.json"
        )
        assert not unusable.passed
        assert unusable.recipe_unusable
        assert unusable.feedback is not None
        assert "input error in your reply" in unusable.feedback
        assert "Framework local validation" not in unusable.feedback
    gates.new()


async def _duplicate_objectives(ctx: CoreHost, root: Workspace, gates: _Gates) -> None:
    # Two objectives of one name are refused before anything runs.
    objective = BenchmarkObjective(name="latency", direction=MetricDirection.MINIMIZE)
    with pytest.raises(ValueError, match="objective names must be unique"):
        await ctx.evaluation.benchmark(root, objectives=(objective, objective))
    assert gates.new() == []


async def _refused_requests(ctx: CoreHost, root: Workspace, gates: _Gates) -> None:
    # A foreign handle is refused before a gate opens; a foreign receipt opens one and fails it.
    with pytest.raises(TypeError, match="live handle"):
        await ctx.evaluation.accuracy(FakeWorkspace(path=root.path))
    with pytest.raises(RuntimeContractError, match="another run"):
        await ctx.evaluation.accuracy(
            root,
            reuse=AccuracyReceipt(run_id="foreign", workspace_id=None, revision="revision"),
        )
    refused_start, refused_end = gates.new()
    assert isinstance(refused_start.data, GateStartedData)
    assert refused_start.status is EventStatus.ACTIVE
    assert refused_end.status is EventStatus.FAILED
    assert isinstance(refused_end.data, GateFinishedData)
    assert refused_end.data.output_tail is not None
    assert "RuntimeContractError" in refused_end.data.output_tail
    assert "another run" in refused_end.data.output_tail


def test_the_adapter_evaluates_a_task_and_reports_each_gate_as_events(tmp_path: Path) -> None:
    """One host, every operation of the adapter over a passing task, each checked as it ends.

    Budget: one product host over a real Git project (the adapter needs the whole run).
    """

    async def body(ctx: CoreHost, integration: LocalRunIntegration) -> None:
        root, gates = ctx.workspaces.root, _Gates(integration)
        accuracy = await _accuracy_and_benchmark(ctx, root, gates)
        await _receipt_reuse(ctx, root, gates, accuracy)
        await _scalar_benchmark(ctx, root, gates)
        await _local_validation(ctx, root, gates)
        await _unusable_recipes(ctx, root, gates)
        await _duplicate_objectives(ctx, root, gates)
        await _refused_requests(ctx, root, gates)

    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )

    async def exercise() -> None:
        project_root = tmp_path / "project"
        _write_project(project_root)
        async with open_core_host(
            _request(project_root), integration, backend_factory=host_container_backend
        ) as ctx:
            await body(ctx, integration)

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_stub_backend_skips_trusted_execution(tmp_path: Path) -> None:
    async def body(ctx: CoreHost) -> tuple[AccuracyEvaluation, BenchmarkEvaluation]:
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


def test_a_docker_gate_runs_in_a_throwaway_candidate_not_the_live_root(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    backend = FakeComputeBackend(threads=SimThreads())

    async def exercise() -> bool:
        async with open_core_host(
            _request(project_root),
            integration,
            backend_factory=lambda *_args, **_kwargs: backend,
        ) as ctx:
            seen = {c.host_workspace for c in backend.creations}
            await ctx.evaluation.accuracy(ctx.workspaces.root)
            return {c.host_workspace for c in backend.creations} > seen

    try:
        assert asyncio.run(exercise())
    finally:
        integration.close()


@pytest.mark.parametrize("gate", [GateKind.ACCURACY, GateKind.BENCHMARK])
def test_gate_starts_before_execution_and_finishes_on_cancellation(
    tmp_path: Path,
    gate: GateKind,
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    sandbox = _BlockingEvaluationSandbox()
    backend = FakeComputeBackend(threads=SimThreads())
    # Docker gates run in a throwaway candidate, so hold every Docker workspace.
    backend.script_sandbox_for_kind(SandboxKind.DOCKER, sandbox)

    async def exercise() -> None:
        async with open_core_host(
            _request(project_root),
            integration,
            backend_factory=lambda *_args, **_kwargs: backend,
        ) as ctx:
            operation = (
                ctx.evaluation.accuracy(ctx.workspaces.root)
                if gate is GateKind.ACCURACY
                else ctx.evaluation.benchmark(ctx.workspaces.root)
            )
            evaluation = asyncio.create_task(operation)
            await wait_until_started(sandbox.started, evaluation)
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
