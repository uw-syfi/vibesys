"""Shared orchestration policy over runtime-owned trusted evaluation."""

from __future__ import annotations

import asyncio
import time
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
    AgentEvaluation,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    BenchmarkObjective,
    CandidateProfile,
    CandidateProfileStatus,
    Evaluation,
    LocalValidationEvaluation,
    MetricDirection,
    ProfileField,
    ReleasedJobs,
    RuntimeContractError,
    Workspace,
)
from vs_runtime.api.infrastructure import (
    FrameworkValidationResult,
    LocalValidationRecipeError,
    ProtocolBenchmarkContract,
    RuntimeWorkspaceEvaluation,
    ScalarBenchmarkContract,
    TrustedAccuracyResult,
    TrustedBenchmarkContract,
    TrustedBenchmarkResult,
    TrustedEvaluationPlan,
    ValidationRecipe,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.inputs import InputBundle
    from vibesys.run.contracts import RunRequest
    from vs_evaluation.api import EvaluationSettlements
    from vs_runtime.api import Workspace
    from vs_runtime.api.infrastructure import (
        RunEnvironmentSession,
        WorkspaceRuntime,
    )


GATE_LOG_TAIL_CHARS = 1000
GATE_FEEDBACK_TAIL_CHARS = 4000


def trusted_evaluation_plan(
    bundle: InputBundle,
    session: RunEnvironmentSession,
) -> TrustedEvaluationPlan:
    """Lower task and environment configuration into runtime execution facts."""
    scalar = bundle.benchmark_result
    contract = (
        ScalarBenchmarkContract(
            output_argument=scalar.json_argument,
            metric=scalar.metric,
        )
        if scalar is not None
        else (ProtocolBenchmarkContract() if bundle.benchmark_result_protocol is not None else None)
    )
    return TrustedEvaluationPlan(
        accuracy_command=session.view.paths.accuracy_command,
        accuracy_timeout_seconds=bundle.manifest.accuracy.timeout_seconds,
        benchmark_command=session.view.paths.benchmark_command,
        benchmark_timeout_seconds=bundle.manifest.benchmark.timeout_seconds,
        framework_setup_timeout_seconds=session.view.framework_setup_timeout_seconds,
        profile_timeout_seconds=(
            bundle.manifest.profile.timeout_seconds if bundle.manifest.profile else None
        ),
        benchmark_contract=contract,
    )


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


def recipe_input_error_feedback(detail: str) -> str:
    """Tell the agent its validation recipe reference was unusable and how to fix it."""
    return (
        f"Your `validation_recipe_artifact` could not be used: {detail}. Set it to the "
        "workspace-relative path of a JSON file that follows the validation recipe "
        "contract, or omit it. This is an input error in your reply, not a failure of "
        "the candidate or of the framework."
    )


async def _validate_local(
    evaluation: RuntimeWorkspaceEvaluation,
    events: CoreEventWriter,
    workspace: Workspace,
    *,
    recipe_artifact: str,
    report_location: str,
) -> LocalValidationEvaluation:
    """Run the mechanism and map its detailed outcome to product feedback."""
    try:
        results = await evaluation.validate_local(
            workspace,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
            events=_LocalValidationEvents(events),
        )
    except LocalValidationRecipeError as error:
        # The recipe reference came from the agent's own reply, so this is an
        # input error the agent can fix, not a candidate or framework failure.
        return LocalValidationEvaluation(
            passed=False,
            feedback=recipe_input_error_feedback(str(error)),
            recipe_unusable=True,
        )

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
        self._log = log
        self._identifiers = count(1)
        self._released: set[str] = set()

    def _live_workspace(self, workspace: Workspace) -> Workspace:
        self._runtime_evaluation.spec(workspace)
        return workspace

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
            emit_gate_started(self._events, GateKind.ACCURACY, command=spec.accuracy_command)
            try:
                run = await self._runtime_evaluation.accuracy(
                    self._run_id,
                    live,
                    reuse=reuse,
                )
            except asyncio.CancelledError:
                self._finish_gate_error(GateKind.ACCURACY, "evaluation cancelled")
                raise
            except Exception as error:
                self._finish_gate_error(GateKind.ACCURACY, self._error_text(error))
                raise
            emit_gate_finished(
                self._events,
                GateFinishedData(gate=GateKind.ACCURACY, reused=True),
                passed=True,
            )
            return AccuracyEvaluation(executed=False, receipt=run.receipt)
        if self._request.agent_backend == "stub":
            return AccuracyEvaluation(executed=False)

        bundle = self._request.input_bundle
        release = (
            bundle.benchmark_result is None and bundle.benchmark_result_protocol is None
        ) or not spec.benchmark_command
        emit_gate_started(self._events, GateKind.ACCURACY, command=spec.accuracy_command)
        try:
            run = await self._runtime_evaluation.accuracy(
                self._run_id,
                live,
                release=release,
            )
            result = self._require_accuracy_result(run.result)
            self._log_provisioning(result.provisioned_volumes, result.failure)
            self._publish_streams(
                result.stdout,
                result.stderr,
                f"evaluation-accuracy-{next(self._identifiers)}",
                "accuracy_checker",
            )
            evaluation = AccuracyEvaluation(
                executed=result.executed,
                feedback=self._accuracy_feedback(result),
                receipt=run.receipt if result.passed else None,
            )
        except asyncio.CancelledError:
            self._finish_gate_error(GateKind.ACCURACY, "evaluation cancelled")
            raise
        except Exception as error:
            self._finish_gate_error(GateKind.ACCURACY, self._error_text(error))
            raise
        self._finish_accuracy(result)
        return evaluation

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
        emit_gate_started(self._events, GateKind.BENCHMARK, command=spec.benchmark_command)
        try:
            run = await self._runtime_evaluation.benchmark(
                live,
                required_metrics=frozenset(item.name for item in objectives),
            )
            result = run.result
            self._log_provisioning(result.provisioned_volumes, result.failure)
            evaluation = self._interpret_benchmark(
                result,
                objectives,
                run.contract,
            )
            self._publish_streams(
                result.stdout,
                result.stderr,
                f"evaluation-benchmark-{next(self._identifiers)}",
                "benchmark",
            )
        except asyncio.CancelledError:
            self._finish_gate_error(GateKind.BENCHMARK, "evaluation cancelled")
            raise
        except Exception as error:
            self._finish_gate_error(GateKind.BENCHMARK, self._error_text(error))
            raise
        self._finish_benchmark(result, evaluation)
        return evaluation

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Run candidate-authored recipes while isolating workspace effects."""
        return await _validate_local(
            self._runtime_evaluation,
            self._events,
            workspace,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
        )

    async def agent_evaluations(self, workspace: Workspace) -> tuple[AgentEvaluation, ...]:
        """Return no history: without the evaluation tool, agents submit nothing."""
        del workspace
        return ()

    def settlements(self) -> EvaluationSettlements:
        """Fail explicitly when this run offers no agent evaluation tools."""
        message = "agent evaluation settlements are unavailable"
        raise RuntimeContractError(message)

    def current_time(self) -> float:
        """Use UTC time so persisted deadlines survive process restarts."""
        return time.time()

    async def wait_until(self, deadline_at_s: float) -> None:
        """Wait without agent calls, with cancellation releasing the timer."""
        await asyncio.sleep(max(0.0, deadline_at_s - self.current_time()))

    async def submitted_generation(self, handle_id: str) -> int:
        """No agent submission exists without the evaluation tool."""
        message = f"evaluation {handle_id!r} has no submitted generation"
        raise RuntimeContractError(message)

    async def submitted_deadline(self, handle_id: str) -> float:
        """No agent submission exists without the evaluation tool."""
        message = f"evaluation {handle_id!r} has no submitted deadline"
        raise RuntimeContractError(message)

    async def cancel_submitted(self, handle_id: str) -> None:
        """No agent submission exists without the evaluation tool."""
        message = f"evaluation {handle_id!r} has no submitted evaluation"
        raise RuntimeContractError(message)

    async def accepted_evidence_ids(self, handle_id: str) -> tuple[str, ...]:
        """No agent-submitted evidence exists without the evaluation tool."""
        del handle_id
        return ()

    async def submitted_report(self, handle_id: str) -> str:
        """No agent submission exists without the evaluation tool."""
        message = f"evaluation {handle_id!r} has no submitted report"
        raise RuntimeContractError(message)

    async def submitted_revision(self, handle_id: str) -> str:
        """No agent submission exists without the evaluation tool."""
        message = f"evaluation {handle_id!r} has no submitted revision"
        raise RuntimeContractError(message)

    async def can_profile(self) -> bool:
        """Return False: without the evaluation tool the run provisions no profiler agent."""
        return False

    async def profile(
        self,
        revision: str,
        request: str,
        *,
        member_id: str,
        required_fields: tuple[ProfileField, ...] = (),
    ) -> CandidateProfile:
        """Fail typed: without the evaluation tool the run provisions no profiler agent."""
        del request, member_id, required_fields
        return CandidateProfile(
            revision=revision,
            status=CandidateProfileStatus.FAILED,
            failure="no profiler agent is provisioned",
        )

    async def reopen_jobs(self, member_id: str) -> None:
        """Reconcile a completed release and open a fresh generation for resumed work."""
        self._released.discard(member_id)

    async def jobs_released(self, member_id: str) -> bool:
        """Project whether the member's durable scope refuses ordinary admission.

        Closing and completed releases both fence new work. Recovery can
        reconcile cleanup before opening a fresh scope generation.
        """
        return member_id in self._released

    async def release_jobs(self, member_id: str) -> ReleasedJobs:
        """Release nothing: without the evaluation tool, agents start no cluster jobs.

        Its profiles already fail without starting, so only the first-release
        record is kept.
        """
        first_release = member_id not in self._released
        self._released.add(member_id)
        return ReleasedJobs(
            member_id=member_id,
            evaluations=(),
            profiler_operations=(),
            first_release=first_release,
        )

    def _finish_accuracy(self, result: TrustedAccuracyResult) -> None:
        emit_gate_finished(
            self._events,
            GateFinishedData(
                gate=GateKind.ACCURACY,
                output_tail=(result.output[-GATE_LOG_TAIL_CHARS:] if not result.passed else None),
            ),
            passed=result.passed,
        )

    @staticmethod
    def _require_accuracy_result(
        result: TrustedAccuracyResult | None,
    ) -> TrustedAccuracyResult:
        if result is None:
            message = "runtime accuracy execution unexpectedly returned a reused result"
            raise RuntimeContractError(message)
        return result

    def _finish_benchmark(
        self,
        result: TrustedBenchmarkResult,
        evaluation: BenchmarkEvaluation,
    ) -> None:
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

    def _finish_gate_error(self, gate: GateKind, output: str) -> None:
        emit_gate_finished(
            self._events,
            GateFinishedData(gate=gate, output_tail=output[-GATE_LOG_TAIL_CHARS:]),
            passed=False,
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        detail = str(error)
        return f"{type(error).__name__}: {detail}" if detail else type(error).__name__

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
                result.failure_reason or result.failure
                if not result.executed or provisioning_failure
                else result.failure_reason
                or (f"Framework benchmark failed.\n{result.output[-GATE_FEEDBACK_TAIL_CHARS:]}")
            )
            return BenchmarkEvaluation(
                executed=result.executed,
                feedback=feedback,
                failure_kind=result.failure_kind,
                partial_measurement=result.partial_measurement,
            )
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
                failure_kind=BenchmarkFailureKind.WORKLOAD,
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


def create_evaluation(
    run_id: str,
    request: RunRequest,
    runtime: WorkspaceRuntime,
    events: CoreEventWriter,
    log: Callable[[str], None],
) -> Evaluation:
    """Bind shared VibeSys evaluation policy to one run's execution mechanism."""
    return _EvaluationAdapter(run_id, request, runtime, events, log)


__all__ = ["create_evaluation", "trusted_evaluation_plan"]
