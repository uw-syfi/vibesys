"""Concrete agent turn adapter and framework-owned validation gates."""

from __future__ import annotations

import shlex
from collections.abc import Sequence  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Literal, cast

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
from vibesys.events import FrameworkSource, GateKind
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.hypothesis_controller import (
    HypothesisEngine,
    persist_active_hypothesis,
    persist_agent_run_state,
    plan_changed_keys,
    publish_experiments_changed,
)
from vibesys.loops.agent.policy_support import (
    _load_validation_recipes,
    _reusable_validation_result,
    _run_implementer,
    _run_judge,
    _run_orchestrator_plan,
    _run_pre_round_decision,
    _run_profiler,
    _run_single_agent_round,
    _validation_input_digest,
)
from vibesys.render.sink import output_sink
from vibesys.schemas import FrameworkValidationResult

if TYPE_CHECKING:
    from vibesys.domains.base import DomainDefinition
    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.evaluators.metrics import Objective
    from vibesys.loops.agent.policy_attempts import AttemptRequest, AttemptState
    from vibesys.loops.agent.policy_ports import RoundPreparationRequest
    from vibesys.loops.agent.policy_scheduler import PlanRequest
    from vibesys.loops.agent.policy_support import _ImplementerAttempt
    from vibesys.loops.agent.roles import BuiltInAgentRoles
    from vibesys.loops.agent.state import AgentRunState, AgentRunStateStore, Hypothesis
    from vibesys.run import LoopContext
    from vibesys.schemas import (
        ImplementerResponse,
        JudgeResponse,
        OrchestratorPlan,
        PreRoundDecision,
        ProfilerSummary,
        SingleAgentRoundResponse,
    )


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


