"""``ctx.gates``: trusted accuracy/benchmark checks in the run workspace.

Split from ``runtime.py`` by capability; see that module's docstring.
"""

# Capabilities in this module share one private owner for resource lifetime.
# lint-waiver: LW-040101 [SLF001]; capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.evaluators.gates import (
    GATE_RECORD_TAIL_CHARS,
    AccuracyGateResult,
    BenchmarkContract,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    emit_gate_finished,
    emit_gate_started,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.evaluators.metrics import MetricSpace
from vibesys.events import GateFinishedData, GateKind
from vibesys.orchestration import progress_log

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from framework.api import Sandbox
    from vibesys.context import _RunResources
    from vibesys.evaluators.metrics import Objective
    from vibesys.orchestration._host import HostResources
    from vibesys.orchestration.workspaces import WorkspaceHandle
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.git_tracker import GitTracker
    from vibesys.runtime import WorkspaceScope
    from vibesys.sandbox.run_environment import RunEnvironmentView


class GateExecutor(Protocol):
    """The two trusted gate operations ``ctx.gates`` runs off-thread.

    Injected on :class:`~vibesys.orchestration.runtime.RunContext` (default:
    the real trusted-command gates, ``_RealGateExecutor`` below) the same way
    ``agent_client_factory``/``backend_factory`` are: a test passes a fake in
    place of monkeypatching the module-level ``run_accuracy_gate``/
    ``run_benchmark_gate`` functions this protocol's real implementation
    wraps.
    """

    def run_accuracy(
        self,
        ctx: _GateInputs,
        *,
        process_id: str,
        timeout_seconds: int | None = None,
        execution_command: str | None = None,
        round_label: str | None = None,
    ) -> AccuracyGateResult:
        """Run the trusted accuracy command for one candidate."""
        ...

    def run_benchmark(  # noqa: PLR0913  # LW-040102 [PLR0913]; mirrors run_benchmark_gate's own field count.
        self,
        ctx: _GateInputs,
        *,
        contract: BenchmarkContract,
        space: MetricSpace,
        process_id: str,
        output_slug: str,
        execution_base: str | None = None,
        round_label: str | None = None,
    ) -> BenchmarkGateResult:
        """Run the trusted benchmark result contract for one candidate."""
        ...


class _RealGateExecutor:
    """Default `GateExecutor`: the real trusted accuracy/benchmark commands."""

    def run_accuracy(
        self,
        ctx: _GateInputs,
        *,
        process_id: str,
        timeout_seconds: int | None = None,
        execution_command: str | None = None,
        round_label: str | None = None,
    ) -> AccuracyGateResult:
        """Delegate to the module-level trusted accuracy gate."""
        return run_accuracy_gate(
            ctx,
            process_id=process_id,
            timeout_seconds=timeout_seconds,
            execution_command=execution_command,
            round_label=round_label,
        )

    def run_benchmark(  # noqa: PLR0913  # LW-040103 [PLR0913]; mirrors run_benchmark_gate's own field count.
        self,
        ctx: _GateInputs,
        *,
        contract: BenchmarkContract,
        space: MetricSpace,
        process_id: str,
        output_slug: str,
        execution_base: str | None = None,
        round_label: str | None = None,
    ) -> BenchmarkGateResult:
        """Delegate to the module-level trusted benchmark gate."""
        return run_benchmark_gate(
            ctx,
            contract=contract,
            space=space,
            process_id=process_id,
            output_slug=output_slug,
            execution_base=execution_base,
            round_label=round_label,
        )


_REAL_GATE_EXECUTOR = _RealGateExecutor()


@dataclass(frozen=True, slots=True)
class MeasurementOptions:
    """Policy-selected metric axes, event label, and command override."""

    objectives: Sequence[Objective] = ()
    label: str | None = None
    execution_base: str | None = None


_DEFAULT_MEASUREMENT_OPTIONS = MeasurementOptions()


@dataclass(frozen=True)
class _GateInputs:
    """Bind trusted gate inputs from one workspace's owned resources."""

    events: EventJournal
    judge_backend: Sandbox
    judge_accuracy_command: str | None
    judge_benchmark_command: str | None
    run_environment_view: RunEnvironmentView
    git: GitTracker

    @classmethod
    def from_resources(cls, context: _RunResources) -> _GateInputs:
        """Read the selected environment's immutable command paths."""
        return cls(
            events=context.events,
            judge_backend=context.run_environment_session.sandbox,
            judge_accuracy_command=context.run_environment_view.paths.accuracy_command,
            judge_benchmark_command=context.run_environment_view.paths.benchmark_command,
            run_environment_view=context.run_environment_view,
            git=context.git,
        )

    def trusted_input_changes(self) -> list[str]:
        """Detect edits to evaluator-owned project inputs."""
        return self.git.trusted_input_changes()


