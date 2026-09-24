"""Durable round control for the multi strategy over public host capabilities."""

from __future__ import annotations

import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import (
    AttemptDecision,
    AttemptState,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    PerformanceProjection,
    attempt_was_reviewed,
)
from vibesys.agent_run.errors import StrategySessionError
from vibesys.agent_run.evidence import (
    _FAILED_HYPOTHESIS_OUTCOMES,
    CarryOver,
    _detect_plateau,
    _format_metric_row,
    _pareto_archive_conflict,
    _pareto_archive_summary,
    _pareto_frontier_records,
    _provisional_candidates_since_official,
    _record_candidate_metrics,
    _select_final_candidate,
    _terminal_workspace_notice,
)
from vibesys.agent_run.hypotheses import adopt_metric_space, update_active_hypothesis
from vibesys.agent_run.record import RecordInput, build_round_record
from vibesys.agent_run.state import AgentRunState
from vibesys.evaluators.gates import (
    GATE_LOG_TAIL_CHARS,
    GATE_RECORD_TAIL_CHARS,
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.events import (
    CoreEventType,
    EventStatus,
    ExperimentsChangedData,
    GateKind,
    RoundFinishedData,
)
from vibesys.loops.multi.decisions import (
    AttemptRequest,
    HypothesisEngine,
    PlanRequest,
    RoundSelection,
    TerminalRequest,
    candidate_evidence_is_fresh,
    implementation_keeps_hypothesis_active,
    implementation_requests_continuation,
    official_evaluation_reason,
    review_due,
    transition_round,
)
from vibesys.loops.multi.turns import MultiAgentTurns
from vibesys.loops.multi.validation import (
    _load_validation_recipes,
    _reusable_validation_result,
    _validation_input_digest,
)
from vibesys.orchestration.runtime import MeasurementOptions
from vibesys.schemas import (
    CandidateDisposition,
    FrameworkValidationResult,
    HypothesisOutcome,
    ProfilerSummary,
    Verdict,
)
from vs_agent.api import RoundProgress
from vs_loop_state.api import RoundHistory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.agent_run.options import AgentOrchestrationOptions
    from vibesys.events import ExperimentsChangeReason
    from vibesys.orchestration.runtime import RunContext


MultiSessionError = StrategySessionError


@dataclass(frozen=True)
class MultiRound:
    """One selected hypothesis and the mutable attempt state for its round."""

    selection: RoundSelection
    request: AttemptRequest
    attempt: AttemptState


class _TerminalPolicy:
    """Multi role policy facts consumed by the pure round transition."""

    def project_performance(
        self, request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        """Use the framework metric or reviewed implementer evidence."""
        implementation = state.implementation
        official = state.passed and state.official_reason is not None
        implementation_metric = (
            implementation.perf_metric if implementation is not None and official else None
        )
        if (
            implementation_metric is None
            and official
            and implementation is not None
            and implementation.hypothesis_outcome
            in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
            and request.active_hypothesis.gate_revalidation_pending
        ):
            implementation_metric = request.active_hypothesis.gate_approved_perf_metric
        profile_skipped = state.framework_perf_metric is None and implementation_metric is None
        if state.framework_perf_metric is not None and official:
            return PerformanceProjection(
                metric=state.framework_perf_metric,
                unit=state.framework_benchmark.metric_name,
                provenance="framework",
                profile_skipped=profile_skipped,
                accepted_metrics={},
                accepted_evaluation_artifact=None,
                next_single_response=None,
            )
        if implementation_metric is None:
            return PerformanceProjection(None, None, None, profile_skipped, {}, None, None)
        if implementation is not None and implementation.perf_metric is not None:
            return PerformanceProjection(
                implementation_metric,
                implementation.perf_unit,
                "implementer",
                profile_skipped,
                dict(implementation.metrics),
                implementation.evaluation_artifact,
                None,
            )
        hypothesis = request.active_hypothesis
        return PerformanceProjection(
            implementation_metric,
            hypothesis.gate_approved_perf_unit,
            "implementer",
            profile_skipped,
            dict(hypothesis.gate_approved_metrics),
            hypothesis.gate_approved_evaluation_artifact,
            None,
        )

    def reviewed(self, state: AttemptState) -> bool:
        """Only the final attempt's judge verdict counts as a review."""
        return attempt_was_reviewed(state.judge)

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        """A reviewed or provisional implementation may retain its lease."""
        return implementation_keeps_hypothesis_active(
            state.implementation, continuation_rounds=continuation_rounds
        )

    def terminal_success_needs_parent_choice(
        self, state: AttemptState, continuation_rounds: int
    ) -> bool:
        """Terminal reviewed edits need the next designer to choose a parent."""
        implementation = state.implementation
        return (
            implementation is not None
            and not self.keeps_hypothesis_active(state, continuation_rounds)
            and implementation.hypothesis_outcome is not HypothesisOutcome.NOMINATED
        )


class MultiSession:
    """Own the multi strategy's state, roles, attempts, and checkpoints."""

    def __init__(self, ctx: RunContext, options: AgentOrchestrationOptions) -> None:
        """Bind one host; resource opening occurs in ``open``."""
        self.ctx = ctx
        self.options = options
        self.workspace = ctx.workspaces.root
        self.turns = MultiAgentTurns(ctx, options)
        self.terminal_policy = _TerminalPolicy()
        self.framework_benchmark_configured = (
            ctx.request.input_bundle.benchmark_result is not None
            or ctx.request.input_bundle.benchmark_result_protocol is not None
        )

    @classmethod
    async def open(cls, ctx: RunContext, options: AgentOrchestrationOptions) -> MultiSession:
        """Open roles and recover the strategy's typed state."""
        session = cls(ctx, options)
        await session.turns.open()
        try:
            await session._initialize()
        except BaseException:
            await session.turns.close()
            raise
        return session

    async def _initialize(self) -> None:
        ctx = self.ctx
        turns = self.turns
        ctx.run_configured(
            run_log_path=str(ctx.environment.run_log_path),
            project_root=str(ctx.request.project_root),
            objective=turns.objective,
        )
        issue_board.ensure_progress_file(turns.progress_path)
        issue_board.ensure_roadmap_file(turns.roadmap_path)
        issue_board.write_validation_recipe_schema(turns.progress_path)
        previous = await ctx.state.load(AgentRunState)
        state = adopt_metric_space(previous or AgentRunState(), self.options.metric_space)
        self.state = state
        self.records = state.rounds
        self.history = RoundHistory(records=self.records)
        self.carry = CarryOver(regression_info=_terminal_workspace_notice(self.records))
        self.round_number = len(self.records) + 1
        self.last_profile_focus = "general latency hotspots on /v1/completions"
        self.engine = HypothesisEngine.create(state)
        if previous != state:
            await self._save_state(state, label="multi: initialize policy state")

    async def _save_state(self, state: AgentRunState, *, label: str) -> None:
        await self.ctx.state.checkpoint(
            sequence=self.round_number,
            writes={"state.json": state},
            candidate=False,
            label=label,
            publish=state,
        )

    @property
    def has_next_round(self) -> bool:
        """Whether the durable cursor remains within the total round budget."""
        return self.round_number <= self.options.max_rounds

    @asynccontextmanager
    async def round_scope(self) -> AsyncIterator[None]:
        """Attribute one round's turns and release progress on all exits."""
        number = self.round_number
        self.ctx.switch_log(f"round{number:03d}")
        issue_board.write_pareto_archive(
            self.turns.progress_path, _pareto_archive_summary(self.records, self.state.metrics)
        )
        progress = RoundProgress(number, self.options.max_rounds)
        self.ctx.log(f"\n{'=' * 60}\n  {progress.label()}\n{'=' * 60}\n")
        with self.ctx.agents.progress(progress):
            yield

    async def select_hypothesis(self) -> MultiRound:
        """Choose a designer plan or continue the active hypothesis."""
        state = self.state
        engine = self.engine
        hypothesis = state.active_hypothesis
        number = self.round_number
        if hypothesis is None:
            provisional = _provisional_candidates_since_official(self.records)
            summary = await self._pre_round_profile()
            plan = await self.turns.plan(
                PlanRequest(
                    round_number=number,
                    state=state,
                    records=self.records,
                    carry=self.carry,
                    profiler_summary=summary,
                    plateau_warning=_detect_plateau(self.records),
                    provisional_candidates=provisional,
                    profile_guidance=engine.controller.guidance,
                )
            )
            parent_round = plan.revert_to_round or (number - 1 if number > 1 else None)
            parent = next(
                (
                    record
                    for record in reversed(self.records)
                    if record.round_number == parent_round
                ),
                None,
            )
            engine = engine.replace_state(state).start(
                plan,
                started_round=number,
                parent_round=parent_round,
                parent_commit=(
                    parent.commit
                    if parent is not None and parent.commit is not None
                    else self.workspace.revision
                ),
            )
            state = engine.state
            hypothesis = state.active_hypothesis
            if hypothesis is None:
                raise MultiSessionError.missing_active()
            await self._save_state(state, label=f"multi: start hypothesis {plan.hypothesis_id}")
            self._announce_experiments("active_hypothesis_changed", state)
        else:
            plan = hypothesis.plan
            issue_board.append_hypothesis_continuation(
                self.turns.progress_path,
                number,
                plan=plan,
                started_round=hypothesis.started_round,
                continuation_step=hypothesis.next_step or plan.task,
            )
            self.ctx.log(
                f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
            )
        self.engine = engine
        self.state = state
        reason = official_evaluation_reason(
            records=self.records,
            round_number=number,
            max_rounds=self.options.max_rounds,
            official_eval_every=self.options.official_eval_every,
            requested=plan.request_official_evaluation,
            candidate_ready=True,
        )
        selected = RoundSelection(
            engine=engine,
            state=state,
            hypothesis=hypothesis,
            plan=plan,
            planned_official_reason=reason,
        )
        await self._apply_rollback(selected)
        request = AttemptRequest(
            round_number=number,
            plan=plan,
            planned_official_reason=reason,
            records=self.records,
            active_hypothesis=hypothesis,
            engine=self.engine,
            last_profile_focus=self.last_profile_focus,
        )
        attempt = AttemptState(
            agent_run_state=self.state,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return MultiRound(selected, request, attempt)

    async def _pre_round_profile(self) -> ProfilerSummary | None:
        decision = await self.turns.pre_round_decision(
            self.round_number,
            self.carry,
            has_history=not (self.round_number == 1 and not self.records),
        )
        if not decision.need_profile:
            return None
        return await self.turns.profile(
            self.round_number,
            decision.profile_focus or "general steady-state benchmark hotspots",
        )

    async def _apply_rollback(self, selection: RoundSelection) -> None:
        parent_round = selection.plan.revert_to_round
        hypothesis = selection.hypothesis
        if parent_round is None or hypothesis.revert_applied:
            return
        target = next(
            (record for record in self.records if record.round_number == parent_round), None
        )
        if target is None or not target.commit:
            self.ctx.log(f"cannot revert: no commit recorded for round {parent_round}")
            return
        rollback, failed_child = self.history.resolve_rollback_commit(
            target, _FAILED_HYPOTHESIS_OUTCOMES
        )
        if rollback is None:
            raise MultiSessionError.missing_rollback()
        memory = self._memory_paths()
        await self.workspace.restore(rollback, clean=True, preserve_paths=memory)
        hypothesis.revert_applied = True
        hypothesis.revert_commit = rollback
        hypothesis.parent_commit = rollback
        state = update_active_hypothesis(selection.state, hypothesis)
        await self._save_state(
            state, label=f"multi: set hypothesis {hypothesis.hypothesis_id} parent"
        )
        self.state = state
        self.engine = self.engine.replace_state(state)
        if failed_child is None:
            self.ctx.log(f"Reverted workspace to round {parent_round} ({rollback[:8]}).")
        else:
            self.ctx.log(
                f"Reverted workspace to the pre-hypothesis parent of failed round "
                f"{failed_child} ({rollback[:8]}), based on parent round {parent_round}."
            )

    def remaining_attempts(self, selected: MultiRound) -> range:
        """Resume after the last durable paid attempt marker."""
        first = issue_board.next_implementer_attempt(
            self.turns.progress_path, selected.request.round_number
        )
        limit = self.options.max_retries_per_round
        if first > limit:
            raise MultiSessionError.exhausted_attempts(self.round_number, first, limit)
        if first > 1:
            self.ctx.log(f"[resume] round {self.round_number} continues at attempt {first}/{limit}")
        return range(first, limit + 1)

    async def begin_attempt(self, selected: MultiRound, retry: int) -> None:
        """Prepare attempt state and device before the paid implementer turn."""
        self.ctx.log(f"\n--- attempt {retry}/{self.options.max_retries_per_round} ---\n")
        attempt = selected.attempt
        attempt.retry = retry
        attempt.official_reason = None
        attempt.judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
        await self._save_state(
            attempt.agent_run_state,
            label=f"multi: start round {self.round_number} attempt {retry}",
        )
        await self.ctx.environment.reselect_device()

    async def implement(self, selected: MultiRound) -> bool:
        """Invoke one paid implementer attempt and retain its evidence."""
        response, synthesized = await self.turns.implement(selected.request, selected.attempt)
        selected.attempt.implementation = response
        if synthesized:
            selected.attempt.judge = JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
            self.ctx.log(
                f"[implementer] attempt {selected.attempt.retry} returned no parseable response; "
                "retrying within the same round"
            )
            return False
        return True

    async def review(self, selected: MultiRound) -> AttemptDecision:
        """Apply sparse review, independent judge, and local validation."""
        request, state = selected.request, selected.attempt
        implementation = state.implementation
        if implementation is None:
            raise MultiSessionError.missing_implementation()
        due = review_due(
            round_number=request.round_number,
            max_rounds=self.options.max_rounds,
            judge_every=self.options.judge_every,
            outcome=implementation.hypothesis_outcome,
            candidate_evidence_fresh=candidate_evidence_is_fresh(implementation, request.records),
        )
        if state.review_started and not implementation_requests_continuation(implementation):
            due = True
        if (
            state.review_started
            and request.round_number != self.options.max_rounds
            and implementation_requests_continuation(implementation)
            and implementation.candidate_disposition is not CandidateDisposition.PARETO_FRONTIER
            and not state.revalidation_required
        ):
            due = False
        if not due:
            state.judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
            issue_board.append_judge_skipped(
                self.turns.progress_path,
                request.round_number,
                outcome=implementation.hypothesis_outcome.value,
                judge_every=self.options.judge_every,
            )
            self.ctx.log("[judge] deferred by sparse-review policy; official gates were not run")
            return AttemptDecision.FINISH
        state.review_started = True
        state.revalidation_required = False
        conflict = _pareto_archive_conflict(
            candidate_disposition=implementation.candidate_disposition,
            candidate_metrics=dict(implementation.candidate_metrics),
            records=request.records,
            space=state.agent_run_state.metrics,
        )
        verdict = await self.turns.review(request, state, conflict)
        state.judge = JudgeReviewed(verdict.verdict)
        if verdict.verdict is not Verdict.PASS:
            state.feedback = verdict.feedback
            request.active_hypothesis.feedback = verdict.feedback
            await self._checkpoint_active(selected)
            return AttemptDecision.RETRY
        validation_feedback = await self._validate_local(
            selected, implementation.validation_recipe_artifact
        )
        if validation_feedback is not None:
            state.feedback = validation_feedback
            request.active_hypothesis.feedback = validation_feedback
            await self._checkpoint_active(selected)
            return AttemptDecision.RETRY
        await self._approve_candidate(selected)
        candidate_ready = (
            implementation.hypothesis_outcome
            in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
            or implementation.candidate_disposition is CandidateDisposition.PARETO_FRONTIER
        )
        reason = official_evaluation_reason(
            records=request.records,
            round_number=request.round_number,
            max_rounds=self.options.max_rounds,
            official_eval_every=self.options.official_eval_every,
            requested=request.plan.request_official_evaluation,
            candidate_ready=candidate_ready,
        )
        if reason is None:
            if candidate_ready:
                self._record_official_decision(selected, run=False, reason="cadence_not_due")
                self.ctx.log("[official-evaluation] deferred; candidate retained as provisional")
            state.passed = True
            return AttemptDecision.FINISH
        await self._approve_perf(selected)
        state.official_reason = reason
        return AttemptDecision.OFFICIAL

    async def _approve_candidate(self, selected: MultiRound) -> None:
        implementation = selected.attempt.implementation
        if (
            implementation is None
            or implementation.candidate_disposition is not CandidateDisposition.PARETO_FRONTIER
        ):
            return
        hypothesis = selected.request.active_hypothesis
        hypothesis.gate_approved_candidate_disposition = implementation.candidate_disposition.value
        hypothesis.gate_approved_candidate_metrics = dict(implementation.candidate_metrics)
        hypothesis.gate_approved_candidate_evaluation_artifact = (
            implementation.candidate_evaluation_artifact
        )
        hypothesis.gate_approved_candidate_operating_point = (
            implementation.candidate_operating_point
        )
        hypothesis.gate_approved_candidate_retention_reason = (
            implementation.candidate_retention_reason
        )
        await self._checkpoint_active(selected)

    async def _validate_local(  # noqa: C901  # bounded framework recipe gate
        self, selected: MultiRound, recipe_artifact: str | None
    ) -> str | None:
        """Run judge approved local recipes against an immutable candidate revision."""
        if recipe_artifact is None:
            return None
        number = self.round_number
        retry = selected.attempt.retry
        try:
            recipes = _load_validation_recipes(self.workspace.path, recipe_artifact)
        except ValueError as error:
            return f"Framework local validation recipe error: {error}."
        names = [recipe.name for recipe in recipes]
        if len(names) != len(set(names)):
            return "Framework local validation recipes contain duplicate names."
        revision = await self.workspace.snapshot(
            f"round-{number}-retry-{retry}-framework-validation-input"
        )
        results: list[FrameworkValidationResult] = []
        restore_required = False
        for recipe in recipes:
            try:
                digest = _validation_input_digest(self.workspace.path, recipe)
            except (OSError, ValueError) as error:
                results.append(
                    FrameworkValidationResult(
                        recipe=recipe, input_digest="", passed=False, error=str(error)
                    )
                )
                break
            reused = _reusable_validation_result(self.turns.progress_path, recipe, digest)
            emit_gate_started(
                GateKind.VALIDATION,
                recipe=recipe.name,
                command=recipe.command,
                round_label=f"round-{number}",
            )
            if reused is not None:
                results.append(reused)
                emit_gate_finished(
                    GateKind.VALIDATION,
                    passed=True,
                    recipe=recipe.name,
                    reused=True,
                    round_label=f"round-{number}",
                )
                continue
            try:
                execution = await self.ctx.environment.execute(
                    recipe.command, timeout_seconds=recipe.timeout_seconds
                )
                output = execution.output.strip()
                result = FrameworkValidationResult(
                    recipe=recipe,
                    input_digest=digest,
                    passed=execution.exit_code == 0,
                    exit_code=execution.exit_code,
                    output=output[-GATE_RECORD_TAIL_CHARS:],
                    error=None if execution.exit_code == 0 else "command exited nonzero",
                )
            except Exception as error:  # noqa: BLE001  # report execution failure as gate feedback
                result = FrameworkValidationResult(
                    recipe=recipe,
                    input_digest=digest,
                    passed=False,
                    error=f"command could not be executed: {error}",
                )
            changes = await self.workspace.pending_changes()
            if changes:
                restore_required = True
                result = result.model_copy(
                    update={
                        "passed": False,
                        "error": f"validation command mutated the workspace: {', '.join(changes[:8])}",
                    }
                )
            results.append(result)
            failure = (
                None if result.passed else (result.error or result.output or "unknown failure")
            )
            emit_gate_finished(
                GateKind.VALIDATION,
                passed=result.passed,
                recipe=recipe.name,
                output_tail=None if failure is None else failure[-GATE_LOG_TAIL_CHARS:],
                round_label=f"round-{number}",
            )
            if not result.passed:
                break
        if restore_required:
            await self.workspace.restore(revision, clean=True)
        artifact = issue_board.write_validation_result_artifact(
            self.turns.progress_path, number, retry, results
        )
        location = issue_board.display_path(artifact, self.workspace.path)
        issue_board.append_framework_validation_gate(
            self.turns.progress_path,
            number,
            retry,
            artifact=location,
            results=results,
        )
        await self.workspace.snapshot(f"round-{number}-retry-{retry}-framework-validation")
        failed = next((result for result in results if not result.passed), None)
        if failed is None:
            return None
        detail = failed.error or failed.output or "unknown failure"
        return f"Framework local validation failed for {failed.recipe.name!r}: {detail}. Inspect `{location}` and repair only the affected local contract."

    async def _approve_perf(self, selected: MultiRound) -> None:
        implementation = selected.attempt.implementation
        if implementation is None or implementation.perf_metric is None:
            return
        hypothesis = selected.request.active_hypothesis
        hypothesis.gate_approved_perf_metric = implementation.perf_metric
        hypothesis.gate_approved_perf_unit = implementation.perf_unit
        hypothesis.gate_approved_metrics = dict(implementation.metrics)
        hypothesis.gate_approved_evaluation_artifact = implementation.evaluation_artifact
        await self._checkpoint_active(selected)

    async def official_gates(self, selected: MultiRound) -> bool:
        """Evaluate a candidate only after independent review passes."""
        return await self._official_gates(selected)

    async def _checkpoint_active(self, selected: MultiRound) -> None:
        state = update_active_hypothesis(
            selected.attempt.agent_run_state, selected.request.active_hypothesis
        )
        selected.attempt.agent_run_state = state
        await self._save_state(
            state, label=f"multi: checkpoint hypothesis {selected.request.plan.hypothesis_id}"
        )

    def _record_official_decision(self, selected: MultiRound, *, run: bool, reason: str) -> None:
        issue_board.append_official_evaluation_decision(
            self.turns.progress_path,
            self.round_number,
            selected.attempt.retry,
            run=run,
            reason=reason,
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=_provisional_candidates_since_official(self.records),
        )

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

    async def _official_gates(self, selected: MultiRound) -> bool:
        attempt = selected.attempt
        reason = attempt.official_reason
        if reason is None:
            raise MultiSessionError.missing_gate_reason()
        self._record_official_decision(selected, run=True, reason=reason)
        commit = self.workspace.revision
        hypothesis = selected.request.active_hypothesis
        reuse_accuracy = bool(
            hypothesis.gate_revalidation_pending
            and commit is not None
            and hypothesis.gate_candidate_commit == commit
            and hypothesis.gate_accuracy_passed
        )
        feedback, benchmark, accuracy_passed = await self._run_gates(
            attempt.retry, commit, reuse_accuracy=reuse_accuracy
        )
        attempt.framework_benchmark = benchmark
        attempt.framework_perf_metric = benchmark.metric_value
        if feedback is None:
            attempt.passed = True
            return True
        attempt.feedback = feedback
        attempt.revalidation_required = True
        hypothesis.gate_revalidation_pending = True
        hypothesis.gate_candidate_commit = commit
        hypothesis.gate_accuracy_passed = accuracy_passed
        hypothesis.feedback = feedback
        await self._checkpoint_active(selected)
        return False

    async def _run_gates(
        self, retry: int, commit: str | None, *, reuse_accuracy: bool
    ) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
        if self.turns.worker.backend_name == "stub":
            return None, FrameworkBenchmarkOutcome(), False
        resource_feedback = await self.ctx.environment.reconcile_model_requests()
        if resource_feedback is not None:
            return resource_feedback, FrameworkBenchmarkOutcome(), False
        accuracy = await self._accuracy_gate(retry, commit, reuse=reuse_accuracy)
        if accuracy.feedback is not None:
            return accuracy.feedback, FrameworkBenchmarkOutcome(), False
        benchmark = await self._benchmark_gate(retry, commit)
        return benchmark.outcome.feedback, benchmark.outcome, True

    async def _accuracy_gate(
        self, retry: int, commit: str | None, *, reuse: bool
    ) -> AccuracyGateResult:
        number = self.round_number
        view = self.ctx.environment.view
        command = view.paths.accuracy_command
        if reuse:
            issue_board.append_framework_accuracy_gate(
                self.turns.progress_path,
                number,
                retry,
                command=command or "(not configured)",
                passed=True,
                output=(
                    "Reused the prior framework-owned PASS for this exact candidate commit; "
                    "a later gate, not accuracy, caused the retry."
                ),
            )
            return await self.ctx.evaluator.reuse_accuracy(label=f"round-{number}")
        release = (
            self.ctx.request.input_bundle.benchmark_result is None
            and self.ctx.request.input_bundle.benchmark_result_protocol is None
        ) or not view.paths.benchmark_command
        execution = self._command(
            command,
            commit,
            view.deployment_release_env_var if release else None,
        )
        result = await self.ctx.evaluator.check(
            f"accuracy-{number}-{retry}",
            label=f"round-{number}",
            execution_command=execution,
        )
        if result.passed and not result.executed:
            return result
        issue_board.append_framework_accuracy_gate(
            self.turns.progress_path,
            number,
            retry,
            command=result.command or "(not configured)",
            passed=result.passed,
            output=result.output[-GATE_RECORD_TAIL_CHARS:],
        )
        await self.workspace.snapshot(f"round-{number}-retry-{retry}-framework-accuracy")
        return result

    async def _benchmark_gate(self, retry: int, commit: str | None) -> BenchmarkGateResult:
        number = self.round_number
        view = self.ctx.environment.view
        execution = self._command(
            view.paths.benchmark_command, commit, view.deployment_release_env_var
        )
        result = await self.ctx.evaluator.measure(
            f"{number}-{retry}",
            options=MeasurementOptions(
                objectives=tuple(self.state.metrics.objectives),
                label=f"round-{number}",
                execution_base=execution,
            ),
        )
        if not result.executed:
            return result
        spec = self.ctx.request.input_bundle.benchmark_result
        issue_board.append_framework_benchmark(
            self.turns.progress_path,
            number,
            retry,
            command=result.command or "(not configured)",
            passed=result.passed,
            metric_name=result.outcome.metric_name or (spec.metric if spec else None),
            metric_value=result.outcome.metric_value,
            output=result.output[-GATE_RECORD_TAIL_CHARS:],
        )
        await self.workspace.snapshot(f"round-{number}-retry-{retry}-framework-benchmark")
        return result

    async def commit_round(self, selected: MultiRound) -> None:
        """Commit the completed round and publish its observable record."""
        attempt = selected.attempt
        projection = self.terminal_policy.project_performance(selected.request, attempt)
        candidate_commit = await self.workspace.snapshot(f"round-{self.round_number}-record-input")
        worker = self.turns.worker
        record = build_round_record(
            RecordInput(
                state=attempt.agent_run_state,
                records=self.records,
                round_number=self.round_number,
                hypothesis=selected.selection.hypothesis,
                plan=selected.selection.plan,
                attempt=attempt,
                projection=projection,
                reviewed=self.terminal_policy.reviewed(attempt),
                framework_benchmark_configured=self.framework_benchmark_configured,
                accuracy_configured=bool(self.ctx.environment.view.paths.accuracy_command),
                candidate_commit=candidate_commit,
                backend_name=worker.backend_name,
                driver_name=worker.driver_name,
                provider=worker.provider,
                model=worker.model,
            )
        )
        terminal = transition_round(
            self.terminal_policy,
            TerminalRequest(
                engine=self.engine,
                state=attempt.agent_run_state,
                hypothesis=selected.selection.hypothesis,
                attempt=attempt,
                record=record,
                records=self.records,
                carry=self.carry,
                reviewed=self.terminal_policy.reviewed(attempt),
                max_retries_per_round=self.options.max_retries_per_round,
            ),
        )
        if terminal.exhaustion_feedback is not None:
            issue_board.append_exhaustion_note(
                self.turns.progress_path,
                self.round_number,
                self.options.max_retries_per_round,
                terminal.exhaustion_feedback,
            )
        await self.ctx.state.checkpoint(
            sequence=self.round_number,
            writes={"state.json": terminal.state},
            publish=terminal.state,
        )
        self.engine = terminal.engine
        self.state = terminal.state
        self.records = terminal.state.rounds
        self.history = RoundHistory(records=self.records)
        self.carry = terminal.carry
        self.round_number += 1
        self._announce_experiments("round_persisted", terminal.state)
        self.ctx.events.emit(
            CoreEventType.ROUND_FINISHED,
            status=EventStatus.COMPLETED
            if attempt.passed or not record.reviewed
            else EventStatus.FAILED,
            round_label=f"round-{record.round_number}",
            data=RoundFinishedData(
                attempts=attempt.retry,
                judge_verdict="pass"
                if attempt.passed
                else "fail"
                if record.reviewed
                else "skipped",
                perf_metric=projection.metric,
                perf_unit=projection.unit,
                profile_skipped=projection.profile_skipped,
            ),
        )

    def _announce_experiments(self, reason: ExperimentsChangeReason, state: AgentRunState) -> None:
        self.ctx.events.emit(
            CoreEventType.EXPERIMENTS_CHANGED,
            data=ExperimentsChangedData(reason=reason, revision=state.experiment_revision),
        )

    def _memory_paths(self) -> tuple[str, ...]:
        root = self.workspace.path
        return tuple(
            str(path.relative_to(root)) for path in issue_board.framework_memory_paths(root)
        )

    async def finish(self) -> bool:
        """Restore the best trusted candidate or the trusted input baseline."""
        self.ctx.log(f"Reached max_rounds={self.options.max_rounds}. Stopping.")
        issue_board.write_pareto_archive(
            self.turns.progress_path, _pareto_archive_summary(self.records, self.state.metrics)
        )
        if self.state.metrics.objectives:
            frontier = _pareto_frontier_records(self.records, self.state.metrics)
            self.ctx.log(f"\nFinal Pareto frontier ({len(frontier)} rounds):")
            for record in frontier:
                self.ctx.log(
                    f"  round {record.round_number}: "
                    f"{_format_metric_row(_record_candidate_metrics(record), self.state.metrics.objectives)} "
                    f"(commit {(record.commit or 'n/a')[:12]})"
                )
        winner = _select_final_candidate(self.records, self.state.metrics)
        if winner is None:
            baseline = self.workspace.trusted_input_baseline
            if baseline is None:
                raise MultiSessionError.missing_baseline()
            await self.workspace.restore(baseline, clean=True, preserve_paths=self._memory_paths())
            await self.workspace.snapshot("multi: restore trusted input baseline")
            self.ctx.log(f"\nNo evaluated winner was retained. Restored baseline {baseline[:12]}.")
            return True
        if winner.commit is None:
            raise MultiSessionError.missing_winner_commit()
        await self.workspace.retain(f"selected-round-{winner.round_number:04d}", winner.commit)
        await self.workspace.restore(winner.commit, clean=True, preserve_paths=self._memory_paths())
        await self.workspace.snapshot(f"multi: select round {winner.round_number}")
        metrics = (
            _format_metric_row(_record_candidate_metrics(winner), self.state.metrics.objectives)
            if self.state.metrics.objectives
            else f"{winner.perf_metric:.6g} {winner.perf_unit or ''}"
        )
        self.ctx.log(
            f"\nFinal selected candidate: round {winner.round_number}, "
            f"commit {winner.commit[:12]}, official metrics: {metrics.strip()}"
        )
        return True

    async def close(self) -> None:
        """Release clients before the host releases their sandboxes."""
        await self.turns.close()
