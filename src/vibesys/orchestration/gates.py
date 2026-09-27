"""VibeSys evaluation policy over runtime-owned trusted execution."""

from __future__ import annotations

import shlex
from itertools import count
from typing import TYPE_CHECKING

from vibesys.events import (
    CoreEventType,
    CoreEventWriter,
    EventStatus,
    GateFinishedData,
    GateKind,
    GateStartedData,
    SubprocessOutputData,
)
from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    BenchmarkEvaluation,
    BenchmarkObjective,
    LocalValidationEvaluation,
    MetricDirection,
    RuntimeContractError,
    Workspace,
    WorkspaceAccess,
    validate_workspace_writable_paths,
)
from vs_runtime.api.infrastructure import (
    FrameworkValidationResult,
    LocalValidationRecipeError,
    LocalValidationRecipeErrorKind,
    ScalarBenchmarkContract,
    TrustedAccuracyResult,
    TrustedBenchmarkContract,
    TrustedBenchmarkResult,
    ValidationRecipe,
    run_local_validation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.orchestration.request import RunRequest
    from vs_runtime.api import Commands, Workspace
    from vs_runtime.api.infrastructure import WorkspaceRuntime


GATE_LOG_TAIL_CHARS = 1000
GATE_FEEDBACK_TAIL_CHARS = 4000


def emit_gate_started(
    events: CoreEventWriter,
    gate: GateKind,
    *,
    recipe: str | None = None,
    command: str | None = None,
    round_label: str | None = None,
) -> None:
    """Publish that one framework gate began evaluating a candidate."""
    events.emit(
        CoreEventType.GATE_STARTED,
        data=GateStartedData(gate=gate, recipe=recipe, command=command),
        status=EventStatus.ACTIVE,
        round_label=round_label,
    )


def emit_gate_finished(
    events: CoreEventWriter,
    data: GateFinishedData,
    *,
    passed: bool,
    round_label: str | None = None,
) -> None:
    """Publish one framework gate outcome."""
    events.emit(
        CoreEventType.GATE_FINISHED,
        data=data,
        status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
        round_label=round_label,
    )


class _LocalValidationEvents:
    """Translate policy-neutral recipe observations to VibeSys gate events."""

    def __init__(self, events: CoreEventWriter) -> None:
        self._events = events

    def started(self, recipe: ValidationRecipe) -> None:
        emit_gate_started(
            self._events,
            GateKind.VALIDATION,
            recipe=recipe.name,
            command=recipe.command,
        )

    def finished(self, result: FrameworkValidationResult) -> None:
        failure = None if result.passed else (result.error or result.output or "unknown failure")
        emit_gate_finished(
            self._events,
            GateFinishedData(
                gate=GateKind.VALIDATION,
                recipe=result.recipe.name,
                reused=result.reused,
                output_tail=None if failure is None else failure[-GATE_LOG_TAIL_CHARS:],
            ),
            passed=result.passed,
        )


async def _validate_local(
    commands: Commands,
    events: CoreEventWriter,
    workspace: Workspace,
    *,
    recipe_artifact: str,
    report_location: str,
) -> LocalValidationEvaluation:
    """Run the mechanism and map its detailed outcome to product feedback."""
    try:
        results = await run_local_validation(
            commands,
            workspace,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
            events=_LocalValidationEvents(events),
        )
    except LocalValidationRecipeError as error:
        if error.kind is LocalValidationRecipeErrorKind.DUPLICATE_NAMES:
            feedback = "Framework local validation recipes contain duplicate names."
        else:
            feedback = f"Framework local validation recipe error: {error}."
        return LocalValidationEvaluation(passed=False, feedback=feedback)

    failed = next((result for result in results if not result.passed), None)
    if failed is None:
        return LocalValidationEvaluation(passed=True, report_location=report_location)
    detail = failed.error or failed.output or "unknown failure"
    return LocalValidationEvaluation(
        passed=False,
        feedback=(
            f"Framework local validation failed for {failed.recipe.name!r}: {detail}. "
            f"Inspect `{report_location}` and repair only the affected local contract."
        ),
        report_location=report_location,
    )


class _EvaluationAdapter:
    """Apply VibeSys receipt, snapshot, metric, and event policy."""

    def __init__(
        self,
        run_id: str,
        request: RunRequest,
        runtime: WorkspaceRuntime,
        events: CoreEventWriter,
        log: Callable[[str], None],
    ) -> None:
        self._run_id = run_id
        self._request = request
        self._runtime_evaluation = runtime.evaluation
        self._events = events
        self._commands = runtime.commands
        self._log = log
        self._identifiers = count(1)

    def _live_workspace(self, workspace: Workspace) -> Workspace:
        self._runtime_evaluation.spec(workspace)
        return workspace

    def _receipt(self, workspace: Workspace, revision: str) -> AccuracyReceipt:
        return AccuracyReceipt(
            run_id=self._run_id,
            workspace_id=workspace.id,
            revision=revision,
        )

    def _validate_receipt_owner(self, workspace: Workspace, receipt: AccuracyReceipt) -> None:
        if receipt.run_id != self._run_id:
            message = "accuracy receipt belongs to another run"
            raise RuntimeContractError(message)
        if receipt.workspace_id != workspace.id:
            message = "accuracy receipt belongs to another workspace"
            raise RuntimeContractError(message)

    async def _validate_receipt_revision(
        self,
        workspace: Workspace,
        receipt: AccuracyReceipt,
    ) -> None:
        current_revision = workspace.revision
        if current_revision is None:
            message = "accuracy receipt does not match the current workspace revision"
            raise RuntimeContractError(message)
        if receipt.revision == current_revision:
            return
        same_candidate = await self._runtime_evaluation.revisions_equivalent(
            workspace,
            receipt.revision,
            current_revision,
        )
        if not same_candidate:
            message = "accuracy receipt does not match the current candidate revision"
            raise RuntimeContractError(message)

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Run or explicitly reuse trusted accuracy for one candidate."""
        live = self._live_workspace(workspace)
        spec = self._runtime_evaluation.spec(live)
        if reuse is not None:
            self._validate_receipt_owner(live, reuse)
            await live.snapshot("framework-accuracy-reuse-input")
            await self._validate_receipt_revision(live, reuse)
            command = spec.accuracy_command
            emit_gate_started(self._events, GateKind.ACCURACY, command=command)
            emit_gate_finished(
                self._events,
                GateFinishedData(gate=GateKind.ACCURACY, reused=True),
                passed=True,
            )
            return AccuracyEvaluation(executed=False, receipt=reuse)
        if self._request.agent_backend == "stub":
            return AccuracyEvaluation(executed=False)

        candidate_revision = await live.snapshot("framework-accuracy-input")
        bundle = self._request.input_bundle
        release = (
            bundle.benchmark_result is None and bundle.benchmark_result_protocol is None
        ) or not spec.benchmark_command
        execution = self._command(
            spec.accuracy_command,
            candidate_revision,
            spec.deployment_release_env_var if release else None,
        )
        result = await self._runtime_evaluation.accuracy(
            live,
            command_override=execution,
        )
        self._log_provisioning(result.provisioned_volumes, result.failure)
        self._publish_accuracy(result, process_id=f"evaluation-accuracy-{next(self._identifiers)}")
        if result.executed:
            await live.snapshot("framework-accuracy-evaluation")
        receipt = self._receipt(live, candidate_revision) if result.passed else None
        return AccuracyEvaluation(
            executed=result.executed,
            feedback=self._accuracy_feedback(result),
            receipt=receipt,
        )

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        """Run the benchmark and apply policy-selected headline semantics."""
        self._validate_objectives(objectives)
        live = self._live_workspace(workspace)
        if self._request.agent_backend == "stub":
            return BenchmarkEvaluation(executed=False)
        spec = self._runtime_evaluation.spec(live)
        candidate_revision = await live.snapshot("framework-benchmark-input")
        execution = self._command(
            spec.benchmark_command,
            candidate_revision,
            spec.deployment_release_env_var,
        )
        result = await self._runtime_evaluation.benchmark(
            live,
            command_override=execution,
            required_metrics=frozenset(item.name for item in objectives),
        )
        self._log_provisioning(result.provisioned_volumes, result.failure)
        evaluation = self._interpret_benchmark(
            result,
            objectives,
            spec.benchmark_contract,
        )
        self._publish_benchmark(
            result,
            evaluation,
            process_id=f"evaluation-benchmark-{next(self._identifiers)}",
        )
        if result.executed:
            await live.snapshot("framework-benchmark-evaluation")
        return evaluation

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Run candidate-authored recipes while isolating workspace effects."""
        validate_workspace_writable_paths(
            WorkspaceAccess.LIMITED,
            (recipe_artifact, report_location),
        )
        live = self._live_workspace(workspace)
        return await _validate_local(
            self._commands,
            self._events,
            live,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
        )

    def _publish_accuracy(self, result: TrustedAccuracyResult, *, process_id: str) -> None:
        provisioning_failure = result.failure is not None and result.failure.startswith(
            "Model-weight request"
        )
        if result.command is None or (not result.executed and provisioning_failure):
            return
        emit_gate_started(self._events, GateKind.ACCURACY, command=result.command)
        self._publish_streams(result.stdout, result.stderr, process_id, "accuracy_checker")
        emit_gate_finished(
            self._events,
            GateFinishedData(
                gate=GateKind.ACCURACY,
                output_tail=(result.output[-GATE_LOG_TAIL_CHARS:] if not result.passed else None),
            ),
            passed=result.passed,
        )

    def _publish_benchmark(
        self,
        result: TrustedBenchmarkResult,
        evaluation: BenchmarkEvaluation,
        *,
        process_id: str,
    ) -> None:
        if not result.executed or result.command is None:
            return
        emit_gate_started(self._events, GateKind.BENCHMARK, command=result.command)
        self._publish_streams(result.stdout, result.stderr, process_id, "benchmark")
        emit_gate_finished(
            self._events,
            GateFinishedData(
                gate=GateKind.BENCHMARK,
                metric=evaluation.metric_name if result.passed else None,
                value=evaluation.metric_value if result.passed else None,
                unit=(evaluation.metric_unit or evaluation.metric_name)
                if result.passed and evaluation.metric_name is not None
                else None,
                output_tail=(result.output[-GATE_LOG_TAIL_CHARS:] if not result.passed else None),
            ),
            passed=result.passed,
        )

    def _publish_streams(
        self,
        stdout: str,
        stderr: str,
        process_id: str,
        process_kind: str,
    ) -> None:
        for stream, content in (("stdout", stdout), ("stderr", stderr)):
            if content:
                self._events.emit(
                    CoreEventType.SUBPROCESS_OUTPUT,
                    data=SubprocessOutputData(
                        process_id=process_id,
                        process_kind=process_kind,
                        stream=stream,
                        content=content,
                    ),
                )

    @staticmethod
    def _accuracy_feedback(result: TrustedAccuracyResult) -> str | None:
        if result.passed:
            return None
        if result.failure is not None and result.failure.startswith("Model-weight request"):
            return result.failure
        return f"Framework accuracy gate failed.\n{result.output[-GATE_FEEDBACK_TAIL_CHARS:]}"

    @staticmethod
    def _interpret_benchmark(
        result: TrustedBenchmarkResult,
        objectives: tuple[BenchmarkObjective, ...],
        contract: TrustedBenchmarkContract | None,
    ) -> BenchmarkEvaluation:
        if not result.passed:
            provisioning_failure = result.failure is not None and result.failure.startswith(
                "Model-weight request"
            )
            feedback = (
                result.failure
                if not result.executed or provisioning_failure
                else (f"Framework benchmark failed.\n{result.output[-GATE_FEEDBACK_TAIL_CHARS:]}")
            )
            return BenchmarkEvaluation(executed=result.executed, feedback=feedback)
        if result.row is None:
            return BenchmarkEvaluation(executed=result.executed)
        if objectives:
            name = objectives[0].name
        elif len(result.row) == 1:
            name = next(iter(result.row))
        else:
            names = ", ".join(sorted(result.row))
            feedback = (
                f"benchmark evaluator reported metrics {names} but the task configures no "
                "objectives, so no headline metric is defined; declare the optimized metrics "
                "in objectives.toml"
            )
            return BenchmarkEvaluation(
                executed=result.executed,
                feedback=feedback,
                row=result.row,
            )
        objective = next((item for item in objectives if item.name == name), None)
        declaration = result.metrics.get(name)
        direction = (
            objective.direction
            if objective is not None
            else (
                MetricDirection(declaration.direction)
                if declaration is not None and declaration.direction is not None
                else (
                    MetricDirection.MAXIMIZE
                    if isinstance(contract, ScalarBenchmarkContract)
                    else None
                )
            )
        )
        return BenchmarkEvaluation(
            executed=result.executed,
            metric_name=name,
            metric_value=result.row[name],
            metric_direction=direction,
            metric_unit=declaration.unit if declaration is not None else None,
            row=result.row,
        )

    @staticmethod
    def _validate_objectives(objectives: tuple[BenchmarkObjective, ...]) -> None:
        names = [item.name for item in objectives]
        if len(names) != len(set(names)):
            message = "objective names must be unique"
            raise ValueError(message)

    @staticmethod
    def _command(command: str | None, revision: str | None, release_env: str | None) -> str | None:
        if command is None:
            return None
        variables = []
        if revision:
            variables.append(f"VIBESYS_CANDIDATE_REVISION={shlex.quote(revision)}")
        if release_env:
            variables.append(f"{release_env}=1")
        return f"env {' '.join(variables)} {command}" if variables else command

    def _log_provisioning(
        self,
        volumes: tuple[str, ...],
        failure: str | None,
    ) -> None:
        prefix = "Model-weight request could not be satisfied: "
        if failure is not None and failure.startswith(prefix):
            self._log(f"[model-request] rejected: {failure.removeprefix(prefix)}")
        if volumes:
            self._log(
                f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes)
            )
