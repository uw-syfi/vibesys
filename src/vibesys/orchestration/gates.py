"""VibeSys evaluation policy over runtime-owned trusted execution."""

# This policy adapter consumes the private workspace/resource owner until that
# owner moves to vs_runtime.
# lint-waiver: LW-040101 [SLF001]; the adapter and workspace manager share one run-owned resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import shlex
import subprocess
from itertools import count
from typing import TYPE_CHECKING

from vibesys.evaluators.gates import (
    GATE_FEEDBACK_TAIL_CHARS,
    GATE_LOG_TAIL_CHARS,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.events import CoreEventType, GateFinishedData, GateKind, SubprocessOutputData
from vibesys.orchestration.local_validation import validate_local
from vibesys.orchestration.workspaces import WorkspaceHandle
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
    ScalarBenchmarkContract,
    TrustedAccuracyResult,
    TrustedBenchmarkContract,
    TrustedBenchmarkResult,
)

if TYPE_CHECKING:
    from vibesys.context import _RunResources
    from vibesys.orchestration._host import HostResources


class _EvaluationAdapter:
    """Apply VibeSys receipt, snapshot, metric, and event policy."""

    def __init__(self, host: HostResources) -> None:
        self._host = host
        self._identifiers = count(1)

    @staticmethod
    def _live_workspace(workspace: Workspace) -> WorkspaceHandle:
        if not isinstance(workspace, WorkspaceHandle):
            message = "workspace must be a live handle from this run"
            raise TypeError(message)
        return workspace

    def _receipt(self, workspace: WorkspaceHandle, revision: str) -> AccuracyReceipt:
        return AccuracyReceipt(
            run_id=self._host.run_id,
            workspace_id=workspace.id,
            revision=revision,
        )

    def _validate_receipt_owner(self, workspace: WorkspaceHandle, receipt: AccuracyReceipt) -> None:
        if receipt.run_id != self._host.run_id:
            message = "accuracy receipt belongs to another run"
            raise RuntimeContractError(message)
        if receipt.workspace_id != workspace.id:
            message = "accuracy receipt belongs to another workspace"
            raise RuntimeContractError(message)

    def _validate_receipt_revision(
        self, workspace: WorkspaceHandle, receipt: AccuracyReceipt
    ) -> None:
        current_revision = workspace.revision
        if current_revision is None:
            message = "accuracy receipt does not match the current workspace revision"
            raise RuntimeContractError(message)
        if receipt.revision == current_revision:
            return
        git = self._host.workspaces._resources_for(workspace).git
        try:
            same_candidate = git.candidate_patch(receipt.revision) == git.candidate_patch(
                current_revision
            )
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            message = "accuracy receipt revision is unavailable in this workspace"
            raise RuntimeContractError(message) from error
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
        resources = self._host.workspaces._resources_for(live)
        if reuse is not None:
            self._validate_receipt_owner(live, reuse)
            await live.snapshot("framework-accuracy-reuse-input")
            self._validate_receipt_revision(live, reuse)
            command = resources.trusted_evaluation_plan.accuracy_command
            emit_gate_started(self._host.events, GateKind.ACCURACY, command=command)
            emit_gate_finished(
                self._host.events,
                GateFinishedData(gate=GateKind.ACCURACY, reused=True),
                passed=True,
            )
            return AccuracyEvaluation(executed=False, receipt=reuse)
        if self._host.request.agent_backend == "stub":
            return AccuracyEvaluation(executed=False)

        candidate_revision = await live.snapshot("framework-accuracy-input")
        view = resources.run_environment_view
        bundle = self._host.request.input_bundle
        release = (
            bundle.benchmark_result is None and bundle.benchmark_result_protocol is None
        ) or not view.paths.benchmark_command
        execution = self._command(
            view.paths.accuracy_command,
            candidate_revision,
            view.deployment_release_env_var if release else None,
        )
        async with self._host.workspaces._mutation_lock(live):
            result = await resources.trusted_evaluation.accuracy(command_override=execution)
        self._log_provisioning(resources, result.provisioned_volumes, result.failure)
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
        if self._host.request.agent_backend == "stub":
            return BenchmarkEvaluation(executed=False)
        resources = self._host.workspaces._resources_for(live)
        candidate_revision = await live.snapshot("framework-benchmark-input")
        view = resources.run_environment_view
        execution = self._command(
            view.paths.benchmark_command,
            candidate_revision,
            view.deployment_release_env_var,
        )
        async with self._host.workspaces._mutation_lock(live):
            result = await resources.trusted_evaluation.benchmark(
                command_override=execution,
                required_metrics=frozenset(item.name for item in objectives),
            )
        self._log_provisioning(resources, result.provisioned_volumes, result.failure)
        evaluation = self._interpret_benchmark(
            result,
            objectives,
            resources.trusted_evaluation_plan.benchmark_contract,
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
        return await validate_local(
            self._host,
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
        emit_gate_started(self._host.events, GateKind.ACCURACY, command=result.command)
        self._publish_streams(result.stdout, result.stderr, process_id, "accuracy_checker")
        emit_gate_finished(
            self._host.events,
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
        emit_gate_started(self._host.events, GateKind.BENCHMARK, command=result.command)
        self._publish_streams(result.stdout, result.stderr, process_id, "benchmark")
        emit_gate_finished(
            self._host.events,
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
                self._host.events.emit(
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

    @staticmethod
    def _log_provisioning(
        resources: _RunResources,
        volumes: tuple[str, ...],
        failure: str | None,
    ) -> None:
        prefix = "Model-weight request could not be satisfied: "
        if failure is not None and failure.startswith(prefix):
            resources.lprint(f"[model-request] rejected: {failure.removeprefix(prefix)}")
        if volumes:
            resources.lprint(
                f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes)
            )
