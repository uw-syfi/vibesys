"""Durable round control for the profile guided multi strategy over public host capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.agent_run import issue_board
from vibesys.errors import StrategySessionError
from vibesys.evaluators.gates import (
    GATE_LOG_TAIL_CHARS,
    GATE_RECORD_TAIL_CHARS,
    emit_gate_finished,
    emit_gate_started,
)
from vibesys.evaluators.validation_recipe import FrameworkValidationResult
from vibesys.events import GateKind
from vibesys.loops.profile_multi.attribution import run_attribution
from vibesys.loops.profile_multi.decisions import AttemptRequest, PlanRequest
from vibesys.loops.profile_multi.turns import ProfileMultiTurns
from vibesys.loops.profile_multi.validation import (
    _load_validation_recipes,
    _reusable_validation_result,
    _validation_input_digest,
)
from vibesys.orchestration.runtime import WorkspaceRestoreError
from vibesys.roles.common import Verdict
from vibesys.roles.profiler import ProfilerSummary  # noqa: TC001  # tracked: #288
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
)
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.search.hypothesis import cadence as hypothesis_cadence
from vibesys.search.hypothesis.attempts import (
    AttemptDecision,
    AttemptState,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    PerformanceProjection,
    attempt_was_reviewed,
)
from vibesys.search.hypothesis.record import RecordInput, build_round_record
from vibesys.search.hypothesis.results import Continue, Finished, NewHypothesis
from vibesys.search.hypothesis.state import HypothesisState
from vibesys.search.hypothesis.transitions import (
    FAILED_HYPOTHESIS_OUTCOMES,
    CarryOver,
    _format_metric_row,
    adopt_metric_space,
    pareto_archive_summary,
    provisional_candidates_since_official,
    record_candidate_metrics,
    terminal_workspace_notice,
    update_active_hypothesis,
)
from vibesys.search.profile_focus import ProfileFocus, ProfileFocusConfig
from vs_agent.api import RoundProgress
from vs_loop_state.api import RoundHistory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.evaluators.input_manifest import ProfileGuidedInput
    from vibesys.loops.agent_options import AgentOrchestrationOptions
    from vibesys.orchestration.runtime import RunContext
    from vibesys.search.hypothesis.state import Hypothesis, RoundRecord
    from vibesys.search.profile_focus import FocusView, ProfileFocusState


ProfileMultiSessionError = StrategySessionError


@dataclass
class ProfileMultiRound:
    """One selected hypothesis's turn facts and the mutable attempt state."""

    request: AttemptRequest
    attempt: AttemptState