@dataclass(frozen=True)
class _LocalAgentPolicyIO:
    """Bind concrete run resources once, outside policy decision code."""

    ctx: LoopContext
    agents: BuiltInAgentRoles
    template_dir: Path
    state_namespace: str
    state_store: AgentRunStateStore
    domain_definition: DomainDefinition
    objective: str
    modality: str | None
    interface: str
    progress_path: Path
    progress_location: str
    roadmap_path: Path
    roadmap_location: str
    pareto_archive_location: str
    framework_benchmark_configured: bool
    benchmark_result: BenchmarkResult | None
    benchmark_result_protocol: Literal[2] | None
    objectives: list[Objective]
    accuracy_timeout_seconds: int | None
    benchmark_timeout_seconds: int | None
    judge_every: int
    official_eval_every: int

    def plan(self, request: PlanRequest) -> OrchestratorPlan:
        return _run_orchestrator_plan(
            self.ctx,
            template_dir=self.template_dir,
            agent=self.agents.orchestrator,
            agent_run_state=request.state,
            round_number=request.round_number,
            objective=self.objective,
            profiler_summary=request.profiler_summary,
            carry=request.carry,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            roadmap_location=self.roadmap_location,
            pareto_archive_location=self.pareto_archive_location,
            plateau_warning=request.plateau_warning,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            official_eval_every=self.official_eval_every,
            provisional_candidates=request.provisional_candidates,
            official_eval_cadence_due=(
                request.provisional_candidates + 1 >= self.official_eval_every
            ),
            profile_guidance=request.profile_guidance,
        )

    def persist_started(self, state: AgentRunState, plan: OrchestratorPlan) -> None:
        persist_agent_run_state(
            self.ctx,
            self.state_store,
            state,
            label=f"agent: start hypothesis {plan.hypothesis_id}",
        )
        publish_experiments_changed(
            self.ctx,
            state,
            "active_hypothesis_changed",
            plan_changed_keys(plan),
            namespace=self.state_namespace,
        )

    def record_continuation(self, round_number: int, hypothesis: Hypothesis) -> None:
        plan = hypothesis.plan
        issue_board.append_hypothesis_continuation(
            self.progress_path,
            round_number,
            plan=plan,
            started_round=hypothesis.started_round,
            continuation_step=hypothesis.next_step or plan.task,
        )
        self.ctx.lprint(
            f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
        )

    def checkout_rollback(
        self, commit: str, parent_round: int, failed_child_round: int | None
    ) -> bool:
        memory_paths = tuple(
            str(path.relative_to(self.ctx.workspace))
            for path in (
                self.roadmap_path,
                self.progress_path,
                issue_board.pareto_archive_path(self.progress_path),
            )
        )
        if not self.ctx.git.checkout_tree(commit, clean=True, preserve_paths=memory_paths):
            return False
        if failed_child_round is None:
            self.ctx.lprint(f"Reverted workspace to round {parent_round} ({commit[:8]}).")
        else:
            self.ctx.lprint(
                "Reverted workspace to the pre-hypothesis parent of "
                f"failed round {failed_child_round} ({commit[:8]}), "
                f"based on parent round {parent_round}."
            )
        return True

    def persist_rollback(self, state: AgentRunState, hypothesis: Hypothesis) -> AgentRunState:
        return persist_active_hypothesis(
            self.ctx,
            self.state_store,
            state,
            hypothesis,
            label=f"agent: set hypothesis {hypothesis.hypothesis_id} parent",
        )

    def warn(self, message: str) -> None:
        output_sink().framework_warning(message, source=FrameworkSource.LOOP)

    def pre_round_decision(
        self, request: RoundPreparationRequest, *, has_history: bool
    ) -> PreRoundDecision:
        return _run_pre_round_decision(
            self.ctx,
            template_dir=self.template_dir,
            agent=self.agents.orchestrator,
            round_number=request.round_number,
            objective=self.objective,
            carry=request.carry,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            has_history=has_history,
        )

    def profile(self, request: RoundPreparationRequest, focus: str) -> ProfilerSummary | None:
        return _run_profiler(
            self.ctx,
            template_dir=self.template_dir,
            agent=self.agents.profiler,
            round_number=request.round_number,
            profile_focus=focus,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            progress_path=self.progress_path,
            objective=self.objective,
        )

    def implement(self, request: AttemptRequest, state: AttemptState) -> _ImplementerAttempt:
        hypothesis = request.active_hypothesis
        artifacts = tuple(
            issue_board.display_path(path, self.ctx.workspace)
            for path in issue_board.implementer_artifact_paths(
                self.progress_path, request.round_number
            )
        )
        self.ctx.reselect_gpu()
        return _run_implementer(
            self.ctx,
            template_dir=self.template_dir,
            agent=self.agents.implementer,
            round_number=request.round_number,
            retry=state.retry,
            plan=request.plan,
            objective=self.objective,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            feedback=state.feedback,
            continuation_step=hypothesis.next_step,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_archive_location,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            prior_attempt_artifact_locations=artifacts,
            profile_guidance=request.engine.controller.guidance,
        )

    def judge(
        self, request: AttemptRequest, state: AttemptState, conflict: str | None
    ) -> JudgeResponse:
        hypothesis = request.active_hypothesis
        self.ctx.reselect_gpu()
        return _run_judge(
            self.ctx,
            template_dir=self.template_dir,
            agent=self.agents.judge,
            round_number=request.round_number,
            retry=state.retry,
            plan=request.plan,
            implementation=cast("ImplementerResponse", state.implementation),
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_archive_location,
            objective=self.objective,
            framework_revert_applied=hypothesis.revert_applied,
            framework_revert_round=hypothesis.parent_round,
            framework_revert_commit=hypothesis.revert_commit,
            gate_revalidation_pending=hypothesis.gate_revalidation_pending,
            gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
            gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
            gate_approved_metrics=hypothesis.gate_approved_metrics,
            gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            pareto_archive_conflict=conflict,
        )

    def combined(self, request: AttemptRequest, state: AttemptState) -> SingleAgentRoundResponse:
        self.ctx.reselect_gpu()
        return _run_single_agent_round(
            self.ctx,
            template_dir=self.template_dir,
            agent=self.agents.implementer,
            round_number=request.round_number,
            retry=state.retry,
            plan=request.plan,
            modality=self.modality,
            interface=self.interface,
            domain_definition=self.domain_definition,
            feedback=state.feedback,
            progress_path=self.progress_path,
            progress_location=self.progress_location,
            pareto_archive_location=self.pareto_archive_location,
            objective=self.objective,
            profile_focus=request.last_profile_focus,
            official_evaluation_due=request.planned_official_reason is not None,
            official_evaluation_reason=request.planned_official_reason,
            framework_benchmark_enabled=self.framework_benchmark_configured,
            pareto_records=request.records,
            space=state.agent_run_state.metrics,
        )

    def prepare_profile(
        self,
        engine: HypothesisEngine,
        state: AgentRunState,
        settings: ProfileGuidedInput,
        round_number: int,
    ) -> tuple[HypothesisEngine, AgentRunState]:
        prepared = engine.replace_state(state).prepare_profile(
            self.ctx, settings, round_number=round_number
        )
        persist_agent_run_state(
            self.ctx,
            self.state_store,
            prepared.state,
            label=f"profile-guided: prepare round {round_number}",
        )
        return prepared, prepared.state

    def checkpoint(self, request: AttemptRequest, state: AttemptState) -> None:
        state.agent_run_state = persist_active_hypothesis(
            self.ctx,
            self.state_store,
            state.agent_run_state,
            request.active_hypothesis,
            label=f"agent: checkpoint hypothesis {request.plan.hypothesis_id}",
        )

    def record_official_decision(
        self,
        request: AttemptRequest,
        state: AttemptState,
        *,
        run: bool,
        reason: str,
        provisional_candidates: int,
    ) -> None:
        issue_board.append_official_evaluation_decision(
            self.progress_path,
            request.round_number,
            state.retry,
            run=run,
            reason=reason,
            official_eval_every=self.official_eval_every,
            provisional_candidates=provisional_candidates,
        )

    def record_judge_skipped(self, request: AttemptRequest, outcome: str) -> None:
        issue_board.append_judge_skipped(
            self.progress_path,
            request.round_number,
            outcome=outcome,
            judge_every=self.judge_every,
        )

    def validate(
        self, request: AttemptRequest, state: AttemptState, recipe: str | None
    ) -> str | None:
        return _run_framework_validation_gate(
            self.ctx,
            recipe_artifact=recipe,
            round_number=request.round_number,
            retry=state.retry,
            progress_path=self.progress_path,
        )

    def current_commit(self) -> str | None:
        return self.ctx.git.current_sha()

    def official_gates(
        self,
        request: AttemptRequest,
        state: AttemptState,
        *,
        reuse_accuracy_pass: bool,
        candidate_commit: str | None,
    ) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
        return _run_framework_gates(
            self.ctx,
            benchmark_result=self.benchmark_result,
            benchmark_result_protocol=self.benchmark_result_protocol,
            objectives=self.objectives,
            round_number=request.round_number,
            retry=state.retry,
            progress_path=self.progress_path,
            accuracy_timeout_seconds=self.accuracy_timeout_seconds,
            benchmark_timeout_seconds=self.benchmark_timeout_seconds,
            reuse_accuracy_pass=reuse_accuracy_pass,
            candidate_revision=candidate_commit,
        )

    def next_attempt(self, round_number: int) -> int:
        return issue_board.next_implementer_attempt(self.progress_path, round_number)

    def log(self, message: str) -> None:
        self.ctx.lprint(message)
