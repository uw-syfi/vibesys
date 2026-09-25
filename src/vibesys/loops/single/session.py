"""Durable round control for the single strategy over public host capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.agent_run import issue_board
from vibesys.errors import StrategySessionError
from vibesys.loops.single.turns import SingleAgentTurns
from vibesys.orchestration.runtime import WorkspaceRestoreError
from vibesys.roles.common import Verdict
from vibesys.roles.profiler import ProfilerSummary
from vibesys.search.hypothesis import HypothesisConfig, HypothesisSearch
from vibesys.search.hypothesis.attempts import (
    AttemptDecision,
    AttemptState,
    JudgeReviewed,
    PerformanceProjection,
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
from vs_agent.api import RoundProgress
from vs_loop_state.api import RoundHistory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.loops.agent_options import AgentOrchestrationOptions
    from vibesys.orchestration.runtime import RunContext
    from vibesys.search.hypothesis.plan import OrchestratorPlan
    from vibesys.search.hypothesis.state import Hypothesis, RoundRecord


SingleSessionError = StrategySessionError


class PlanGuidance(Protocol):
    """Prompt data exposed by this strategy's hypothesis selection."""

    def plan_prompt_context(self) -> dict[str, object]:
        """Return optional plan template variables."""
        ...


@dataclass(frozen=True)
class _StaticGuidance:
    """Fixed no-op guidance: the plain single strategy renders no extras."""

    def plan_prompt_context(self) -> dict[str, object]:
        """Return no optional plan variables."""
        return {}


STATIC_GUIDANCE = _StaticGuidance()


@dataclass(frozen=True)
class PlanRequest:
    """Evidence supplied to this strategy's designer role."""

    round_number: int
    state: HypothesisState
    records: list[RoundRecord]
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: PlanGuidance


@dataclass(frozen=True)
class AttemptRequest:
    """The selected plan and evidence for one combined role turn."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    last_profile_focus: str


@dataclass
class SingleRound:
    """One selected hypothesis's turn facts and the mutable attempt state."""

    request: AttemptRequest
    attempt: AttemptState


class _TerminalPolicy:
    """Single role policy facts consumed by the pure round transition."""

    def project_performance(
        self, _request: AttemptRequest, state: AttemptState
    ) -> PerformanceProjection:
        response = state.single_agent_response
        official = state.passed and state.official_reason is not None
        provenance = None
        if response is not None and state.framework_perf_metric is not None and official:
            response.perf_metric = state.framework_perf_metric
            response.perf_unit = state.framework_benchmark.metric_name
            provenance = "framework"
        metric = response.perf_metric if response is not None and official else None
        unit = response.perf_unit if response is not None and official else None
        if metric is not None and provenance is None:
            provenance = "implementer"
        return PerformanceProjection(
            metric=metric,
            unit=unit,
            provenance=provenance,
            profile_skipped=response is None or response.perf_metric is None,
            accepted_metrics={},
            accepted_evaluation_artifact=None,
            next_single_response=response,
        )