class _TerminalPolicy:
    """Profile guided multi role policy facts consumed by the pure round transition."""

    def __init__(self, config: HypothesisConfig) -> None:
        """Bind the continuation-lease budget this policy enforces."""
        self.config = config

    def project_performance(
        self, hypothesis: Hypothesis, state: AttemptState
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
            and hypothesis.gate_revalidation_pending
        ):
            implementation_metric = hypothesis.gate_approved_perf_metric
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
        implementation = state.implementation
        return hypothesis_cadence.keeps_hypothesis_active(
            outcome=implementation.hypothesis_outcome if implementation is not None else None,
            next_step=implementation.next_step if implementation is not None else "",
            continuation_rounds=continuation_rounds,
            max_continuation_rounds=self.config.max_continuation_rounds,
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


class _ProfilePolicy:
    """Component measurement and advancement decisions for this strategy."""

    def __init__(self, config: ProfileGuidedInput) -> None:
        """Bind the manifest configuring this run's profile attribution."""
        self.config = config

    def official_reason(self, reason: str | None, focus: FocusView) -> str | None:
        """Measure an active component even when the regular cadence defers."""
        if focus.active_component:
            return reason or "profile-guided component measurement"
        return reason


class ProfileMultiSession:
    """Own the profile multi strategy's state, roles, attempts, and checkpoints."""

    def __init__(self, ctx: RunContext, options: AgentOrchestrationOptions) -> None:
        """Bind one host; resource opening occurs in ``open``."""
        self.ctx = ctx
        self.options = options
        self.workspace = ctx.workspaces.root
        self.turns = ProfileMultiTurns(ctx, options)
        self._gate_recorder = issue_board.GateBoardRecorder(self.turns.progress_path)
        self.search = HypothesisSearch(
            HypothesisConfig(
                max_rounds=options.max_rounds,
                judge_every=options.judge_every,
                official_eval_every=options.official_eval_every,
                max_retries_per_round=options.max_retries_per_round,
            )
        )
        self.terminal_policy = _TerminalPolicy(self.search.config)
        config = options.profile_guided
        if config is None:
            raise ProfileMultiSessionError.missing_profile_config(  # noqa: TRY003  # tracked: #288
                "profile_multi requires profile_guided settings"
            )
        self.profile = _ProfilePolicy(config)
        self.focus = ProfileFocus(
            ProfileFocusConfig(
                plateau_min_rounds=config.min_measured_rounds,
                min_relative_improvement=config.min_relative_improvement,
            )
        )
        self.framework_benchmark_configured = (
            ctx.request.input_bundle.benchmark_result is not None
            or ctx.request.input_bundle.benchmark_result_protocol is not None
        )

    @classmethod
    async def open(cls, ctx: RunContext, options: AgentOrchestrationOptions) -> ProfileMultiSession:
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
        previous = await ctx.state.load(HypothesisState)
        state = adopt_metric_space(previous or self.search.initial(), self.options.metric_space)
        self.state = state
        self.carry = CarryOver(regression_info=terminal_workspace_notice(self.records))
        self.round_number = len(self.records) + 1
        self.last_profile_focus = "general latency hotspots on /v1/completions"
        if previous != state:
            await self.ctx.state.commit(
                sequence=self.round_number,
                writes={"state.json": state},
                candidate=False,
                label="profile_multi: initialize policy state",
                publish=state,
            )

    @property
    def records(self) -> list[RoundRecord]:
        """The active state's completed rounds, in recorded order."""
        return self.state.rounds

    @property
    def has_next_round(self) -> bool:
        """Whether the durable cursor remains within the total round budget."""
        return self.round_number <= self.options.max_rounds

    def _focus_state(self) -> ProfileFocusState:
        return self.state.profile_guidance or self.focus.initial()

    @asynccontextmanager
    async def round_scope(self) -> AsyncIterator[None]:
        """Attribute one round's turns and release progress on all exits."""
        number = self.round_number
        self.ctx.switch_log(f"round{number:03d}")
        issue_board.write_pareto_archive(
            self.turns.progress_path, pareto_archive_summary(self.records, self.state.metrics)
        )
        progress = RoundProgress(number, self.options.max_rounds)
        self.ctx.log(f"\n{'=' * 60}\n  {progress.label()}\n{'=' * 60}\n")
        with self.ctx.agents.progress(progress):
            yield

    async def select_hypothesis(self) -> ProfileMultiRound:
        """Choose a designer plan or continue the active hypothesis."""
        number = self.round_number
        decision = self.search.next_round(
            self.state, round_number=number, records=self.records, carry=self.carry
        )
        if isinstance(decision, Finished):
            raise ProfileMultiSessionError.missing_active()
        if isinstance(decision, NewHypothesis):
            context = decision.context
            bottlenecks = await run_attribution(self.ctx, self.profile.config, round_number=number)
            focus_state = self.focus.observe(
                self._focus_state(), round_number=number, bottlenecks=bottlenecks
            )
            self.state = self.state.model_copy(update={"profile_guidance": focus_state})
            await self.ctx.state.commit(
                sequence=self.round_number,
                writes={"state.json": self.state},
                candidate=False,
                label=f"profile-guided: prepare round {number}",
                publish=self.state,
            )
            summary = await self._pre_round_profile()
            plan = await self.turns.plan(
                PlanRequest(
                    round_number=context.round_number,
                    state=self.state,
                    records=list(context.records),
                    carry=context.carry,
                    profiler_summary=summary,
                    plateau_warning=context.plateau_warning,
                    provisional_candidates=context.provisional_candidates,
                    profile_guidance=self.focus.focus(focus_state),
                )
            )
            started = self.search.start(
                self.state,
                plan,
                round_number=number,
                current_commit=self.workspace.revision,
                records=self.records,
            )
            self.state = started.state
            hypothesis = started.hypothesis
            await self.ctx.state.commit(
                sequence=self.round_number,
                writes={"state.json": self.state},
                candidate=False,
                label=f"profile_multi: start hypothesis {plan.hypothesis_id}",
                publish=self.state,
            )
        else:
            assert isinstance(decision, Continue)  # noqa: S101  # only remaining variant
            hypothesis = decision.hypothesis
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
        reason = self.search.official_due(
            records=self.records,
            round_number=number,
            requested=plan.request_official_evaluation,
            candidate_ready=True,
        )
        reason = self.profile.official_reason(reason, self.focus.focus(self._focus_state()))
        await self._apply_rollback(hypothesis)
        request = AttemptRequest(
            round_number=number,
            plan=plan,
            planned_official_reason=reason,
            records=self.records,
            active_hypothesis=hypothesis,
            profile_focus=self.focus.focus(self._focus_state()),
            last_profile_focus=self.last_profile_focus,
        )
        attempt = AttemptState(
            agent_run_state=self.state,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return ProfileMultiRound(request=request, attempt=attempt)

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

    async def _apply_rollback(self, hypothesis: Hypothesis) -> None:
        parent_round = hypothesis.plan.revert_to_round
        if parent_round is None or hypothesis.revert_applied:
            return
        target = next(
            (record for record in self.records if record.round_number == parent_round), None
        )
        if target is None or not target.commit:
            self.ctx.log(f"cannot revert: no commit recorded for round {parent_round}")
            return
        rollback, failed_child = RoundHistory(records=self.records).resolve_rollback_commit(
            target, FAILED_HYPOTHESIS_OUTCOMES
        )
        if rollback is None:
            raise ProfileMultiSessionError.missing_rollback()
        try:
            async with self.workspace.transaction() as tx:
                await self.workspace.restore(rollback, clean=True)
                tx.commit()
        except WorkspaceRestoreError:
            self.ctx.warning(
                f"could not check out rollback revision {rollback[:8]} for round "
                f"{parent_round}; will retry the rollback next round"
            )
            return
        hypothesis.revert_applied = True
        hypothesis.revert_commit = rollback
        hypothesis.parent_commit = rollback
        self.state = update_active_hypothesis(self.state, hypothesis)
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": self.state},
            candidate=False,
            label=f"profile_multi: set hypothesis {hypothesis.hypothesis_id} parent",
            publish=self.state,
        )
        if failed_child is None:
            self.ctx.log(f"Reverted workspace to round {parent_round} ({rollback[:8]}).")
        else:
            self.ctx.log(
                f"Reverted workspace to the pre-hypothesis parent of failed round "
                f"{failed_child} ({rollback[:8]}), based on parent round {parent_round}."
            )

    def remaining_attempts(self, selected: ProfileMultiRound) -> range:
        """Resume after the last durable paid attempt marker."""
        first = issue_board.next_implementer_attempt(
            self.turns.progress_path, selected.request.round_number
        )
        limit = self.options.max_retries_per_round
        if first > limit:
            raise ProfileMultiSessionError.exhausted_attempts(self.round_number, first, limit)
        if first > 1:
            self.ctx.log(f"[resume] round {self.round_number} continues at attempt {first}/{limit}")
        return range(first, limit + 1)

    async def begin_attempt(self, selected: ProfileMultiRound, retry: int) -> None:
        """Prepare attempt state and device before the paid implementer turn."""
        self.ctx.log(f"\n--- attempt {retry}/{self.options.max_retries_per_round} ---\n")
        attempt = selected.attempt
        attempt.retry = retry
        attempt.official_reason = None
        attempt.judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": attempt.agent_run_state},
            candidate=False,
            label=f"profile_multi: start round {self.round_number} attempt {retry}",
            publish=attempt.agent_run_state,
        )
        await self.ctx.environment.reselect_device()

    async def implement(self, selected: ProfileMultiRound) -> bool:
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

    async def review(self, selected: ProfileMultiRound) -> AttemptDecision:
        """Apply sparse review, independent judge, and local validation."""
        state = selected.attempt
        implementation = state.implementation
        if implementation is None:
            raise ProfileMultiSessionError.missing_implementation()
        due = self.search.review_due(
            round_number=selected.request.round_number,
            outcome=implementation.hypothesis_outcome,
            candidate_evidence_is_fresh=hypothesis_cadence.candidate_evidence_fresh(
                candidate_metrics=implementation.candidate_metrics,
                candidate_evaluation_artifact=implementation.candidate_evaluation_artifact,
                records=self.records,
            ),
            review_started=state.review_started,
            requests_continuation=hypothesis_cadence.continuation_requested(
                outcome=implementation.hypothesis_outcome, next_step=implementation.next_step
            ),
            pareto_frontier_claim=implementation.candidate_disposition
            is CandidateDisposition.PARETO_FRONTIER,
            revalidation_required=state.revalidation_required,
        )
        if not due:
            state.judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
            issue_board.append_judge_skipped(
                self.turns.progress_path,
                selected.request.round_number,
                outcome=implementation.hypothesis_outcome.value,
                judge_every=self.options.judge_every,
            )
            self.ctx.log("[judge] deferred by sparse-review policy; official gates were not run")
            return AttemptDecision.FINISH
        state.review_started = True
        state.revalidation_required = False
        conflict = self.search.pareto_conflict(
            disposition=implementation.candidate_disposition,
            metrics=dict(implementation.candidate_metrics),
            records=self.records,
            space=state.agent_run_state.metrics,
        )
        verdict = await self.turns.review(selected.request, selected.attempt, conflict)
        state.judge = JudgeReviewed(verdict.verdict.value)
        if verdict.verdict is not Verdict.PASS:
            state.feedback = verdict.feedback
            selected.request.active_hypothesis.feedback = verdict.feedback
            await self._checkpoint_hypothesis(selected)
            return AttemptDecision.RETRY
        validation_feedback = await self._validate_local(
            selected, implementation.validation_recipe_artifact
        )
        if validation_feedback is not None:
            state.feedback = validation_feedback
            selected.request.active_hypothesis.feedback = validation_feedback
            await self._checkpoint_hypothesis(selected)
            return AttemptDecision.RETRY
        await self._approve_candidate(selected)
        candidate_ready = (
            implementation.hypothesis_outcome
            in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
            or implementation.candidate_disposition is CandidateDisposition.PARETO_FRONTIER
        )
        reason = self.search.official_due(
            records=self.records,
            round_number=selected.request.round_number,
            requested=selected.request.plan.request_official_evaluation,
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

    async def _checkpoint_hypothesis(self, selected: ProfileMultiRound) -> None:
        state = update_active_hypothesis(
            selected.attempt.agent_run_state, selected.request.active_hypothesis
        )
        selected.attempt.agent_run_state = state
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": state},
            candidate=False,
            label=f"profile_multi: checkpoint hypothesis {selected.request.plan.hypothesis_id}",
            publish=state,
        )

    async def _approve_candidate(self, selected: ProfileMultiRound) -> None:
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
        await self._checkpoint_hypothesis(selected)

    async def _validate_local(  # noqa: C901  # bounded framework recipe gate
        self, selected: ProfileMultiRound, recipe_artifact: str | None
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
        results: list[FrameworkValidationResult] = []
        restore_required = False
        async with self.workspace.transaction(
            label=f"round-{number}-retry-{retry}-framework-validation-input"
        ) as tx:
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
            if not restore_required:
                tx.commit()
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

    async def _approve_perf(self, selected: ProfileMultiRound) -> None:
        implementation = selected.attempt.implementation
        if implementation is None or implementation.perf_metric is None:
            return
        hypothesis = selected.request.active_hypothesis
        hypothesis.gate_approved_perf_metric = implementation.perf_metric
        hypothesis.gate_approved_perf_unit = implementation.perf_unit
        hypothesis.gate_approved_metrics = dict(implementation.metrics)
        hypothesis.gate_approved_evaluation_artifact = implementation.evaluation_artifact
        await self._checkpoint_hypothesis(selected)

    async def official_gates(self, selected: ProfileMultiRound) -> bool:
        """Evaluate a candidate only after independent review passes."""
        return await self._official_gates(selected)

    def _record_official_decision(
        self, selected: ProfileMultiRound, *, run: bool, reason: str
    ) -> None:
        issue_board.append_official_evaluation_decision(
            self.turns.progress_path,
            self.round_number,
            selected.attempt.retry,
            run=run,
            reason=reason,
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=provisional_candidates_since_official(self.records),
        )

    async def _official_gates(self, selected: ProfileMultiRound) -> bool:
        attempt = selected.attempt
        reason = attempt.official_reason
        if reason is None:
            raise ProfileMultiSessionError.missing_gate_reason()
        self._record_official_decision(selected, run=True, reason=reason)
        commit = self.workspace.revision
        hypothesis = selected.request.active_hypothesis
        reuse_accuracy = bool(
            hypothesis.gate_revalidation_pending
            and commit is not None
            and hypothesis.gate_candidate_commit == commit
            and hypothesis.gate_accuracy_passed
        )
        result = await self.ctx.gates.run(
            round_number=self.round_number,
            retry=attempt.retry,
            commit=commit,
            objectives=self.state.metrics.objectives,
            record=self._gate_recorder,
            reuse_accuracy=reuse_accuracy,
            agent_backend_name=self.turns.worker.backend_name,
        )
        attempt.framework_benchmark = result.benchmark
        attempt.framework_perf_metric = result.benchmark.metric_value
        if result.feedback is None:
            attempt.passed = True
            return True
        attempt.feedback = result.feedback
        attempt.revalidation_required = True
        hypothesis.gate_revalidation_pending = True
        hypothesis.gate_candidate_commit = commit
        hypothesis.gate_accuracy_passed = result.accuracy_passed
        hypothesis.feedback = result.feedback
        await self._checkpoint_hypothesis(selected)
        return False

    async def commit_round(self, selected: ProfileMultiRound) -> None:
        """Commit the completed round and publish its observable record."""
        attempt = selected.attempt
        hypothesis = selected.request.active_hypothesis
        implementation = attempt.implementation
        projection = self.terminal_policy.project_performance(hypothesis, attempt)
        candidate_commit = await self.workspace.snapshot(f"round-{self.round_number}-record-input")
        worker = self.turns.worker
        record = build_round_record(
            RecordInput(
                state=attempt.agent_run_state,
                records=self.records,
                round_number=self.round_number,
                hypothesis=hypothesis,
                plan=selected.request.plan,
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
        keeps_active = self.terminal_policy.keeps_hypothesis_active(
            attempt, hypothesis.continuation_rounds
        )
        reviewed = self.terminal_policy.reviewed(attempt)
        next_step = implementation.next_step if implementation is not None else None
        requests_continuation = hypothesis_cadence.continuation_requested(
            outcome=implementation.hypothesis_outcome if implementation is not None else None,
            next_step=next_step or "",
        )
        terminal_needs_parent_choice = self.terminal_policy.terminal_success_needs_parent_choice(
            attempt, hypothesis.continuation_rounds
        )
        closed = self.search.close_round(
            self.state,
            hypothesis=hypothesis,
            record=record,
            records=self.records,
            carry=self.carry,
            passed=attempt.passed,
            reviewed=reviewed,
            feedback=attempt.feedback,
            keeps_active=keeps_active,
            requests_continuation=requests_continuation,
            next_step=next_step,
            terminal_needs_parent_choice=terminal_needs_parent_choice,
            has_implementation=implementation is not None,
        )
        focus_state = self.focus.record(
            self._focus_state(),
            round_number=self.round_number,
            passed=attempt.passed and record.official_evaluation,
            relative_improvement=(
                record.perf_delta_pct / 100 if record.perf_delta_pct is not None else None
            ),
        )
        state = closed.state.model_copy(update={"profile_guidance": focus_state})
        if closed.exhaustion_feedback is not None:
            issue_board.append_exhaustion_note(
                self.turns.progress_path,
                self.round_number,
                self.options.max_retries_per_round,
                closed.exhaustion_feedback,
            )
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": state},
            publish=state,
        )
        self.state = state
        self.carry = closed.carry
        self.round_number += 1

    async def finish(self) -> bool:
        """Restore the best trusted candidate or the trusted input baseline."""
        self.ctx.log(f"Reached max_rounds={self.options.max_rounds}. Stopping.")
        issue_board.write_pareto_archive(
            self.turns.progress_path, pareto_archive_summary(self.records, self.state.metrics)
        )
        if self.state.metrics.objectives:
            frontier = self.search.frontier(self.records, space=self.state.metrics)
            self.ctx.log(f"\nFinal Pareto frontier ({len(frontier)} rounds):")
            for record in frontier:
                self.ctx.log(
                    f"  round {record.round_number}: "
                    f"{_format_metric_row(record_candidate_metrics(record), self.state.metrics.objectives)} "
                    f"(commit {(record.commit or 'n/a')[:12]})"
                )
        winner = self.search.best(self.records, space=self.state.metrics)
        if winner is None:
            baseline = self.workspace.trusted_input_baseline
            if baseline is None:
                raise ProfileMultiSessionError.missing_baseline()
            await self.workspace.restore(baseline, clean=True)
            await self.workspace.snapshot("profile_multi: restore trusted input baseline")
            self.ctx.log(f"\nNo evaluated winner was retained. Restored baseline {baseline[:12]}.")
            return True
        if winner.commit is None:
            raise ProfileMultiSessionError.missing_winner_commit()
        await self.workspace.retain(f"selected-round-{winner.round_number:04d}", winner.commit)
        await self.workspace.restore(winner.commit, clean=True)
        await self.workspace.snapshot(f"profile_multi: select round {winner.round_number}")
        metrics = (
            _format_metric_row(record_candidate_metrics(winner), self.state.metrics.objectives)
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