@dataclass(frozen=True, slots=True)
class GateRunResult:
    """Combined accuracy+benchmark outcome from one `_Evaluator.run` call.

    `feedback` is the first gate's rejection message, or `None` if both
    passed (or gates were skipped, e.g. a stub backend). `accuracy_passed`
    tells the caller whether it may reuse this outcome for a later retry of
    the exact same candidate commit (see `reuse_accuracy`).
    """

    feedback: str | None
    benchmark: FrameworkBenchmarkOutcome
    accuracy_passed: bool


class _Evaluator:
    """Trusted checks and measurements in the parent run workspace."""

    def __init__(self, host: HostResources) -> None:
        self._host = host
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, scope: WorkspaceScope | WorkspaceHandle | None) -> asyncio.Lock:
        """Serialize gates with the workspace they inspect.

        The parent tree (``scope is None``, or an isolation-free
        ``WorkspaceHandle``) shares ``_parent_mutation_lock`` with
        ``adopt``/``checkpoint`` so a gate on the parent tree and an
        adopt/checkpoint can never interleave (R6). Isolated scopes keep
        their own lock, independent of the parent lock and of each other.
        """
        key = scope.id if scope is not None else None
        if key is None:
            return self._host._parent_mutation_lock
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _forget(self, scope: WorkspaceScope) -> None:
        """Release a discarded scope's synchronization state."""
        self._locks.pop(scope.id, None)

    async def check(
        self,
        process_id: str,
        *,
        label: str | None = None,
        execution_command: str | None = None,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
    ) -> AccuracyGateResult:
        """Run the bundle's trusted accuracy command and reject input tampering."""
        async with self._lock_for(scope):
            context = _GateInputs.from_resources(self._host.workspaces._resources_for(scope))
            timeout = framework_command_timeout(
                context, self._host.request.input_bundle.manifest.accuracy.timeout_seconds
            )
            executor = self._host._gate_executor or _REAL_GATE_EXECUTOR
            return await self._host._run_blocking(
                executor.run_accuracy,
                context,
                process_id=process_id,
                timeout_seconds=timeout,
                execution_command=execution_command,
                round_label=label,
            )

    async def reuse_accuracy(self, *, label: str | None = None) -> AccuracyGateResult:
        """Publish a paired accuracy PASS reused for an exact candidate revision."""
        return await self._host._run_blocking(self._reuse_accuracy, label)

    def _reuse_accuracy(self, label: str | None) -> AccuracyGateResult:
        command = self._host.environment.view.paths.accuracy_command
        emit_gate_started(GateKind.ACCURACY, command=command, round_label=label)
        emit_gate_finished(
            GateFinishedData(gate=GateKind.ACCURACY, reused=True),
            passed=True,
            round_label=label,
        )
        return AccuracyGateResult(
            command=command,
            passed=True,
            output="Reused the prior framework-owned PASS for this exact candidate commit",
            feedback=None,
            executed=False,
        )

    async def measure(
        self,
        output_slug: str,
        *,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
        options: MeasurementOptions = _DEFAULT_MEASUREMENT_OPTIONS,
    ) -> BenchmarkGateResult:
        """Run and parse the bundle's declared trusted benchmark contract."""
        async with self._lock_for(scope):
            context = _GateInputs.from_resources(self._host.workspaces._resources_for(scope))
            bundle = self._host.request.input_bundle
            timeout = framework_command_timeout(context, bundle.manifest.benchmark.timeout_seconds)
            executor = self._host._gate_executor or _REAL_GATE_EXECUTOR
            return await self._host._run_blocking(
                executor.run_benchmark,
                context,
                contract=BenchmarkContract(
                    result_spec=bundle.benchmark_result,
                    result_protocol=bundle.benchmark_result_protocol,
                    timeout_seconds=timeout,
                ),
                space=MetricSpace(objectives=tuple(options.objectives)),
                process_id=output_slug,
                output_slug=output_slug,
                execution_base=options.execution_base,
                round_label=options.label,
            )

    @staticmethod
    def _write_accuracy_gate(  # noqa: PLR0913  # LW-040104 [PLR0913]; mirrors render_framework_accuracy_gate's own field count.
        progress_path: Path | None,
        round_number: int,
        retry: int,
        *,
        command: str,
        passed: bool,
        output: str,
    ) -> None:
        """Write one accuracy-gate outcome, if this run declared a progress path.

        ``ctx.gates.run`` writes gate entries itself (synchronously, ahead of
        the snapshot it already takes right after) rather than handing a
        recorder back to the caller: a strategy declares its progress path
        via ``ctx.progress.declare`` and never renders or writes anything
        for gates itself.
        """
        if progress_path is None:
            return
        progress_log.write(
            progress_path,
            progress_log.render_framework_accuracy_gate(
                round_number, retry, command=command, passed=passed, output=output
            ),
        )

    async def run(  # noqa: PLR0913  # LW-040105 [PLR0913]; one call replaces four strategies' hand-rolled sequencing.
        self,
        *,
        round_number: int,
        retry: int,
        commit: str | None,
        objectives: Sequence[Objective],
        reuse_accuracy: bool = False,
        agent_backend_name: str | None = None,
    ) -> GateRunResult:
        """Run the accuracy then benchmark gate, recording each outcome once.

        This is the one place the "official gates are due" mechanics live
        (reuse-accuracy, command composition with the candidate revision and
        release env var, accuracy-then-benchmark sequencing, the stub-backend
        skip). *When* to call it (retry/review policy) stays with the
        strategy. A stub `agent_backend_name` (tests, fast local runs) skips
        both gates and reports a pass with no feedback and an empty outcome,
        matching every strategy's prior hand-rolled check.

        This is often the next write to `ctx.progress`'s declared path after
        blocks became available (see `vibesys.orchestration.progress`), so
        this flushes that pending buffer first, in order, before this gate's
        own outcome -- keeping the file's section order the same as when
        every write happened synchronously.
        """
        progress_path = self._host.progress.path
        pending = self._host.progress.drain()
        if progress_path is not None:
            for block in pending:
                progress_log.write(progress_path, block)
        if agent_backend_name == "stub":
            return GateRunResult(
                feedback=None, benchmark=FrameworkBenchmarkOutcome(), accuracy_passed=False
            )
        resource_feedback = await self._host.environment.reconcile_model_requests()
        if resource_feedback is not None:
            return GateRunResult(
                feedback=resource_feedback,
                benchmark=FrameworkBenchmarkOutcome(),
                accuracy_passed=False,
            )
        accuracy = await self._run_accuracy_gate(
            round_number, retry, commit, reuse=reuse_accuracy, progress_path=progress_path
        )
        if accuracy.feedback is not None:
            return GateRunResult(
                feedback=accuracy.feedback,
                benchmark=FrameworkBenchmarkOutcome(),
                accuracy_passed=False,
            )
        benchmark = await self._run_benchmark_gate(
            round_number, retry, commit, objectives, progress_path=progress_path
        )
        return GateRunResult(feedback=benchmark.feedback, benchmark=benchmark, accuracy_passed=True)

    async def _run_accuracy_gate(
        self,
        round_number: int,
        retry: int,
        commit: str | None,
        *,
        reuse: bool,
        progress_path: Path | None,
    ) -> AccuracyGateResult:
        view = self._host.environment.view
        command = view.paths.accuracy_command
        if reuse:
            self._write_accuracy_gate(
                progress_path,
                round_number,
                retry,
                command=command or "(not configured)",
                passed=True,
                output=(
                    "Reused the prior framework-owned PASS for this exact candidate commit; "
                    "a later gate, not accuracy, caused the retry."
                ),
            )
            return await self.reuse_accuracy(label=f"round-{round_number}")
        bundle = self._host.request.input_bundle
        release = (
            bundle.benchmark_result is None and bundle.benchmark_result_protocol is None
        ) or not view.paths.benchmark_command
        execution = self._command(
            command, commit, view.deployment_release_env_var if release else None
        )
        result = await self.check(
            f"accuracy-{round_number}-{retry}",
            label=f"round-{round_number}",
            execution_command=execution,
        )
        if result.passed and not result.executed:
            return result
        self._write_accuracy_gate(
            progress_path,
            round_number,
            retry,
            command=result.command or "(not configured)",
            passed=result.passed,
            output=result.output[-GATE_RECORD_TAIL_CHARS:],
        )
        await self._host.workspaces.root.snapshot(
            f"round-{round_number}-retry-{retry}-framework-accuracy"
        )
        return result

    async def _run_benchmark_gate(
        self,
        round_number: int,
        retry: int,
        commit: str | None,
        objectives: Sequence[Objective],
        *,
        progress_path: Path | None,
    ) -> FrameworkBenchmarkOutcome:
        view = self._host.environment.view
        execution = self._command(
            view.paths.benchmark_command, commit, view.deployment_release_env_var
        )
        result = await self.measure(
            f"{round_number}-{retry}",
            options=MeasurementOptions(
                objectives=tuple(objectives),
                label=f"round-{round_number}",
                execution_base=execution,
            ),
        )
        if not result.executed:
            return result.outcome
        spec = self._host.request.input_bundle.benchmark_result
        if progress_path is not None:
            progress_log.write(
                progress_path,
                progress_log.render_framework_benchmark(
                    round_number,
                    retry,
                    command=result.command or "(not configured)",
                    passed=result.passed,
                    metric_name=result.outcome.metric_name or (spec.metric if spec else None),
                    metric_value=result.outcome.metric_value,
                    output=result.output[-GATE_RECORD_TAIL_CHARS:],
                ),
            )
        await self._host.workspaces.root.snapshot(
            f"round-{round_number}-retry-{retry}-framework-benchmark"
        )
        return result.outcome

    @staticmethod
    def _command(command: str | None, revision: str | None, release_env: str | None) -> str | None:
        """Compose a trusted gate command with the candidate revision/release env."""
        if command is None:
            return None
        variables = []
        if revision:
            variables.append(f"VIBESYS_CANDIDATE_REVISION={shlex.quote(revision)}")
        if release_env:
            variables.append(f"{release_env}=1")
        return f"env {' '.join(variables)} {command}" if variables else command
