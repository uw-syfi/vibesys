"""Concurrent, durable hypothesis portfolio orchestration over :class:`Run`."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.models import (
    DynamicOptions,
    DynamicState,
    DynamicWorkstream,
    EvaluationResult,
    EvidenceReference,
    ImplementerResult,
    PortfolioPlan,
    ReviewResult,
    WorkstreamPhase,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.prompts import (
    render_implementation,
    render_portfolio,
    render_review,
)
from vibesys.orchestration.hypothesis import HypothesisConfig, HypothesisSearch, OrchestratorPlan
from vibesys.orchestration.hypothesis import transitions as hypothesis_transitions
from vibesys.orchestration.metrics import Measurement
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome, RoundRecord
from vs_runtime.api import BenchmarkObjective, MetricDirection, Run, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_runtime.api import CandidateWorkspace


_READY_OUTCOMES = frozenset({HypothesisOutcome.NOMINATED, HypothesisOutcome.SUPPORTED})
_RECOVERABLE_PHASES = frozenset(
    {
        WorkstreamPhase.PENDING,
        WorkstreamPhase.IMPLEMENTING,
        WorkstreamPhase.IMPLEMENTED,
        WorkstreamPhase.REVIEWED,
        WorkstreamPhase.EVALUATED,
    }
)
_TERMINAL_OUTCOMES = frozenset(
    {
        HypothesisOutcome.NOMINATED,
        HypothesisOutcome.SUPPORTED,
        HypothesisOutcome.DISPROVEN,
        HypothesisOutcome.IMPLEMENTATION_FAILED,
        HypothesisOutcome.INCONCLUSIVE,
        HypothesisOutcome.BLOCKED,
    }
)
_MAX_HISTORY_ROWS = 16
_MAX_HISTORY_METRICS = 8
_MAX_HISTORY_REVISION_CHARS = 256
_MAX_HISTORY_METRIC_NAME_CHARS = 128
_MAX_HISTORY_METRIC_UNIT_CHARS = 64


class DynamicPlanError(ValueError):
    """A portfolio cannot be applied to the durable hypothesis search."""

    @classmethod
    def unknown_continuation(cls, hypothesis_id: str) -> DynamicPlanError:
        """Reject continuation of an unknown hypothesis."""
        return cls(f"unknown hypothesis {hypothesis_id!r} cannot be continued")

    @classmethod
    def reused_id(cls, hypothesis_id: str) -> DynamicPlanError:
        """Reject reusing an ID without explicit continuation."""
        return cls(f"hypothesis ID {hypothesis_id!r} was already used")

    @classmethod
    def terminal_continuation(cls, hypothesis_id: str) -> DynamicPlanError:
        """Reject continuation after trusted terminal evaluation."""
        return cls(f"evaluated hypothesis {hypothesis_id!r} is already terminal")

    @classmethod
    def incomplete_checkpoint(
        cls,
        hypothesis_id: str,
        phase: WorkstreamPhase,
    ) -> DynamicPlanError:
        """Reject a post-implementation phase without its retained result."""
        return cls(
            f"dynamic workstream {hypothesis_id!r} has phase {phase.value!r} "
            "without a retained implementation"
        )


class DynamicAttemptError(RuntimeError):
    """One isolated workstream attempt failed after its failure was persisted."""

    @classmethod
    def from_cause(cls, hypothesis_id: str, cause: BaseException) -> DynamicAttemptError:
        """Describe the isolated slot and retain the original failure as the cause."""
        return cls(f"{hypothesis_id}: {cause}")


@dataclass(slots=True)
class _DynamicRun:
    run: Run
    options: DynamicOptions
    state: DynamicState
    _state_lock: asyncio.Lock

    @classmethod
    async def open(cls, run: Run, options: DynamicOptions) -> _DynamicRun:
        """Restore the policy aggregate and complete an interrupted adoption."""
        state = await run.state.load(DynamicState) or DynamicState()
        search = HypothesisSearch(_hypothesis_config(options))
        state.search = search.resume(state.search, options.metric_space)
        dynamic = cls(run, options, state, asyncio.Lock())
        if state.adoption_pending:
            await dynamic._finish_adoption()
        return dynamic

    async def execute(self) -> RunStatus:
        """Run bounded portfolio epochs and adopt the best trusted candidate."""
        while self.state.next_epoch <= self.options.max_rounds:
            await self.run.control.checkpoint()
            epoch = self.state.next_epoch
            plans = self._recoverable_plans(epoch)
            if not plans:
                portfolio = await self._plan(epoch)
                plans = portfolio.workstreams
                await self._record_plans(epoch, portfolio)
            await self._execute_bounded(plans, epoch)
            async with self._state_lock:
                self.state.next_epoch = epoch + 1
                await self._commit(label=f"dynamic: close epoch {epoch}")
        await self._select_and_adopt()
        return RunStatus.SUCCEEDED

    def _capacity(self) -> int:
        return self.options.max_in_flight if self.run.workspaces.supports_parallel_candidates else 1

    async def _execute_bounded(
        self,
        plans: Sequence[WorkstreamPlan],
        epoch: int,
    ) -> None:
        """Execute every durable plan without exceeding current capacity."""
        capacity = self._capacity()
        pending = list(plans)
        fatal_failures: list[BaseException] = []
        failures = dict.fromkeys((plan.hypothesis_id for plan in plans), 0)
        retries = self.options.max_retries_per_round
        while pending:
            batch = pending[:capacity]
            del pending[:capacity]
            results = await asyncio.gather(
                *(self._execute_workstream(plan, epoch) for plan in batch),
                return_exceptions=True,
            )
            for plan, result in zip(batch, results, strict=True):
                if isinstance(result, BaseException):
                    self.run.observations.note(
                        f"dynamic workstream {plan.hypothesis_id} failed: {result}"
                    )
                    index = self._index(plan.hypothesis_id)
                    item = self.state.workstreams[index]
                    if not isinstance(result, DynamicAttemptError) or item.attempts == 0:
                        fatal_failures.append(result)
                        continue
                    failures[plan.hypothesis_id] += 1
                    # A failure after a retained implementation keeps its
                    # checkpoint, so the retry resumes at the failed stage.
                    retained = item.phase is not WorkstreamPhase.FAILED
                    if failures[plan.hypothesis_id] < retries and (
                        retained or item.attempts < retries
                    ):
                        pending.append(plan)
                    elif retained:
                        await self._update(index, phase=WorkstreamPhase.FAILED)
        if fatal_failures:
            raise fatal_failures[0]

    def _recoverable_plans(self, epoch: int) -> tuple[WorkstreamPlan, ...]:
        """Recover work durably scheduled but not completed before interruption."""
        return tuple(
            item.plan
            for item in self.state.workstreams
            if item.epoch == epoch
            and (
                item.phase in _RECOVERABLE_PHASES
                or (
                    item.phase is WorkstreamPhase.FAILED
                    and item.attempts < self.options.max_retries_per_round
                )
            )
        )

    async def _plan(self, epoch: int) -> PortfolioPlan:
        session = await self.run.agents.create_session(
            ORCHESTRATOR,
            workspace=self.run.workspaces.root,
        )
        try:
            prompt = render_portfolio(
                epoch=epoch,
                max_epochs=self.options.max_rounds,
                capacity=self._capacity(),
                objective_location=self.run.facts.objective_location,
                root_revision=self._root_revision(),
                history=self._history_projection(),
            )
            first_error: DynamicPlanError | ValidationError | None = None
            for attempt in range(2):
                message = (
                    prompt if attempt == 0 else f"{prompt}\n\nCorrection required: {first_error}"
                )
                plan = await session.turn(message, response=PortfolioPlan)
                try:
                    self._validate_plan(plan)
                except (DynamicPlanError, ValidationError) as error:
                    first_error = error
                else:
                    return plan
            raise first_error or DynamicPlanError("portfolio planning failed")
        finally:
            await session.close()

    def _validate_plan(self, portfolio: PortfolioPlan) -> None:
        if len(portfolio.workstreams) > self._capacity():
            message = (
                f"portfolio requested {len(portfolio.workstreams)} workstreams, "
                f"but capacity is {self._capacity()}"
            )
            raise DynamicPlanError(message)
        known = {item.hypothesis_id: item for item in self.state.workstreams}
        try:
            hypothesis_transitions.apply_strategy_updates(
                self.state.search,
                portfolio.hypothesis_updates,
            )
        except ValueError as error:
            raise DynamicPlanError(str(error)) from error
        for plan in portfolio.workstreams:
            prior = known.get(plan.hypothesis_id)
            if prior is None and plan.continue_hypothesis:
                raise DynamicPlanError.unknown_continuation(plan.hypothesis_id)
            if prior is not None and not plan.continue_hypothesis:
                raise DynamicPlanError.reused_id(plan.hypothesis_id)
            if prior is not None and prior.phase is WorkstreamPhase.EVALUATED:
                raise DynamicPlanError.terminal_continuation(plan.hypothesis_id)

    async def _record_plans(self, epoch: int, portfolio: PortfolioPlan) -> None:
        parent = self._root_revision()
        async with self._state_lock:
            by_id = {item.hypothesis_id: index for index, item in enumerate(self.state.workstreams)}
            search = HypothesisSearch(_hypothesis_config(self.options))
            self.state.search = hypothesis_transitions.apply_strategy_updates(
                self.state.search,
                portfolio.hypothesis_updates,
            )
            sequence = max((record.round_number for record in self.state.search.rounds), default=0)
            for plan in portfolio.workstreams:
                sequence += 1
                index = by_id.get(plan.hypothesis_id)
                if index is None:
                    started = search.start(
                        self.state.search,
                        _orchestrator_plan(plan, portfolio.reasoning),
                        round_number=len(self.state.workstreams) + 1,
                        current_commit=parent,
                        records=self.state.search.rounds,
                    )
                    self.state.search = search.finish(started.state)
                workstream = DynamicWorkstream(
                    hypothesis_id=plan.hypothesis_id,
                    member_id=plan.hypothesis_id,
                    sequence=sequence,
                    epoch=epoch,
                    plan=plan,
                    parent_revision=(
                        self.state.workstreams[index].candidate_revision or parent
                        if index is not None
                        else parent
                    ),
                )
                if index is None:
                    self.state.workstreams.append(workstream)
                else:
                    self.state.workstreams[index] = workstream
            await self._commit(label=f"dynamic: schedule epoch {epoch}")

    async def _execute_workstream(self, plan: WorkstreamPlan, epoch: int) -> None:
        index = self._index(plan.hypothesis_id)
        item = self.state.workstreams[index]
        if item.phase is WorkstreamPhase.EVALUATED and (
            item.evaluation is None or item.evaluation.accepted
        ):
            await self._record_hypothesis_round(index)
            return
        parent = item.parent_revision
        resume_implemented = item.phase in {
            WorkstreamPhase.IMPLEMENTED,
            WorkstreamPhase.REVIEWED,
            WorkstreamPhase.EVALUATED,
        }
        workspace = await self.run.workspaces.create_candidate(
            item.candidate_revision if resume_implemented else parent
        )
        try:
            feedback: str | None = None
            completed = False
            if resume_implemented:
                completed, feedback = await self._resume_implemented(
                    index,
                    plan,
                    workspace,
                    epoch,
                )
            for _attempt in range(
                self.state.workstreams[index].attempts,
                self.options.max_retries_per_round,
            ):
                if completed:
                    break
                await self._update(
                    index,
                    phase=WorkstreamPhase.IMPLEMENTING,
                    increment_attempts=True,
                )
                implementation = await self._implement(
                    plan,
                    workspace,
                    parent,
                    feedback=feedback,
                )
                revision = await workspace.snapshot(
                    f"dynamic: {plan.hypothesis_id} implementation epoch {epoch}"
                )
                implementation = _bind_evidence_revision(implementation, revision)
                await workspace.retain(
                    revision,
                    label=f"dynamic-{plan.hypothesis_id}-epoch-{epoch}",
                )
                await self._update(
                    index,
                    phase=WorkstreamPhase.IMPLEMENTED,
                    candidate_revision=revision,
                    implementation=implementation,
                    clear_downstream=True,
                )
                completed, feedback = await self._assess_candidate(
                    plan,
                    implementation,
                    workspace,
                    epoch,
                )
            if not completed:
                await self._update(index, phase=WorkstreamPhase.FAILED)
            await self._record_hypothesis_round(index)
        except asyncio.CancelledError:
            await self._update(index, phase=WorkstreamPhase.FAILED)
            raise
        except Exception as error:
            if self.state.workstreams[index].phase is WorkstreamPhase.IMPLEMENTING:
                await self._update(index, phase=WorkstreamPhase.FAILED)
            raise DynamicAttemptError.from_cause(plan.hypothesis_id, error) from error
        finally:
            await workspace.discard()

    async def _assess_candidate(
        self,
        plan: WorkstreamPlan,
        implementation: ImplementerResult,
        workspace: CandidateWorkspace,
        epoch: int,
    ) -> tuple[bool, str | None]:
        """Review and evaluate one implementation, returning correction guidance."""
        index = self._index(plan.hypothesis_id)
        revision = workspace.revision
        if revision is None:
            message = "dynamic candidate assessment requires a recorded revision"
            raise RuntimeError(message)
        review = await self._maybe_review(plan, implementation, workspace, revision, epoch)
        if review is not None:
            await self._update(index, phase=WorkstreamPhase.REVIEWED, review=review)
        if review is not None and not review.passed:
            return False, review.feedback
        evaluation = await self._maybe_evaluate(
            plan,
            implementation,
            review,
            workspace,
            revision,
            epoch,
        )
        final_phase = (
            WorkstreamPhase.EVALUATED
            if evaluation is not None
            else WorkstreamPhase.REVIEWED
            if review is not None
            else WorkstreamPhase.IMPLEMENTED
        )
        await self._update(index, phase=final_phase, evaluation=evaluation)
        if evaluation is not None and not evaluation.accepted:
            return False, _evaluation_feedback(evaluation)
        return True, None

    async def _resume_implemented(
        self,
        index: int,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
        epoch: int,
    ) -> tuple[bool, str | None]:
        """Finish the first incomplete stage after a retained implementation."""
        item = self.state.workstreams[index]
        implementation = item.implementation
        revision = item.candidate_revision
        if implementation is None or revision is None:
            raise DynamicPlanError.incomplete_checkpoint(item.hypothesis_id, item.phase)
        review = item.review
        if item.phase is WorkstreamPhase.EVALUATED and item.evaluation is not None:
            if item.evaluation.accepted:
                return True, None
            return False, _evaluation_feedback(item.evaluation)
        if item.phase is WorkstreamPhase.IMPLEMENTED:
            review = await self._maybe_review(
                plan,
                implementation,
                workspace,
                revision,
                epoch,
            )
            if review is not None:
                await self._update(index, phase=WorkstreamPhase.REVIEWED, review=review)
        if review is not None and not review.passed:
            return False, review.feedback
        evaluation = await self._maybe_evaluate(
            plan,
            implementation,
            review,
            workspace,
            revision,
            epoch,
        )
        final_phase = (
            WorkstreamPhase.EVALUATED
            if evaluation is not None
            else WorkstreamPhase.REVIEWED
            if review is not None
            else WorkstreamPhase.IMPLEMENTED
        )
        await self._update(index, phase=final_phase, evaluation=evaluation)
        if evaluation is not None and not evaluation.accepted:
            return False, _evaluation_feedback(evaluation)
        return True, None

    async def _implement(
        self,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
        parent_revision: str,
        *,
        feedback: str | None,
    ) -> ImplementerResult:
        session = await self.run.agents.create_session(
            IMPLEMENTER,
            workspace=workspace,
            member_id=plan.hypothesis_id,
        )
        try:
            return await session.turn(
                render_implementation(
                    hypothesis_id=plan.hypothesis_id,
                    objective_location=self.run.facts.objective_location,
                    hypothesis=plan.hypothesis,
                    task=plan.task,
                    pass_criteria=plan.pass_criteria,
                    parent_revision=parent_revision,
                    evidence=_references_text(plan.evidence),
                    feedback=feedback,
                ),
                response=ImplementerResult,
            )
        finally:
            await session.close()

    async def _maybe_review(
        self,
        plan: WorkstreamPlan,
        implementation: ImplementerResult,
        workspace: CandidateWorkspace,
        revision: str,
        epoch: int,
    ) -> ReviewResult | None:
        promotable = implementation.outcome in _READY_OUTCOMES
        due = promotable or (
            implementation.outcome in _TERMINAL_OUTCOMES and epoch % self.options.judge_every == 0
        )
        if not due:
            return None
        session = await self.run.agents.create_session(
            JUDGE,
            workspace=workspace,
            member_id=plan.hypothesis_id,
        )
        try:
            return await session.turn(
                render_review(
                    hypothesis_id=plan.hypothesis_id,
                    objective_location=self.run.facts.objective_location,
                    hypothesis=plan.hypothesis,
                    pass_criteria=plan.pass_criteria,
                    candidate_revision=revision,
                    summary=implementation.summary,
                    evidence=_references_text(implementation.evidence),
                ),
                response=ReviewResult,
            )
        finally:
            await session.close()

    async def _maybe_evaluate(  # noqa: PLR0913  # lint-waiver: LW-930108 [PLR0913]; each argument is an independently established policy fact; a carrier would expose the same state less clearly.
        self,
        plan: WorkstreamPlan,
        implementation: ImplementerResult,
        review: ReviewResult | None,
        workspace: CandidateWorkspace,
        revision: str,
        epoch: int,
    ) -> EvaluationResult | None:
        candidate_ready = implementation.outcome in _READY_OUTCOMES
        if not candidate_ready or review is None or not review.passed:
            return None
        cadence_due = await self._record_eligible_evaluation_candidate(plan.hypothesis_id)
        due = plan.request_evaluation or cadence_due or epoch == self.options.max_rounds
        available = self.run.facts.accuracy_configured or self.run.facts.benchmark_configured
        has_local_recipe = implementation.validation_recipe_artifact is not None
        if not due or (not available and not has_local_recipe):
            return None
        local = (
            await self.run.evaluation.validate_local(
                workspace,
                recipe_artifact=implementation.validation_recipe_artifact,
                report_location=(
                    f"progress/validation/dynamic-{plan.hypothesis_id}-epoch-{epoch}.json"
                ),
            )
            if implementation.validation_recipe_artifact is not None
            else None
        )
        if local is not None and not local.passed:
            return EvaluationResult(
                revision=revision,
                local_validation_passed=False,
                local_validation_feedback=local.feedback,
            )
        async with asyncio.TaskGroup() as evaluations:
            accuracy_task = (
                evaluations.create_task(self.run.evaluation.accuracy(workspace))
                if self.run.facts.accuracy_configured
                else None
            )
            benchmark_task = (
                evaluations.create_task(
                    self.run.evaluation.benchmark(
                        workspace,
                        objectives=tuple(
                            BenchmarkObjective(
                                name=item.name,
                                direction=MetricDirection(item.direction),
                            )
                            for item in self.options.metric_space.objectives
                        ),
                    ),
                )
                if self.run.facts.benchmark_configured
                else None
            )
        accuracy = accuracy_task.result() if accuracy_task is not None else None
        benchmark = benchmark_task.result() if benchmark_task is not None else None
        return EvaluationResult(
            revision=revision,
            local_validation_passed=local.passed if local is not None else None,
            local_validation_feedback=local.feedback if local is not None else None,
            accuracy_passed=accuracy.passed if accuracy is not None else None,
            accuracy_feedback=accuracy.feedback if accuracy is not None else None,
            benchmark_passed=benchmark.passed if benchmark is not None else None,
            benchmark_feedback=benchmark.feedback if benchmark is not None else None,
            metric_name=benchmark.metric_name if benchmark is not None else None,
            metric_value=benchmark.metric_value if benchmark is not None else None,
            metric_direction=benchmark.metric_direction if benchmark is not None else None,
            metric_unit=benchmark.metric_unit if benchmark is not None else None,
            metrics=dict(benchmark.row or {}) if benchmark is not None else {},
        )

    async def _update(  # noqa: PLR0913  # lint-waiver: LW-092703 [PLR0913]; optional fields are explicit transition outputs and avoid an untyped mutation mapping at the durability boundary.
        self,
        index: int,
        *,
        phase: WorkstreamPhase,
        increment_attempts: bool = False,
        candidate_revision: str | None = None,
        implementation: ImplementerResult | None = None,
        review: ReviewResult | None = None,
        evaluation: EvaluationResult | None = None,
        clear_downstream: bool = False,
    ) -> None:
        async with self._state_lock:
            current = self.state.workstreams[index]
            changes: dict[str, object] = {"phase": phase}
            if increment_attempts:
                changes["attempts"] = current.attempts + 1
            if candidate_revision is not None:
                changes["candidate_revision"] = candidate_revision
            if implementation is not None:
                changes["implementation"] = implementation
            if clear_downstream:
                changes["review"] = None
                changes["evaluation"] = None
                changes["evaluation_eligibility_counted"] = False
                changes["cadence_evaluation_due"] = False
            if review is not None:
                changes["review"] = review
            if evaluation is not None:
                changes["evaluation"] = evaluation
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            await self._commit(label=f"dynamic: {current.hypothesis_id} {phase.value}")

    async def _record_hypothesis_round(self, index: int) -> None:
        """Commit one workstream result through shared hypothesis transitions."""
        async with self._state_lock:
            item = self.state.workstreams[index]
            implementation = item.implementation
            if implementation is None or any(
                record.round_number == item.sequence for record in self.state.search.rounds
            ):
                return
            evaluation = item.evaluation
            review = item.review
            accepted = evaluation.accepted if evaluation is not None else None
            metrics = dict(evaluation.metrics) if evaluation is not None else {}
            framework_metric = (
                evaluation is not None
                and evaluation.metric_name is not None
                and evaluation.metric_value is not None
            )
            baseline = (
                hypothesis_transitions.metric_baseline(
                    parent_round=None,
                    parent_commit=item.parent_revision,
                    metric=evaluation.metric_name,
                    rounds=self.state.search.rounds,
                )
                if framework_metric
                else None
            )
            baseline_value = (
                hypothesis_transitions.record_metric_value(baseline, evaluation.metric_name)
                if baseline is not None and evaluation is not None
                else None
            )
            direction = (
                evaluation.metric_direction.value
                if framework_metric and evaluation.metric_direction is not None
                else None
            )
            comparison = (
                self.state.search.metrics.compare(
                    Measurement(
                        metric=evaluation.metric_name,
                        value=evaluation.metric_value,
                        direction=direction,
                    ),
                    Measurement(
                        metric=evaluation.metric_name,
                        value=baseline_value,
                        direction=direction,
                    )
                    if baseline_value is not None
                    else None,
                )
                if framework_metric
                else None
            )
            disposition, retained = self._candidate_decision(
                accepted=accepted,
                metrics=metrics,
                framework_metric=framework_metric,
            )
            record = RoundRecord(
                round_number=item.sequence,
                commit=item.candidate_revision,
                perf_metric=evaluation.metric_value if framework_metric else None,
                perf_unit=evaluation.metric_name if framework_metric else None,
                passed=review.passed if review is not None else True,
                reviewed=review is not None,
                hypothesis_id=item.hypothesis_id,
                hypothesis_declared_outcome=implementation.outcome.value,
                judge_verdict=(
                    "pass" if review and review.passed else "fail" if review else "deferred"
                ),
                hypothesis_outcome=implementation.outcome.value,
                hypothesis_claim=item.plan.hypothesis,
                hypothesis_task=item.plan.task,
                hypothesis_parent_commit=item.parent_revision,
                metrics=metrics,
                official_evaluation=evaluation is not None,
                official_evaluation_reason=(
                    "dynamic_promotion" if evaluation is not None else None
                ),
                candidate_disposition=disposition.value,
                candidate_metrics=metrics,
                candidate_retained=retained,
                perf_direction=direction,
                perf_baseline_round=baseline.round_number if baseline is not None else None,
                perf_baseline_commit=baseline.commit if baseline is not None else None,
                perf_baseline_metric=baseline_value,
                perf_delta_pct=(
                    (evaluation.metric_value - baseline_value) / abs(baseline_value) * 100
                    if framework_metric and baseline_value not in {None, 0}
                    else None
                ),
                perf_comparison=comparison,
                perf_provenance="framework" if framework_metric else None,
                attempts=item.attempts,
            )
            active_search = self.state.search.model_copy(
                update={"active_hypothesis_id": item.hypothesis_id},
                deep=True,
            )
            self.state.search = hypothesis_transitions.append_round(
                active_search,
                record,
                keep_active=implementation.outcome is HypothesisOutcome.CONTINUE,
            )
            if self.state.search.active_hypothesis_id is not None:
                self.state.search = hypothesis_transitions.finish_hypothesis(self.state.search)
            await self._commit(label=f"dynamic: record hypothesis {item.hypothesis_id}")

    async def _select_and_adopt(self) -> None:
        winner = self._winner()
        if winner is None or winner.candidate_revision is None:
            self.run.observations.note("dynamic search produced no trusted candidate")
            return
        self.state.winner_revision = winner.candidate_revision
        self.state.adoption_pending = True
        await self._commit(label="dynamic: winner selected")
        await self._finish_adoption()
        self.run.observations.note(f"adopted dynamic winner {winner.hypothesis_id}")

    async def _finish_adoption(self) -> None:
        revision = self.state.winner_revision
        if revision is None:
            message = "dynamic adoption is pending without a winner revision"
            raise RuntimeError(message)
        await self.run.workspaces.adopt(revision)
        self.state.adoption_pending = False
        await self._commit(
            workspace=True,
            label="dynamic: winner adopted",
        )

    def _winner(self) -> DynamicWorkstream | None:
        search = HypothesisSearch(_hypothesis_config(self.options))
        winner = search.best(
            self.state.search.rounds,
            space=self.options.metric_space,
        )
        if winner is None:
            return None
        return next(
            (item for item in self.state.workstreams if item.sequence == winner.round_number),
            None,
        )

    async def _record_eligible_evaluation_candidate(self, hypothesis_id: str) -> bool:
        """Atomically count an eligible attempt and return whether cadence is due."""
        async with self._state_lock:
            index = self._index(hypothesis_id)
            item = self.state.workstreams[index]
            if item.evaluation_eligibility_counted:
                return item.cadence_evaluation_due
            self.state.eligible_evaluation_candidates += 1
            due = self.state.eligible_evaluation_candidates % self.options.official_eval_every == 0
            self.state.workstreams[index] = item.model_copy(
                update={
                    "evaluation_eligibility_counted": True,
                    "cadence_evaluation_due": due,
                },
                deep=True,
            )
            await self._commit(label="dynamic: evaluation candidate eligible")
            return due

    def _candidate_decision(
        self,
        *,
        accepted: bool | None,
        metrics: dict[str, float],
        framework_metric: bool,
    ) -> tuple[CandidateDisposition, bool | None]:
        """Apply the shared noise aware frontier policy to one evaluated candidate."""
        if accepted is False:
            return CandidateDisposition.DISCARD, False
        if accepted is not True:
            return CandidateDisposition.UNASSESSED, None
        space = self.options.metric_space
        comparable = space.complete(metrics) if space.objectives else framework_metric
        if not comparable:
            return CandidateDisposition.UNASSESSED, None
        search = HypothesisSearch(_hypothesis_config(self.options))
        conflict = search.pareto_conflict(
            disposition=CandidateDisposition.PARETO_FRONTIER,
            metrics=metrics,
            records=self.state.search.rounds,
            space=space,
        )
        if conflict is not None:
            return CandidateDisposition.DISCARD, False
        return CandidateDisposition.PARETO_FRONTIER, True

    def _history_projection(self) -> str:
        rows = [
            {
                "hypothesis_id": item.hypothesis_id,
                "title": item.plan.title,
                "phase": item.phase.value,
                "outcome": (
                    item.implementation.outcome.value if item.implementation is not None else None
                ),
                "next_step": (
                    item.implementation.next_step if item.implementation is not None else ""
                ),
                "revision": _bounded_optional(
                    item.candidate_revision,
                    _MAX_HISTORY_REVISION_CHARS,
                ),
                "evidence": [ref.model_dump(mode="json") for ref in _evidence(item)],
                "evaluation": _compact_evaluation(item.evaluation),
            }
            for item in self.state.workstreams[-_MAX_HISTORY_ROWS:]
        ]
        return json.dumps(rows, separators=(",", ":"))

    def _root_revision(self) -> str:
        revision = self.run.workspaces.root.revision
        if revision is None:
            message = "dynamic orchestration requires a recorded root revision"
            raise RuntimeError(message)
        return revision

    def _index(self, hypothesis_id: str) -> int:
        return next(
            index
            for index, item in enumerate(self.state.workstreams)
            if item.hypothesis_id == hypothesis_id
        )

    async def _commit(self, *, workspace: bool = False, label: str) -> None:
        self.state.experiment_revision += 1
        await self.run.state.commit(
            self.state,
            workspace=self.run.workspaces.root if workspace else None,
            label=label,
        )


def _bind_evidence_revision(result: ImplementerResult, revision: str) -> ImplementerResult:
    evidence = tuple(
        reference
        if reference.revision is not None
        else reference.model_copy(update={"revision": revision})
        for reference in result.evidence
    )
    return result.model_copy(update={"evidence": evidence})


def _evidence(workstream: DynamicWorkstream) -> tuple[EvidenceReference, ...]:
    if workstream.implementation is not None:
        return workstream.implementation.evidence
    return workstream.plan.evidence


def _references_text(references: Sequence[EvidenceReference]) -> str:
    if not references:
        return "[]"
    return json.dumps(
        [item.model_dump(mode="json") for item in references],
        separators=(",", ":"),
    )


def _evaluation_feedback(result: EvaluationResult) -> str:
    """Render trusted gate failures as compact correction guidance."""
    feedback = [
        message
        for passed, message in (
            (result.local_validation_passed, result.local_validation_feedback),
            (result.accuracy_passed, result.accuracy_feedback),
            (result.benchmark_passed, result.benchmark_feedback),
        )
        if passed is False and message
    ]
    return "Trusted evaluation failed: " + "; ".join(feedback or ["no feedback provided"])


def _compact_evaluation(result: EvaluationResult | None) -> dict[str, object] | None:
    """Project bounded decision facts without copying command output into prompts."""
    if result is None:
        return None
    metric_names = sorted(result.metrics)[:_MAX_HISTORY_METRICS]
    return {
        "accepted": result.accepted,
        "local_validation_passed": result.local_validation_passed,
        "accuracy_passed": result.accuracy_passed,
        "benchmark_passed": result.benchmark_passed,
        "metric_name": _bounded_optional(result.metric_name, _MAX_HISTORY_METRIC_NAME_CHARS),
        "metric_value": result.metric_value,
        "metric_direction": (
            result.metric_direction.value if result.metric_direction is not None else None
        ),
        "metric_unit": _bounded_optional(result.metric_unit, _MAX_HISTORY_METRIC_UNIT_CHARS),
        "metrics": {
            name[:_MAX_HISTORY_METRIC_NAME_CHARS]: result.metrics[name] for name in metric_names
        },
    }


def _bounded_optional(value: str | None, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit]


def _hypothesis_config(options: DynamicOptions) -> HypothesisConfig:
    return HypothesisConfig(
        max_rounds=options.max_rounds * options.max_in_flight,
        judge_every=options.judge_every,
        official_eval_every=options.official_eval_every,
        max_retries_per_round=options.max_retries_per_round,
    )


def _orchestrator_plan(plan: WorkstreamPlan, reasoning: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=plan.hypothesis_id,
        hypothesis=plan.hypothesis,
        title=plan.title,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        request_official_evaluation=plan.request_evaluation,
        reasoning=reasoning,
    )


async def orchestrate(run: Run, raw_options: BaseModel) -> RunStatus:
    """Run dynamic portfolio search through the public runtime capability."""
    options = DynamicOptions.model_validate(raw_options)
    dynamic = await _DynamicRun.open(run, options)
    return await dynamic.execute()


__all__ = ["DynamicPlanError", "orchestrate"]
