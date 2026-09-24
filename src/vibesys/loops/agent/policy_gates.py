"""Framework validation and official evaluation gates for agent runs."""

from __future__ import annotations

import shlex
from collections.abc import Sequence  # noqa: TC003  # tracked: #288
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Literal

from vibesys.evaluators.gates import (
    GATE_LOG_TAIL_CHARS,
    GATE_RECORD_TAIL_CHARS,
    FrameworkBenchmarkOutcome,
    emit_gate_finished,
    emit_gate_started,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.evaluators.input_manifest import (  # noqa: TC001  # tracked: #288
    BenchmarkResult,
)
from vibesys.events import (
    GateKind,
)
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.policy_support import (
    _load_validation_recipes,
    _reusable_validation_result,
    _validation_input_digest,
)
from vibesys.schemas import (
    FrameworkValidationResult,
)

if TYPE_CHECKING:
    from vibesys.evaluators.metrics import (
        Objective,
    )
    from vibesys.run import LoopContext


def _run_framework_validation_gate(  # noqa: C901, PLR0912, PLR0915  # tracked: #288
    ctx: LoopContext,
    *,
    recipe_artifact: str | None,
    round_number: int,
    retry: int,
    progress_path: Path,
) -> str | None:
    """Execute judge-audited local checks once and cache exact-input passes.

    This gate intentionally excludes target, deployment, profiler, benchmark,
    and official evaluator work. The Judge audits that boundary before a PASS
    can reach this function. Commands must be non-mutating; any workspace write
    fails the gate and is restored to the pre-validation checkpoint.
    """
    if recipe_artifact is None:
        return None

    try:
        recipes = _load_validation_recipes(ctx.workspace, recipe_artifact)
    except ValueError as exc:
        return f"Framework local validation recipe error: {exc}."

    labels = [recipe.name for recipe in recipes]
    if len(set(labels)) != len(labels):
        return "Framework local validation recipes contain duplicate names."

    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-validation-input")
    checkpoint = ctx.git.current_sha()
    if checkpoint is None:
        return "Framework local validation could not establish a workspace checkpoint."

    results: list[FrameworkValidationResult] = []
    restore_required = False
    for recipe in recipes:
        try:
            input_digest = _validation_input_digest(ctx.workspace, recipe)
        except (OSError, ValueError) as exc:
            results.append(
                FrameworkValidationResult(
                    recipe=recipe,
                    input_digest="",
                    passed=False,
                    error=str(exc),
                )
            )
            break

        reused = _reusable_validation_result(progress_path, recipe, input_digest)
        if reused is not None:
            results.append(reused)
            emit_gate_started(
                GateKind.VALIDATION,
                recipe=recipe.name,
                command=recipe.command,
                round_label=f"round-{round_number}",
            )
            emit_gate_finished(
                GateKind.VALIDATION,
                passed=True,
                recipe=recipe.name,
                reused=True,
                round_label=f"round-{round_number}",
            )
            continue

        emit_gate_started(
            GateKind.VALIDATION,
            recipe=recipe.name,
            command=recipe.command,
            round_label=f"round-{round_number}",
        )
        try:
            execution = ctx.judge_backend.execute(
                recipe.command,
                timeout=recipe.timeout_seconds,
            )
            output = execution.output.strip()
            passed = execution.exit_code == 0
            result = FrameworkValidationResult(
                recipe=recipe,
                input_digest=input_digest,
                passed=passed,
                exit_code=execution.exit_code,
                output=output[-GATE_RECORD_TAIL_CHARS:],
                error=None if passed else "command exited nonzero",
            )
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            result = FrameworkValidationResult(
                recipe=recipe,
                input_digest=input_digest,
                passed=False,
                error=f"command could not be executed: {exc}",
            )

        changes = ctx.git.pending_changes()
        if changes:
            restore_required = True
            shown = ", ".join(changes[:8])
            suffix = "" if len(changes) <= 8 else f", ... (+{len(changes) - 8} more)"  # noqa: PLR2004  # tracked: #288
            result = result.model_copy(
                update={
                    "passed": False,
                    "error": (f"validation command mutated the workspace: {shown}{suffix}"),
                }
            )
        results.append(result)
        failure_detail = (
            None if result.passed else (result.error or result.output or "unknown failure")
        )
        emit_gate_finished(
            GateKind.VALIDATION,
            passed=result.passed,
            recipe=recipe.name,
            output_tail=(None if failure_detail is None else failure_detail[-GATE_LOG_TAIL_CHARS:]),
            round_label=f"round-{round_number}",
        )
        if not result.passed:
            break

    if restore_required:  # noqa: SIM102  # tracked: #288
        if not ctx.git.checkout_tree(checkpoint, clean=True):
            results[-1] = results[-1].model_copy(
                update={
                    "passed": False,
                    "error": "validation mutation could not be restored",
                }
            )

    artifact = issue_board.write_validation_result_artifact(
        progress_path,
        round_number,
        retry,
        results,
    )
    artifact_location = issue_board.display_path(artifact, ctx.workspace)
    issue_board.append_framework_validation_gate(
        progress_path,
        round_number,
        retry,
        artifact=artifact_location,
        results=results,
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-validation")

    failed = next((result for result in results if not result.passed), None)
    if failed is None:
        return None
    detail = failed.error or failed.output or "unknown failure"
    return (
        f"Framework local validation failed for {failed.recipe.name!r}: {detail}. "
        f"Inspect `{artifact_location}` and repair only the affected local contract."
    )


def _deployment_release_env_var(ctx: LoopContext) -> str | None:
    return ctx.run_environment_view.deployment_release_env_var


def _with_candidate_revision(
    command: str,
    candidate_revision: str | None,
    *,
    release_deployment_env_var: str | None = None,
) -> str:
    """Annotate an official command with its bounded deployment-lease lifecycle."""
    environment: list[str] = []
    if candidate_revision:
        environment.append(f"VIBESYS_CANDIDATE_REVISION={shlex.quote(candidate_revision)}")
    if release_deployment_env_var:
        environment.append(f"{release_deployment_env_var}=1")
    if not environment:
        return command
    return f"env {' '.join(environment)} {command}"


def _run_framework_accuracy_gate(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    round_number: int,
    retry: int,
    progress_path: Path,
    timeout_seconds: int | None = None,
    candidate_revision: str | None = None,
    release_deployment_after: bool = False,
) -> str | None:
    """Run the immutable manifest accuracy command after an agent reports PASS."""
    command = ctx.judge_accuracy_command
    execution_command = None
    if command:
        execution_command = _with_candidate_revision(
            command,
            candidate_revision,
            release_deployment_env_var=(
                _deployment_release_env_var(ctx) if release_deployment_after else None
            ),
        )
    result = run_accuracy_gate(
        ctx,
        process_id=f"accuracy-{round_number}-{retry}",
        timeout_seconds=framework_command_timeout(ctx, timeout_seconds),
        execution_command=execution_command,
        round_label=f"round-{round_number}",
    )
    if result.passed and not result.executed:
        return None

    issue_board.append_framework_accuracy_gate(
        progress_path,
        round_number,
        retry,
        command=result.command or "(not configured)",
        passed=result.passed,
        output=result.output[-GATE_RECORD_TAIL_CHARS:],
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-accuracy")
    return result.feedback


def _run_framework_benchmark(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    result_spec: BenchmarkResult | None,
    result_protocol: Literal[2] | None = None,
    objectives: Sequence[Objective] = (),
    round_number: int,
    retry: int,
    progress_path: Path,
    timeout_seconds: int | None = None,
    candidate_revision: str | None = None,
) -> FrameworkBenchmarkOutcome:
    """Run the shared benchmark gate and record its agent-loop bookkeeping.

    The gate itself (result recovery, parsing, the collision-proof result
    path, and the typed gate events) lives in :mod:`vibesys.evaluators.gates`;
    this wrapper owns what is agent-loop specific: progress notes and
    workspace snapshots.
    """
    execution_base = None
    if ctx.judge_benchmark_command:
        execution_base = _with_candidate_revision(
            ctx.judge_benchmark_command,
            candidate_revision,
            release_deployment_env_var=_deployment_release_env_var(ctx),
        )
    result = run_benchmark_gate(
        ctx,
        result_spec=result_spec,
        result_protocol=result_protocol,
        objectives=objectives,
        process_id=f"benchmark-{round_number}-{retry}",
        output_slug=f"{round_number}-{retry}",
        timeout_seconds=framework_command_timeout(ctx, timeout_seconds),
        execution_base=execution_base,
        round_label=f"round-{round_number}",
    )
    if not result.executed:
        return result.outcome

    issue_board.append_framework_benchmark(
        progress_path,
        round_number,
        retry,
        command=result.command or "(not configured)",
        passed=result.passed,
        metric_name=(
            result.outcome.metric_name or (result_spec.metric if result_spec is not None else None)
        ),
        metric_value=result.outcome.metric_value,
        output=result.output[-GATE_RECORD_TAIL_CHARS:],
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-benchmark")
    return result.outcome


def _reconcile_model_requests(ctx: LoopContext) -> str | None:
    """Stage any candidate-declared model weights before the framework gates.

    The candidate may declare extra model weights it needs in
    ``.vibesys/models.json`` (see ``vibesys.sandbox.model_requests``). This runs
    once per gate invocation, before deploy; a malformed or disallowed manifest
    is returned as gate feedback so the candidate can correct it rather than
    crashing the run. Only meaningful for Modal runs (weights live in Modal
    Volumes); a no-op otherwise.
    """
    if getattr(ctx.run_environment_view, "env_kind", "local") != "modal":
        return None
    from vibesys.sandbox.model_requests import (  # noqa: PLC0415  # tracked: #288
        ModelRequestError,
        reconcile_model_requests,
    )

    try:
        volumes = reconcile_model_requests(ctx.workspace, log=ctx.lprint)
    except ModelRequestError as exc:
        ctx.lprint(f"[model-request] rejected: {exc}")
        return f"Model-weight request could not be satisfied: {exc}"
    if volumes:
        ctx.lprint(f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes))
    return None


def _run_framework_gates(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    benchmark_result: BenchmarkResult | None,
    benchmark_result_protocol: Literal[2] | None = None,
    objectives: Sequence[Objective] = (),
    round_number: int,
    retry: int,
    progress_path: Path,
    accuracy_timeout_seconds: int | None = None,
    benchmark_timeout_seconds: int | None = None,
    reuse_accuracy_pass: bool = False,
    candidate_revision: str | None = None,
) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
    """Run the framework-owned gates, returning the first failure's feedback.

    The benchmark outcome is always returned so a passing protocol-path run can
    carry its complete metric row to the round record; it is empty whenever the
    benchmark did not run.
    """
    if ctx.agent_client.backend_name == "stub":
        return None, FrameworkBenchmarkOutcome(), False
    resource_feedback = _reconcile_model_requests(ctx)
    if resource_feedback is not None:
        return resource_feedback, FrameworkBenchmarkOutcome(), False
    if reuse_accuracy_pass:
        feedback = None
        issue_board.append_framework_accuracy_gate(
            progress_path,
            round_number,
            retry,
            command=ctx.judge_accuracy_command or "(not configured)",
            passed=True,
            output=(
                "Reused the prior framework-owned PASS for this exact candidate "
                "commit; a later gate, not accuracy, caused the retry."
            ),
        )
        emit_gate_started(
            GateKind.ACCURACY,
            command=ctx.judge_accuracy_command or None,
            round_label=f"round-{round_number}",
        )
        emit_gate_finished(
            GateKind.ACCURACY,
            passed=True,
            reused=True,
            round_label=f"round-{round_number}",
        )
    else:
        feedback = _run_framework_accuracy_gate(
            ctx,
            round_number=round_number,
            retry=retry,
            progress_path=progress_path,
            timeout_seconds=accuracy_timeout_seconds,
            candidate_revision=candidate_revision,
            release_deployment_after=(
                (benchmark_result is None and benchmark_result_protocol is None)
                or not ctx.judge_benchmark_command
            ),
        )
    if feedback is not None:
        return feedback, FrameworkBenchmarkOutcome(), False
    benchmark = _run_framework_benchmark(
        ctx,
        result_spec=benchmark_result,
        result_protocol=benchmark_result_protocol,
        objectives=objectives,
        round_number=round_number,
        retry=retry,
        progress_path=progress_path,
        timeout_seconds=benchmark_timeout_seconds,
        candidate_revision=candidate_revision,
    )
    return benchmark.feedback, benchmark, True
