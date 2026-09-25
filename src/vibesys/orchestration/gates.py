"""``ctx.gates``/``ctx.evaluator``: trusted accuracy/benchmark checks in the run workspace.

Split from ``runtime.py`` by capability; see that module's docstring.
"""

# Capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from vibesys.evaluators.gates import (
    GATE_RECORD_TAIL_CHARS,
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    emit_gate_finished,
    emit_gate_started,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.events import GateKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.context import _RunResources
    from vibesys.evaluators.metrics import Objective
    from vibesys.orchestration.workspaces import WorkspaceHandle
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.git_tracker import GitTracker
    from vibesys.runtime import WorkspaceScope
    from vibesys.sandbox.run_environment import RunEnvironmentView
    from vs_sandbox.api import Sandbox


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


class GateRecorder(Protocol):
    """Progress-board sink a strategy declares once for `_Evaluator.run`.

    Each method receives the same typed gate result `run` computed, so a
    strategy's board rendering never re-derives verdict/output/metric facts
    from anything but that one result.
    """

    def accuracy(
        self, round_number: int, retry: int, *, command: str, passed: bool, output: str
    ) -> None:
        """Record one accuracy-gate outcome."""
        ...

    def benchmark(  # noqa: PLR0913  # mirrors the typed gate result's own field count
        self,
        round_number: int,
        retry: int,
        *,
        command: str,
        passed: bool,
        metric_name: str | None,
        metric_value: float | None,
        output: str,
    ) -> None:
        """Record one benchmark-gate outcome."""
        ...


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

    def __init__(self, host: Any) -> None:  # noqa: ANN401
        # `host` is a `vibesys.orchestration.runtime.RunContext`, typed `Any`
        # here (rather than imported under `TYPE_CHECKING`) because that
        # module imports this one for real to construct `_Evaluator`; a
        # back-reference, even type-checking-only, would be a tach module
        # cycle (tach freezes `TYPE_CHECKING` imports too:
        # `ignore_type_checking_imports = false`).
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
            return await self._host._run_blocking(
                run_accuracy_gate,
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
        emit_gate_finished(GateKind.ACCURACY, passed=True, reused=True, round_label=label)
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
            return await self._host._run_blocking(
                run_benchmark_gate,
                context,
                result_spec=bundle.benchmark_result,
                result_protocol=bundle.benchmark_result_protocol,
                objectives=options.objectives,
                process_id=output_slug,
                output_slug=output_slug,
                timeout_seconds=timeout,
                execution_base=options.execution_base,
                round_label=options.label,
            )

    async def run(  # noqa: PLR0913  # one call replaces four strategies' hand-rolled sequencing
        self,
        *,
        round_number: int,
        retry: int,
        commit: str | None,
        objectives: Sequence[Objective],
        record: GateRecorder,
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
        """
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
            round_number, retry, commit, reuse=reuse_accuracy, record=record
        )
        if accuracy.feedback is not None:
            return GateRunResult(
                feedback=accuracy.feedback,
                benchmark=FrameworkBenchmarkOutcome(),
                accuracy_passed=False,
            )
        benchmark = await self._run_benchmark_gate(
            round_number, retry, commit, objectives, record=record
        )
        return GateRunResult(feedback=benchmark.feedback, benchmark=benchmark, accuracy_passed=True)

    async def _run_accuracy_gate(
        self,
        round_number: int,
        retry: int,
        commit: str | None,
        *,
        reuse: bool,
        record: GateRecorder,
    ) -> AccuracyGateResult:
        view = self._host.environment.view
        command = view.paths.accuracy_command
        if reuse:
            record.accuracy(
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
        record.accuracy(
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
        record: GateRecorder,
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
        record.benchmark(
            round_number,
            retry,
            command=result.command or "(not configured)",
            passed=result.passed,
            metric_name=result.outcome.metric_name or (spec.metric if spec else None),
            metric_value=result.outcome.metric_value,
            output=result.output[-GATE_RECORD_TAIL_CHARS:],
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