class SingleSession:
    """Own the single strategy's state, roles, attempts, and checkpoints."""

    def __init__(self, ctx: RunContext, options: AgentOrchestrationOptions) -> None:
        """Bind one host; resource opening occurs in ``open``."""
        self.ctx = ctx
        self.options = options
        self.workspace = ctx.workspaces.root
        self.turns = SingleAgentTurns(ctx, options)
        self._gate_recorder = issue_board.GateBoardRecorder(self.turns.progress_path)
        self.search = HypothesisSearch(
            HypothesisConfig(
                max_rounds=options.max_rounds,
                judge_every=options.judge_every,
                official_eval_every=options.official_eval_every,
                max_retries_per_round=options.max_retries_per_round,
            )
        )
        self.terminal_policy = _TerminalPolicy()
        self.framework_benchmark_configured = (
            ctx.request.input_bundle.benchmark_result is not None
            or ctx.request.input_bundle.benchmark_result_protocol is not None
        )

    @classmethod
    async def open(cls, ctx: RunContext, options: AgentOrchestrationOptions) -> SingleSession:
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
        self.last_response = None
        self.last_profile_focus = "general latency hotspots on /v1/completions"
        if previous != state:
            await self.ctx.state.commit(
                sequence=self.round_number,
                writes={"state.json": state},
                candidate=False,
                label="single: initialize policy state",
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

    async def select_hypothesis(self) -> SingleRound:
        """Choose a designer plan or continue the active hypothesis."""
        number = self.round_number
        decision = self.search.next_round(
            self.state, round_number=number, records=self.records, carry=self.carry
        )
        if isinstance(decision, Finished):
            raise SingleSessionError.missing_active()
        if isinstance(decision, NewHypothesis):
            context = decision.context
            summary = self._previous_profile()
            plan = await self.turns.plan(
                PlanRequest(
                    round_number=context.round_number,
                    state=self.state,
                    records=list(context.records),
                    carry=context.carry,
                    profiler_summary=summary,
                    plateau_warning=context.plateau_warning,
                    provisional_candidates=context.provisional_candidates,
                    profile_guidance=STATIC_GUIDANCE,
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
                label=f"single: start hypothesis {plan.hypothesis_id}",
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
        await self._apply_rollback(hypothesis)
        request = AttemptRequest(
            round_number=number,
            plan=plan,
            planned_official_reason=reason,
            records=self.records,
            active_hypothesis=hypothesis,
            last_profile_focus=self.last_profile_focus,
        )
        attempt = AttemptState(
            agent_run_state=self.state,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return SingleRound(request=request, attempt=attempt)

    def _previous_profile(self) -> ProfilerSummary | None:
        response = self.last_response
        if response is None:
            return None
        return ProfilerSummary(
            analysis=response.profile_analysis,
            bottlenecks=response.bottlenecks,
            suggestions=response.suggestions,
            perf_metric=response.perf_metric,
            perf_unit=response.perf_unit,
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
            raise SingleSessionError.missing_rollback()
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
            label=f"single: set hypothesis {hypothesis.hypothesis_id} parent",
            publish=self.state,
        )
        if failed_child is None:
            self.ctx.log(f"Reverted workspace to round {parent_round} ({rollback[:8]}).")
        else:
            self.ctx.log(
                f"Reverted workspace to the pre-hypothesis parent of failed round "
                f"{failed_child} ({rollback[:8]}), based on parent round {parent_round}."
            )

    def remaining_attempts(self, selected: SingleRound) -> range:
        """Resume after the last durable paid attempt marker."""
        first = issue_board.next_implementer_attempt(
            self.turns.progress_path, selected.request.round_number
        )
        limit = self.options.max_retries_per_round
        if first > limit:
            raise SingleSessionError.exhausted_attempts(self.round_number, first, limit)
        if first > 1:
            self.ctx.log(f"[resume] round {self.round_number} continues at attempt {first}/{limit}")
        return range(first, limit + 1)

    async def begin_attempt(self, selected: SingleRound, retry: int) -> None:
        """Persist attempt state before the combined agent's paid turn."""
        self.ctx.log(f"\n--- attempt {retry}/{self.options.max_retries_per_round} ---\n")
        attempt = selected.attempt
        attempt.retry = retry
        attempt.official_reason = None
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": attempt.agent_run_state},
            candidate=False,
            label=f"single: start round {self.round_number} attempt {retry}",
            publish=attempt.agent_run_state,
        )
        # The paid-work marker is written by `turns.combined`'s `before_paid`
        # hook and committed by `ctx.agents.turn`'s pre-turn snapshot
        # (label "...-single-agent-input"), so it lands in git durably
        # before the paid call starts. See the shared design brief, item 3.
        await self.ctx.environment.reselect_device()

    async def combined_turn(self, selected: SingleRound) -> AttemptDecision:
        """Run one combined turn and choose retry, finish, or official gates."""
        attempt = selected.attempt
        response = await self.turns.combined(selected.request, attempt)
        attempt.single_agent_response = response
        attempt.judge = JudgeReviewed(response.verdict.value)
        if response.verdict is Verdict.FAIL:
            attempt.feedback = response.feedback
            selected.request.active_hypothesis.feedback = response.feedback
            await self._checkpoint_hypothesis(selected)
            return AttemptDecision.RETRY
        reason = self.search.official_due(
            records=self.records,
            round_number=selected.request.round_number,
            requested=selected.request.plan.request_official_evaluation,
            candidate_ready=True,
        )
        if reason is None:
            self._record_official_decision(selected, run=False, reason="cadence_not_due")
            self.ctx.log("[official-evaluation] deferred; candidate retained as provisional")
            attempt.passed = True
            return AttemptDecision.FINISH
        attempt.official_reason = reason
        return AttemptDecision.OFFICIAL

    async def official_gates(self, selected: SingleRound) -> bool:
        """Evaluate a candidate only after the combined role passes itself."""
        return await self._official_gates(selected)

    def _record_official_decision(self, selected: SingleRound, *, run: bool, reason: str) -> None:
        issue_board.append_official_evaluation_decision(
            self.turns.progress_path,
            self.round_number,
            selected.attempt.retry,
            run=run,
            reason=reason,
            official_eval_every=self.options.official_eval_every,
            provisional_candidates=provisional_candidates_since_official(self.records),
        )

    async def _official_gates(self, selected: SingleRound) -> bool:
        attempt = selected.attempt
        reason = attempt.official_reason
        if reason is None:
            raise SingleSessionError.missing_gate_reason()
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

    async def _checkpoint_hypothesis(self, selected: SingleRound) -> None:
        state = update_active_hypothesis(
            selected.attempt.agent_run_state, selected.request.active_hypothesis
        )
        selected.attempt.agent_run_state = state
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": state},
            candidate=False,
            label=f"single: checkpoint hypothesis {selected.request.plan.hypothesis_id}",
            publish=state,
        )

    async def commit_round(self, selected: SingleRound) -> None:
        """Commit the completed round and publish its observable record."""
        attempt = selected.attempt
        hypothesis = selected.request.active_hypothesis
        projection = self.terminal_policy.project_performance(selected.request, attempt)
        self.last_response = projection.next_single_response
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
                reviewed=True,
                framework_benchmark_configured=self.framework_benchmark_configured,
                accuracy_configured=bool(self.ctx.environment.view.paths.accuracy_command),
                candidate_commit=candidate_commit,
                backend_name=worker.backend_name,
                driver_name=worker.driver_name,
                provider=worker.provider,
                model=worker.model,
            )
        )
        closed = self.search.close_round(
            self.state,
            hypothesis=hypothesis,
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
        if closed.exhaustion_feedback is not None:
            issue_board.append_exhaustion_note(
                self.turns.progress_path,
                self.round_number,
                self.options.max_retries_per_round,
                closed.exhaustion_feedback,
            )
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": closed.state},
            publish=closed.state,
        )
        self.state = closed.state
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
                raise SingleSessionError.missing_baseline()
            await self.workspace.restore(baseline, clean=True)
            await self.workspace.snapshot("single: restore trusted input baseline")
            self.ctx.log(f"\nNo evaluated winner was retained. Restored baseline {baseline[:12]}.")
            return True
        if winner.commit is None:
            raise SingleSessionError.missing_winner_commit()
        await self.workspace.retain(f"selected-round-{winner.round_number:04d}", winner.commit)
        await self.workspace.restore(winner.commit, clean=True)
        await self.workspace.snapshot(f"single: select round {winner.round_number}")
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
