"""Multi-agent hypothesis search over explicit runtime capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.hypothesis import (
    AttemptDecision,
    AttemptState,
    Continue,
    Finished,
    HypothesisConfig,
    HypothesisSearch,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    NewHypothesis,
    PerformanceProjection,
    RecordInput,
    attempt_was_reviewed,
    build_round_record,
)
from vibesys.orchestration.hypothesis import cadence as hypothesis_cadence
from vibesys.orchestration.metrics import FrameworkBenchmarkOutcome
from vibesys.orchestration.multi.attribution import run_attribution
from vibesys.orchestration.multi.files import MultiFiles
from vibesys.orchestration.multi.models import (
    MultiOptions,
    MultiState,
    PaidAttempt,
    ProfileGuidedMultiOptions,
)
from vibesys.orchestration.multi.turns import AttemptRequest, MultiAgentTurns, PlanRequest
from vibesys.orchestration.profile_focus import (
    FocusView,
    ProfileFocus,
    ProfileFocusConfig,
    ProfileFocusState,
)
from vibesys.orchestration.review import Verdict
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome
from vs_runtime.api import (
    BenchmarkObjective,
    MetricDirection,
    Run,
    RunStatus,
    Workspace,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.hypothesis import CarryOver, RollbackTarget
    from vibesys.orchestration.hypothesis.attempts import ImplementerReply
    from vibesys.orchestration.hypothesis.state import Hypothesis, RoundRecord


@dataclass(slots=True)
class _SelectedRound:
    request: AttemptRequest
    attempt: AttemptState


MultiRunOptions = MultiOptions | ProfileGuidedMultiOptions
_PROFILE_MEASUREMENT_REASON = "profile-guided component measurement"


def _benchmark_objectives(options: MultiRunOptions) -> tuple[BenchmarkObjective, ...]:
    return tuple(
        BenchmarkObjective(
            name=item.name,
            direction=(
                MetricDirection.MAXIMIZE if item.direction == "max" else MetricDirection.MINIMIZE
            ),
        )
        for item in options.metric_space.objectives
    )


class _TerminalPolicy:
    """Multi-role terminal facts consumed by the pure round transition."""

    def __init__(self, config: HypothesisConfig) -> None:
        self.config = config

    def project_performance(
        self,
        hypothesis: Hypothesis,
        state: AttemptState,
    ) -> PerformanceProjection:
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

    @staticmethod
    def reviewed(state: AttemptState) -> bool:
        return attempt_was_reviewed(state.judge)

    def keeps_hypothesis_active(self, state: AttemptState, continuation_rounds: int) -> bool:
        implementation = state.implementation
        return hypothesis_cadence.keeps_hypothesis_active(
            outcome=implementation.hypothesis_outcome if implementation is not None else None,
            next_step=implementation.next_step if implementation is not None else "",
            continuation_rounds=continuation_rounds,
            max_continuation_rounds=self.config.max_continuation_rounds,
        )

    def terminal_success_needs_parent_choice(
        self,
        state: AttemptState,
        continuation_rounds: int,
    ) -> bool:
        implementation = state.implementation
        return (
            implementation is not None
            and not self.keeps_hypothesis_active(state, continuation_rounds)
            and implementation.hypothesis_outcome is not HypothesisOutcome.NOMINATED
        )


class _MultiRun:
    """One plain or profile-guided multi-role run."""

    def __init__(self, run: Run, options: MultiRunOptions) -> None:
        self.run = run
        self.options = options
        self.workspace = run.workspaces.root
        self.search = HypothesisSearch(
            HypothesisConfig(
                max_rounds=options.max_rounds,
                judge_every=options.judge_every,
                official_eval_every=options.official_eval_every,
                max_retries_per_round=options.max_retries_per_round,
            )
        )
        self.files = MultiFiles.open(self.workspace.path)
        self.turns = MultiAgentTurns(run, options, self.search, self.files)
        self.terminal = _TerminalPolicy(self.search.config)
        self.state = MultiState()
        self.carry: CarryOver
        self.round_number = 1
        profile = options.profile_guided
        self.profile_focus = (
            ProfileFocus(
                ProfileFocusConfig(
                    plateau_min_rounds=profile.min_measured_rounds,
                    min_relative_improvement=profile.min_relative_improvement,
                )
            )
            if profile is not None
            else None
        )
        self.label_prefix = "profile_multi" if profile is not None else "multi"

    @property
    def records(self) -> list[RoundRecord]:
        return self.state.search.rounds

    async def _commit(
        self,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        await self.run.state.commit(self.state, workspace=workspace, label=label)

    async def initialize(self) -> None:
        loaded = await self.run.state.load(MultiState)
        aggregate = loaded or MultiState()
        resumed = self.search.resume(aggregate.search, self.options.metric_space)
        self.state = aggregate.model_copy(update={"search": resumed}, deep=True)
        self.carry = self.search.initial_carry(self.records)
        self.round_number = len(self.records) + 1
        self.files.write_pareto(
            self.search.archive_summary(self.records, space=self.state.search.metrics)
        )
        await self._commit(
            workspace=self.workspace,
            label=f"{self.label_prefix}: initialize policy state",
        )

    async def execute(self) -> RunStatus:
        try:
            await self.initialize()
            while self.round_number <= self.options.max_rounds:
                await self.run.control.checkpoint()
                self.run.observations.note(f"round {self.round_number}/{self.options.max_rounds}")
                self.files.write_pareto(
                    self.search.archive_summary(self.records, space=self.state.search.metrics)
                )
                selected = await self._select_round()
                await self._run_attempts(selected)
                await self._close_round(selected)
            await self._finish()
            return RunStatus.SUCCEEDED
        finally:
            await self.turns.close()

    async def _select_round(self) -> _SelectedRound:
        number = self.round_number
        decision = self.search.next_round(
            self.state.search,
            round_number=number,
            records=self.records,
            carry=self.carry,
        )
        if isinstance(decision, Finished):
            message = "hypothesis search finished before the configured round cursor"
            raise TypeError(message)
        if isinstance(decision, NewHypothesis):
            context = decision.context
            guidance = await self._prepare_profile_guidance()
            profile_decision = await self.turns.pre_round(
                number,
                context.carry,
                has_history=not (number == 1 and not self.records),
            )
            summary = None
            if profile_decision.need_profile:
                summary = await self.turns.profile(
                    number,
                    profile_decision.profile_focus or "general steady-state benchmark hotspots",
                )
            plan = await self.turns.plan(
                PlanRequest(
                    round_number=context.round_number,
                    state=self.state.search,
                    carry=context.carry,
                    profiler_summary=summary,
                    plateau_warning=context.plateau_warning,
                    provisional_candidates=context.provisional_candidates,
                    workspace=self.workspace,
                    guidance=guidance,
                )
            )
            started = self.search.start(
                self.state.search,
                plan,
                round_number=number,
                current_commit=self.workspace.revision,
                records=self.records,
            )
            self.state = self.state.model_copy(update={"search": started.state}, deep=True)
            hypothesis = started.hypothesis
            await self._commit(
                workspace=self.workspace,
                label=f"{self.label_prefix}: start hypothesis {plan.hypothesis_id}",
            )
            if started.rollback is not None:
                hypothesis = await self._apply_rollback(hypothesis, started.rollback)
        else:
            assert isinstance(decision, Continue)  # noqa: S101  # lint-waiver: LW-920439 [S101]; the exhaustive decision branch narrows the closed policy result type.
            hypothesis = decision.hypothesis
            plan = hypothesis.plan
            self.files.note_continuation(
                number,
                plan.hypothesis_id,
                hypothesis.next_step or plan.task,
            )
            self.run.observations.note(
                f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
            )
            guidance = None
        reason = self.search.official_due(
            records=self.records,
            round_number=number,
            requested=plan.request_official_evaluation,
            candidate_ready=True,
        )
        focus_state = self._focus_state()
        if focus_state is not None and focus_state.active_component:
            reason = reason or _PROFILE_MEASUREMENT_REASON
        request = AttemptRequest(
            round_number=number,
            plan=plan,
            planned_official_reason=reason,
            records=tuple(self.records),
            active_hypothesis=hypothesis,
            workspace=self.workspace,
            guidance=guidance,
        )
        attempt = AttemptState(
            agent_run_state=self.state.search,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return _SelectedRound(request=request, attempt=attempt)

    async def _apply_rollback(
        self,
        hypothesis: Hypothesis,
        rollback: RollbackTarget,
    ) -> Hypothesis:
        if not rollback.resolved or rollback.commit is None:
            self.run.observations.warning("cannot revert: requested round has no retained revision")
            return hypothesis
        if not await self.workspace.try_restore(rollback.commit, clean=True):
            self.run.observations.warning(f"cannot restore requested revision {rollback.commit}")
            return hypothesis
        hypothesis.revert_applied = True
        hypothesis.revert_commit = rollback.commit
        hypothesis.parent_commit = rollback.commit
        updated = self.search.update_active(self.state.search, hypothesis)
        self.state = self.state.model_copy(update={"search": updated}, deep=True)
        await self._commit(
            workspace=self.workspace,
            label=f"{self.label_prefix}: set hypothesis {hypothesis.hypothesis_id} parent",
        )
        return hypothesis

    def _first_attempt(self, selected: _SelectedRound) -> int:
        marker = self.state.last_paid_attempt
        if (
            marker is not None
            and marker.round_number == self.round_number
            and marker.member_id == selected.request.plan.hypothesis_id
        ):
            return marker.turn_number + 1
        return 1

    async def _mark_paid(self, selected: _SelectedRound, retry: int) -> None:
        self.state = self.state.model_copy(
            update={
                "last_paid_attempt": PaidAttempt(
                    round_number=self.round_number,
                    member_id=selected.request.plan.hypothesis_id,
                    turn_number=retry,
                )
            },
            deep=True,
        )
        await self._commit(
            label=f"{self.label_prefix}: start round {self.round_number} attempt {retry}"
        )

    async def _run_attempts(self, selected: _SelectedRound) -> None:
        first = self._first_attempt(selected)
        if first > self.options.max_retries_per_round:
            message = (
                f"round {self.round_number} exhausted its "
                f"{self.options.max_retries_per_round} paid attempts"
            )
            raise RuntimeError(message)
        for retry in range(first, self.options.max_retries_per_round + 1):
            attempt = selected.attempt
            attempt.retry = retry
            attempt.official_reason = None
            attempt.judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
            await self._mark_paid(selected, retry)
            response, synthesized = await self.turns.implement(selected.request, attempt)
            attempt.implementation = response
            if synthesized:
                attempt.judge = JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
                self.run.observations.warning(
                    f"[implementer] attempt {retry} returned no parseable response; retrying"
                )
                continue
            decision = await self._review(selected)
            if decision is AttemptDecision.FINISH:
                break
            if decision is AttemptDecision.OFFICIAL and await self._official_evaluation(selected):
                break

    async def _review(self, selected: _SelectedRound) -> AttemptDecision:
        state = selected.attempt
        implementation = state.implementation
        if implementation is None:
            message = "review requires an implementer response"
            raise RuntimeError(message)
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
                outcome=implementation.hypothesis_outcome,
                next_step=implementation.next_step,
            ),
            pareto_frontier_claim=(
                implementation.candidate_disposition is CandidateDisposition.PARETO_FRONTIER
            ),
            revalidation_required=state.revalidation_required,
        )
        if not due:
            state.judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
            self.files.note_review_skipped(
                self.round_number,
                implementation.hypothesis_outcome.value,
            )
            self.run.observations.note("[judge] deferred by sparse-review policy")
            return AttemptDecision.FINISH
        state.review_started = True
        state.revalidation_required = False
        conflict = self.search.pareto_conflict(
            disposition=implementation.candidate_disposition,
            metrics=dict(implementation.candidate_metrics),
            records=self.records,
            space=state.agent_run_state.metrics,
        )
        verdict = await self.turns.review(selected.request, state, conflict)
        state.judge = JudgeReviewed(verdict.verdict.value)
        if verdict.verdict is not Verdict.PASS:
            state.feedback = verdict.feedback
            selected.request.active_hypothesis.feedback = verdict.feedback
            await self._checkpoint_hypothesis(selected)
            return AttemptDecision.RETRY
        validation_feedback = await self._validate_local(selected, implementation)
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
            round_number=self.round_number,
            requested=selected.request.plan.request_official_evaluation,
            candidate_ready=candidate_ready,
        )
        if reason is None:
            state.passed = True
            self.files.note_evaluation(
                self.round_number,
                state.retry,
                "- decision: deferred\n- reason: cadence not due\n",
            )
            return AttemptDecision.FINISH
        await self._approve_perf(selected)
        state.official_reason = reason
        return AttemptDecision.OFFICIAL

    async def _validate_local(
        self,
        selected: _SelectedRound,
        implementation: ImplementerReply,
    ) -> str | None:
        artifact = implementation.validation_recipe_artifact
        if artifact is None:
            return None
        report_location = self.files.validation_report_location(
            self.round_number,
            selected.attempt.retry,
        )
        result = await self.run.evaluation.validate_local(
            self.workspace,
            recipe_artifact=artifact,
            report_location=report_location,
        )
        location = result.report_location or "(no report written)"
        detail = f"- decision: {'passed' if result.passed else 'failed'}\n- report: {location}\n"
        if result.feedback:
            detail += f"- feedback: {result.feedback}\n"
        self.files.note_evaluation(
            self.round_number,
            selected.attempt.retry,
            detail,
        )
        return result.feedback if not result.passed else None

    async def _checkpoint_hypothesis(self, selected: _SelectedRound) -> None:
        updated = self.search.update_active(
            selected.attempt.agent_run_state,
            selected.request.active_hypothesis,
        )
        selected.attempt.agent_run_state = updated
        self.state = self.state.model_copy(update={"search": updated}, deep=True)
        await self._commit(
            workspace=self.workspace,
            label=(
                f"{self.label_prefix}: checkpoint hypothesis {selected.request.plan.hypothesis_id}"
            ),
        )

    async def _approve_candidate(self, selected: _SelectedRound) -> None:
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

    async def _approve_perf(self, selected: _SelectedRound) -> None:
        implementation = selected.attempt.implementation
        if implementation is None or implementation.perf_metric is None:
            return
        hypothesis = selected.request.active_hypothesis
        hypothesis.gate_approved_perf_metric = implementation.perf_metric
        hypothesis.gate_approved_perf_unit = implementation.perf_unit
        hypothesis.gate_approved_metrics = dict(implementation.metrics)
        hypothesis.gate_approved_evaluation_artifact = implementation.evaluation_artifact
        await self._checkpoint_hypothesis(selected)

    async def _official_evaluation(self, selected: _SelectedRound) -> bool:
        attempt = selected.attempt
        revision = await self.workspace.snapshot(
            f"round-{self.round_number}-attempt-{attempt.retry}-evaluation"
        )
        receipt = self.state.accuracy_receipt
        reuse = receipt if receipt is not None and receipt.revision == revision else None
        accuracy = await self.run.evaluation.accuracy(self.workspace, reuse=reuse)
        self.state = self.state.model_copy(update={"accuracy_receipt": accuracy.receipt}, deep=True)
        if not accuracy.passed:
            await self._evaluation_failed(selected, accuracy.feedback or "accuracy failed")
            return False
        benchmark = await self.run.evaluation.benchmark(
            self.workspace,
            objectives=_benchmark_objectives(self.options),
        )
        attempt.framework_benchmark = FrameworkBenchmarkOutcome(
            feedback=benchmark.feedback,
            metric_name=benchmark.metric_name,
            metric_value=benchmark.metric_value,
            metric_direction=(
                benchmark.metric_direction.value if benchmark.metric_direction is not None else None
            ),
            metric_unit=benchmark.metric_unit,
            row=benchmark.row,
        )
        attempt.framework_perf_metric = benchmark.metric_value
        if not benchmark.passed:
            await self._evaluation_failed(selected, benchmark.feedback or "benchmark failed")
            return False
        attempt.passed = True
        self.files.note_evaluation(
            self.round_number,
            attempt.retry,
            f"- decision: passed\n- reason: {attempt.official_reason}\n",
        )
        return True

    async def _evaluation_failed(self, selected: _SelectedRound, feedback: str) -> None:
        attempt = selected.attempt
        attempt.feedback = feedback
        attempt.revalidation_required = True
        hypothesis = selected.request.active_hypothesis
        hypothesis.gate_revalidation_pending = True
        hypothesis.gate_candidate_commit = self.workspace.revision
        hypothesis.gate_accuracy_passed = self.state.accuracy_receipt is not None
        hypothesis.feedback = feedback
        self.files.note_evaluation(
            self.round_number,
            attempt.retry,
            f"- decision: failed\n- feedback: {feedback}\n",
        )
        await self._checkpoint_hypothesis(selected)

    async def _close_round(self, selected: _SelectedRound) -> None:
        attempt = selected.attempt
        hypothesis = selected.request.active_hypothesis
        implementation = attempt.implementation
        projection = self.terminal.project_performance(hypothesis, attempt)
        candidate_revision = await self.workspace.snapshot(
            f"round-{self.round_number}-record-input"
        )
        binding = self.turns.binding(selected.request.plan.hypothesis_id)
        record = build_round_record(
            RecordInput(
                state=attempt.agent_run_state,
                records=self.records,
                round_number=self.round_number,
                hypothesis=hypothesis,
                plan=selected.request.plan,
                attempt=attempt,
                projection=projection,
                reviewed=self.terminal.reviewed(attempt),
                framework_benchmark_configured=self.run.facts.benchmark_configured,
                accuracy_configured=self.run.facts.accuracy_configured,
                candidate_commit=candidate_revision,
                backend_name=binding.backend,
                driver_name=binding.driver,
                provider=binding.provider,
                model=binding.model,
            )
        )
        search_state = self.state.search
        if self.profile_focus is not None:
            focused = self.profile_focus.record(
                self._require_focus_state(),
                round_number=record.round_number,
                passed=attempt.passed and record.official_evaluation,
                relative_improvement=(
                    record.perf_delta_pct / 100 if record.perf_delta_pct is not None else None
                ),
            )
            search_state = search_state.model_copy(
                update={"profile_guidance": focused},
                deep=True,
            )
        next_step = implementation.next_step if implementation is not None else None
        closed = self.search.close_round(
            search_state,
            hypothesis=hypothesis,
            record=record,
            records=self.records,
            carry=self.carry,
            passed=attempt.passed,
            reviewed=self.terminal.reviewed(attempt),
            feedback=attempt.feedback,
            keeps_active=self.terminal.keeps_hypothesis_active(
                attempt,
                hypothesis.continuation_rounds,
            ),
            requests_continuation=hypothesis_cadence.continuation_requested(
                outcome=(implementation.hypothesis_outcome if implementation is not None else None),
                next_step=next_step or "",
            ),
            next_step=next_step,
            terminal_needs_parent_choice=(
                self.terminal.terminal_success_needs_parent_choice(
                    attempt,
                    hypothesis.continuation_rounds,
                )
            ),
            has_implementation=implementation is not None,
        )
        self.state = self.state.model_copy(
            update={"search": closed.state, "last_paid_attempt": None},
            deep=True,
        )
        self.carry = closed.carry
        await self._commit(
            workspace=self.workspace,
            label=f"{self.label_prefix}: close round {self.round_number}",
        )
        self.round_number += 1

    async def _finish(self) -> None:
        self.files.write_pareto(
            self.search.archive_summary(self.records, space=self.state.search.metrics)
        )
        winner = self.search.best(self.records, space=self.state.search.metrics)
        if winner is None:
            baseline = self.workspace.trusted_input_baseline
            if baseline is None:
                message = "multi-agent run has no trusted input baseline"
                raise RuntimeError(message)
            await self.workspace.restore(baseline, clean=True)
            await self.workspace.snapshot(f"{self.label_prefix}: restore trusted input baseline")
            self.run.observations.note("no trusted winner; restored the input baseline")
            return
        if winner.commit is None:
            message = "selected multi-agent winner has no workspace revision"
            raise RuntimeError(message)
        await self.workspace.retain(
            winner.commit,
            label=f"selected-round-{winner.round_number:04d}",
        )
        await self.workspace.restore(winner.commit, clean=True)
        await self.workspace.snapshot(f"{self.label_prefix}: select round {winner.round_number}")
        self.run.observations.note(f"selected trusted winner from round {winner.round_number}")

    def _focus_state(self) -> ProfileFocusState | None:
        if self.profile_focus is None:
            return None
        return self.state.search.profile_guidance or self.profile_focus.initial()

    def _require_focus_state(self) -> ProfileFocusState:
        state = self._focus_state()
        if state is None:
            message = "profile focus state requested for a plain multi-agent run"
            raise RuntimeError(message)
        return state

    async def _prepare_profile_guidance(self) -> FocusView | None:
        profile = self.options.profile_guided
        if profile is None or self.profile_focus is None:
            return None
        attribution = await run_attribution(self.run, profile, workspace=self.workspace)
        focused = self.profile_focus.observe(
            self._require_focus_state(),
            round_number=self.round_number,
            bottlenecks=attribution,
        )
        search = self.state.search.model_copy(
            update={"profile_guidance": focused},
            deep=True,
        )
        self.state = self.state.model_copy(update={"search": search}, deep=True)
        await self._commit(label=f"profile-guided: prepare round {self.round_number}")
        return self.profile_focus.focus(focused)


async def orchestrate(run: Run, raw_options: BaseModel) -> RunStatus:
    """Run the plain multi-agent policy against the explicit runtime API."""
    options = MultiOptions.model_validate(raw_options)
    return await _MultiRun(run, options).execute()


async def orchestrate_profile_guided(run: Run, raw_options: BaseModel) -> RunStatus:
    """Run profile-guided multi-agent search against the explicit runtime API."""
    options = ProfileGuidedMultiOptions.model_validate(raw_options)
    return await _MultiRun(run, options).execute()


__all__ = ["orchestrate", "orchestrate_profile_guided"]
