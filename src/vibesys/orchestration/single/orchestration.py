"""Single-agent hypothesis search over explicit runtime capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.constants import DomainName
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import UnsupportedProfilerError
from vibesys.orchestration.gates import FrameworkBenchmarkOutcome
from vibesys.orchestration.hypothesis import (
    AttemptState,
    Continue,
    Finished,
    HypothesisConfig,
    HypothesisSearch,
    JudgeReviewed,
    NewHypothesis,
    PerformanceProjection,
    RecordInput,
    build_round_record,
)
from vibesys.orchestration.profile_focus import (
    FocusView,
    ProfileFocus,
    ProfileFocusConfig,
    ProfileFocusState,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vibesys.orchestration.single.agents import IMPLEMENTER
from vibesys.orchestration.single.attribution import run_attribution
from vibesys.orchestration.single.combined import CombinedTurnRequest, SingleAgentWorker
from vibesys.orchestration.single.designer import DesignerPlanRequest, request_plan
from vibesys.orchestration.single.files import SingleFiles
from vibesys.orchestration.single.models import (
    PaidAttempt,
    PlanContext,
    ProfileGuidedSingleOptions,
    SingleAgentRoundContext,
    SingleOptions,
    SingleState,
)
from vibesys.profilers import ProfilerKind, profiler_definition
from vs_runtime.api import (
    BenchmarkObjective,
    MetricDirection,
    RunHost,
    RunStatus,
    Workspace,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.hypothesis import (
        CarryOver,
        OrchestratorPlan,
        PlanningContext,
        RollbackTarget,
    )
    from vibesys.orchestration.hypothesis.state import Hypothesis, RoundRecord


@dataclass(slots=True)
class _SelectedRound:
    round_number: int
    plan: OrchestratorPlan
    hypothesis: Hypothesis
    official_reason: str | None
    attempt: AttemptState


SingleRunOptions = SingleOptions | ProfileGuidedSingleOptions
_PROFILE_MEASUREMENT_REASON = "profile-guided component measurement"


def _benchmark_objectives(options: SingleRunOptions) -> tuple[BenchmarkObjective, ...]:
    return tuple(
        BenchmarkObjective(
            name=item.name,
            direction=(
                MetricDirection.MAXIMIZE if item.direction == "max" else MetricDirection.MINIMIZE
            ),
        )
        for item in options.metric_space.objectives
    )


class _SingleRun:
    """One plain or profile-guided run's control state and owned resources."""

    def __init__(self, host: RunHost, options: SingleRunOptions) -> None:
        self.host = host
        self.options = options
        self.workspace = host.workspaces.root
        self.search = HypothesisSearch(
            HypothesisConfig(
                max_rounds=options.max_rounds,
                judge_every=options.judge_every,
                official_eval_every=options.official_eval_every,
                max_retries_per_round=options.max_retries_per_round,
            )
        )
        self.worker = SingleAgentWorker(host, self.search)
        self.files = SingleFiles.open(self.workspace.path, options.memory_layout)
        self.state = SingleState()
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
        self.label_prefix = "profile_single" if profile is not None else "single"

    async def initialize(self) -> None:
        """Recover plugin state and durably initialize policy-owned files."""
        loaded = await self.host.state.load(SingleState)
        aggregate = loaded or SingleState()
        resumed = self.search.resume(aggregate.search, self.options.metric_space)
        self.state = aggregate.model_copy(update={"search": resumed}, deep=True)
        self.carry = self.search.initial_carry(resumed.rounds)
        self.round_number = len(resumed.rounds) + 1
        self.files.write_pareto(self.search.archive_summary(resumed.rounds, space=resumed.metrics))
        await self._commit(
            workspace=self.workspace,
            label=f"{self.label_prefix}: initialize policy state",
        )

    async def run(self) -> RunStatus:
        """Run every remaining round, then select a trusted final workspace."""
        try:
            await self.initialize()
            while self.round_number <= self.options.max_rounds:
                await self.host.control.checkpoint()
                self.host.log(f"round {self.round_number}/{self.options.max_rounds}")
                self.files.write_pareto(
                    self.search.archive_summary(self.records, space=self.state.search.metrics)
                )
                selected = await self._select_round()
                await self._run_attempts(selected)
                await self._close_round(selected)
            await self._finish()
            return RunStatus.SUCCEEDED
        finally:
            await self.worker.close()

    @property
    def records(self) -> list[RoundRecord]:
        """Return completed records in their durable order."""
        return self.state.search.rounds

    async def _commit(
        self,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        await self.host.state.commit(self.state, workspace=workspace, label=label)

    async def _mark_paid(
        self,
        *,
        member_id: str,
        turn_number: int,
    ) -> None:
        self.state = self.state.model_copy(
            update={
                "last_paid_attempt": PaidAttempt(
                    round_number=self.round_number,
                    role_id=IMPLEMENTER.id,
                    member_id=member_id,
                    turn_number=turn_number,
                )
            },
            deep=True,
        )
        await self._commit(
            label=(
                f"{self.label_prefix}: start round {self.round_number} "
                f"{IMPLEMENTER.id} turn {turn_number}"
            )
        )

    async def _select_round(self) -> _SelectedRound:
        decision = self.search.next_round(
            self.state.search,
            round_number=self.round_number,
            records=self.records,
            carry=self.carry,
        )
        if isinstance(decision, Finished):
            message = "hypothesis search finished before the configured round cursor"
            raise TypeError(message)
        if isinstance(decision, NewHypothesis):
            guidance = await self._prepare_profile_guidance()
            plan = await request_plan(
                self.host,
                self.search,
                DesignerPlanRequest(
                    round_number=self.round_number,
                    state=self.state.search,
                    context=self._plan_context(decision.context, guidance),
                    workspace=self.workspace,
                ),
            )
            current_revision = self.workspace.revision
            started = self.search.start(
                self.state.search,
                plan,
                round_number=self.round_number,
                current_commit=current_revision,
                records=self.records,
            )
            self.state = self.state.model_copy(update={"search": started.state}, deep=True)
            self.files.write_plan(self.round_number, plan)
            self.files.note_plan(self.round_number, plan)
            await self._commit(
                workspace=self.workspace,
                label=f"{self.label_prefix}: start hypothesis {plan.hypothesis_id}",
            )
            hypothesis = started.hypothesis
            if started.rollback is not None:
                hypothesis = await self._apply_rollback(hypothesis, started.rollback)
        else:
            assert isinstance(decision, Continue)  # noqa: S101  # lint-waiver: LW-920441 [S101]; the exhaustive decision branch narrows the closed policy result type.
            hypothesis = decision.hypothesis
            plan = hypothesis.plan
            self.files.note_continuation(
                self.round_number,
                hypothesis.hypothesis_id,
                hypothesis.next_step or plan.task,
            )
            self.host.log(
                f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
            )
        official_reason = self.search.official_due(
            records=self.records,
            round_number=self.round_number,
            requested=plan.request_official_evaluation,
            candidate_ready=True,
        )
        focus_state = self._focus_state()
        if focus_state is not None and focus_state.active_component:
            official_reason = official_reason or _PROFILE_MEASUREMENT_REASON
        attempt = AttemptState(
            agent_run_state=self.state.search,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return _SelectedRound(
            round_number=self.round_number,
            plan=plan,
            hypothesis=hypothesis,
            official_reason=official_reason,
            attempt=attempt,
        )

    async def _apply_rollback(
        self,
        hypothesis: Hypothesis,
        rollback: RollbackTarget,
    ) -> Hypothesis:
        if not rollback.resolved or rollback.commit is None:
            self.host.log("cannot revert: requested round has no retained revision")
            return hypothesis
        if not await self.workspace.try_restore(rollback.commit, clean=True):
            self.host.log(f"cannot restore requested revision {rollback.commit}")
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
            and marker.role_id == IMPLEMENTER.id
            and marker.member_id == selected.plan.hypothesis_id
        ):
            return marker.turn_number + 1
        return 1

    async def _run_attempts(self, selected: _SelectedRound) -> None:
        first = self._first_attempt(selected)
        if first > self.options.max_retries_per_round:
            message = (
                f"round {self.round_number} exhausted its "
                f"{self.options.max_retries_per_round} paid attempts"
            )
            raise RuntimeError(message)
        for retry in range(first, self.options.max_retries_per_round + 1):
            selected.attempt.retry = retry
            selected.attempt.official_reason = None
            await self._mark_paid(
                member_id=selected.plan.hypothesis_id,
                turn_number=retry,
            )
            response = await self.worker.turn(
                CombinedTurnRequest(
                    round_number=self.round_number,
                    plan=selected.plan,
                    attempt=selected.attempt,
                    records=tuple(self.records),
                    context=self._combined_context(selected),
                    workspace=self.workspace,
                )
            )
            self.files.write_plan(self.round_number, selected.plan)
            selected.attempt.single_agent_response = response
            selected.attempt.judge = JudgeReviewed(response.verdict.value)
            self.files.note_response(self.round_number, retry, response)
            if response.verdict is Verdict.FAIL:
                selected.attempt.feedback = response.feedback
                selected.hypothesis.feedback = response.feedback
                await self._checkpoint_hypothesis(selected)
                continue
            official_reason = self.search.official_due(
                records=self.records,
                round_number=selected.round_number,
                requested=selected.plan.request_official_evaluation,
                candidate_ready=True,
            )
            if official_reason is None:
                selected.attempt.passed = True
                self.files.note_evaluation(
                    self.round_number,
                    retry,
                    "- decision: deferred\n- reason: cadence not due\n",
                )
                return
            selected.attempt.official_reason = official_reason
            if await self._official_evaluation(selected):
                return

    async def _checkpoint_hypothesis(self, selected: _SelectedRound) -> None:
        updated = self.search.update_active(
            selected.attempt.agent_run_state,
            selected.hypothesis,
        )
        selected.attempt.agent_run_state = updated
        self.state = self.state.model_copy(update={"search": updated}, deep=True)
        await self._commit(
            workspace=self.workspace,
            label=(f"{self.label_prefix}: checkpoint hypothesis {selected.plan.hypothesis_id}"),
        )

    async def _official_evaluation(self, selected: _SelectedRound) -> bool:
        revision = await self.workspace.snapshot(
            f"round-{self.round_number}-attempt-{selected.attempt.retry}-evaluation"
        )
        receipt = self.state.accuracy_receipt
        reuse = receipt if receipt is not None and receipt.revision == revision else None
        accuracy = await self.host.evaluation.accuracy(self.workspace, reuse=reuse)
        self.state = self.state.model_copy(update={"accuracy_receipt": accuracy.receipt}, deep=True)
        if not accuracy.passed:
            await self._evaluation_failed(selected, accuracy.feedback or "accuracy failed")
            return False
        benchmark = await self.host.evaluation.benchmark(
            self.workspace,
            objectives=_benchmark_objectives(self.options),
        )
        selected.attempt.framework_benchmark = FrameworkBenchmarkOutcome(
            feedback=benchmark.feedback,
            metric_name=benchmark.metric_name,
            metric_value=benchmark.metric_value,
            metric_direction=(
                benchmark.metric_direction.value if benchmark.metric_direction is not None else None
            ),
            metric_unit=benchmark.metric_unit,
            row=benchmark.row,
        )
        selected.attempt.framework_perf_metric = benchmark.metric_value
        if not benchmark.passed:
            await self._evaluation_failed(selected, benchmark.feedback or "benchmark failed")
            return False
        selected.attempt.passed = True
        self.files.note_evaluation(
            self.round_number,
            selected.attempt.retry,
            f"- decision: passed\n- reason: {selected.official_reason}\n",
        )
        return True

    async def _evaluation_failed(self, selected: _SelectedRound, feedback: str) -> None:
        selected.attempt.feedback = feedback
        selected.attempt.revalidation_required = True
        hypothesis = selected.hypothesis
        hypothesis.gate_revalidation_pending = True
        hypothesis.gate_candidate_commit = self.workspace.revision
        hypothesis.gate_accuracy_passed = self.state.accuracy_receipt is not None
        hypothesis.feedback = feedback
        self.files.note_evaluation(
            self.round_number,
            selected.attempt.retry,
            f"- decision: failed\n- feedback: {feedback}\n",
        )
        await self._checkpoint_hypothesis(selected)

    async def _close_round(self, selected: _SelectedRound) -> None:
        attempt = selected.attempt
        response = attempt.single_agent_response
        official = attempt.passed and attempt.official_reason is not None
        if response is not None and attempt.framework_perf_metric is not None and official:
            response.perf_metric = attempt.framework_perf_metric
            response.perf_unit = attempt.framework_benchmark.metric_name
        metric = response.perf_metric if response is not None and official else None
        projection = PerformanceProjection(
            metric=metric,
            unit=response.perf_unit if response is not None and official else None,
            provenance=(
                "framework"
                if metric is not None and attempt.framework_perf_metric is not None
                else "implementer"
                if metric is not None
                else None
            ),
            profile_skipped=response is None or response.perf_metric is None,
            accepted_metrics={},
            accepted_evaluation_artifact=None,
            next_single_response=response,
        )
        candidate_revision = await self.workspace.snapshot(
            f"round-{self.round_number}-record-input"
        )
        binding = self.worker.binding(selected.plan.hypothesis_id)
        record = build_round_record(
            RecordInput(
                state=attempt.agent_run_state,
                records=self.records,
                round_number=self.round_number,
                hypothesis=selected.hypothesis,
                plan=selected.plan,
                attempt=attempt,
                projection=projection,
                reviewed=True,
                framework_benchmark_configured=self.host.facts.benchmark_configured,
                accuracy_configured=self.host.facts.accuracy_configured,
                candidate_commit=candidate_revision,
                backend_name=binding.backend,
                driver_name=binding.driver,
                provider=binding.provider,
                model=binding.model,
            )
        )
        state = self.state.search
        if self.profile_focus is not None:
            official = record.official_evaluation
            relative_improvement = (
                record.perf_delta_pct / 100 if record.perf_delta_pct is not None else None
            )
            focused = self.profile_focus.record(
                self._require_focus_state(),
                round_number=record.round_number,
                passed=attempt.passed and official,
                relative_improvement=relative_improvement,
            )
            state = state.model_copy(update={"profile_guidance": focused}, deep=True)
        closed = self.search.close_round(
            state,
            hypothesis=selected.hypothesis,
            record=record,
            records=self.records,
            carry=self.carry,
            passed=attempt.passed,
            reviewed=True,
            feedback=attempt.feedback,
            keeps_active=False,
            requests_continuation=False,
            next_step=None,
            terminal_needs_parent_choice=False,
        )
        self.state = self.state.model_copy(
            update={
                "search": closed.state,
                "last_paid_attempt": None,
                "last_response": response,
            },
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
                message = "single-agent run has no trusted input baseline"
                raise RuntimeError(message)
            await self.workspace.restore(baseline, clean=True)
            await self.workspace.snapshot(f"{self.label_prefix}: restore trusted input baseline")
            self.host.log("no trusted winner; restored the input baseline")
            return
        if winner.commit is None:
            message = "selected single-agent winner has no workspace revision"
            raise RuntimeError(message)
        await self.workspace.retain(
            winner.commit,
            label=f"selected-round-{winner.round_number:04d}",
        )
        await self.workspace.restore(winner.commit, clean=True)
        await self.workspace.snapshot(f"{self.label_prefix}: select round {winner.round_number}")
        self.host.log(f"selected trusted winner from round {winner.round_number}")

    def _domain_context(self) -> dict[str, object]:
        facts = self.host.facts
        return {
            "modality": self.options.modality,
            "interface": self.options.interface,
            "reference_path": facts.reference_location,
            "benchmark_command": facts.benchmark_command,
            "accuracy_command": facts.accuracy_command,
            "runtime_notes": facts.environment_notes,
            "profile_execution": facts.profile_execution.value,
            "workspace_sources": tuple(item.model_dump() for item in facts.workspace_sources),
        }

    def _plan_context(
        self,
        context: PlanningContext,
        guidance: FocusView | None,
    ) -> PlanContext:
        facts = self.host.facts
        domain = resolve_domain(DomainName(facts.domain_id))
        last = self.state.last_response
        summary = (
            ProfilerSummary(
                analysis=last.profile_analysis,
                bottlenecks=last.bottlenecks,
                suggestions=last.suggestions,
                perf_metric=last.perf_metric,
                perf_unit=last.perf_unit,
            )
            if last is not None
            else None
        )
        return PlanContext(
            objective_location=facts.objective_location,
            profiler_summary=summary,
            regression_info=context.carry.regression_info,
            exhaustion_info=context.carry.exhaustion_info,
            progress_location=self.files.progress_location,
            roadmap_location=self.files.roadmap_location,
            pareto_archive_location=self.files.pareto_location,
            plateau_warning=context.plateau_warning,
            domain_orchestrator=render_domain_section(
                domain, DomainRole.ORCHESTRATOR, **self._domain_context()
            ),
            runtime_notes=facts.environment_notes,
            framework_benchmark_enabled=facts.benchmark_configured,
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=context.provisional_candidates,
            official_eval_cadence_due=(
                context.provisional_candidates + 1 >= self.options.official_eval_every
            ),
            active_component=guidance.active_component if guidance is not None else None,
            ledger_text=guidance.ledger_text if guidance is not None else None,
            ranked_bottlenecks=(
                [
                    {
                        "component": item.name,
                        "cost_share": item.share * 100,
                        "evidence": item.evidence,
                    }
                    for item in guidance.ranked_bottlenecks
                ]
                if guidance is not None
                else []
            ),
        )

    def _focus_state(self) -> ProfileFocusState | None:
        if self.profile_focus is None:
            return None
        return self.state.search.profile_guidance or self.profile_focus.initial()

    def _require_focus_state(self) -> ProfileFocusState:
        state = self._focus_state()
        if state is None:
            message = "profile focus state requested for a plain single-agent run"
            raise RuntimeError(message)
        return state

    async def _prepare_profile_guidance(self) -> FocusView | None:
        profile = self.options.profile_guided
        if profile is None or self.profile_focus is None:
            return None
        attribution = await run_attribution(
            self.host,
            profile,
            workspace=self.workspace,
        )
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

    def _combined_context(self, selected: _SelectedRound) -> SingleAgentRoundContext:
        facts = self.host.facts
        domain = resolve_domain(DomainName(facts.domain_id))
        profiler_kind = ProfilerKind(facts.profiler_id)
        profiler = (
            None if profiler_kind is ProfilerKind.NONE else profiler_definition(profiler_kind)
        )
        if (
            profiler is not None
            and profiler.requires_domain_torch_support
            and not domain.supports_torch_profiler
        ):
            raise UnsupportedProfilerError
        plan_location = self.files.write_plan(self.round_number, selected.plan)
        domain_context = self._domain_context()
        return SingleAgentRoundContext(
            domain_single_agent=render_domain_section(
                domain, DomainRole.SINGLE_AGENT, **domain_context
            ),
            domain_profiler=render_domain_section(domain, DomainRole.PROFILER, **domain_context),
            interface=self.options.interface,
            objective_location=facts.objective_location,
            plan_artifact_location=plan_location,
            progress_location=self.files.progress_location,
            pareto_archive_location=self.files.pareto_location,
            validation_location=self.files.validation_location,
            feedback=selected.attempt.feedback,
            profiler_kind=facts.profiler_id,
            profiler_support_name=profiler.support_name if profiler is not None else None,
            benchmark_command=facts.benchmark_command,
            accuracy_command=facts.accuracy_command,
            runtime_notes=facts.environment_notes,
            official_evaluation_due=selected.official_reason is not None,
            official_evaluation_reason=selected.official_reason,
        )


async def orchestrate(host: RunHost, raw_options: BaseModel) -> RunStatus:
    """Run the plain single-agent policy against the explicit runtime API."""
    options = SingleOptions.model_validate(raw_options)
    return await _SingleRun(host, options).run()


async def orchestrate_profile_guided(host: RunHost, raw_options: BaseModel) -> RunStatus:
    """Run profile-guided single-agent search against the explicit runtime API."""
    options = ProfileGuidedSingleOptions.model_validate(raw_options)
    return await _SingleRun(host, options).run()


__all__ = ["orchestrate", "orchestrate_profile_guided"]
