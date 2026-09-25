"""Durable round control for the single strategy over public host capabilities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import (
    AttemptDecision,
    AttemptState,
    JudgeReviewed,
    PerformanceProjection,
)
from vibesys.agent_run.errors import StrategySessionError
from vibesys.agent_run.evidence import (
    _FAILED_HYPOTHESIS_OUTCOMES,
    CarryOver,
    _detect_plateau,
    _format_metric_row,
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
from vibesys.loops.single.hypothesis import HypothesisEngine
from vibesys.loops.single.turns import SingleAgentTurns
from vibesys.orchestration.runtime import WorkspaceRestoreError
from vibesys.roles.common import Verdict
from vibesys.roles.profiler import ProfilerSummary
from vs_agent.api import RoundProgress
from vs_loop_state.api import RoundHistory

_MAX_CONTINUATION_ROUNDS = 2

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.agent_run.options import AgentOrchestrationOptions
    from vibesys.agent_run.state import Hypothesis
    from vibesys.orchestration.runtime import RunContext
    from vibesys.schemas import OrchestratorPlan
    from vs_loop_state.api import RoundRecord


SingleSessionError = StrategySessionError


class PlanGuidance(Protocol):
    """Prompt data exposed by this strategy's hypothesis selection."""

    def plan_prompt_context(self) -> dict[str, object]:
        """Return optional plan template variables."""
        ...


@dataclass(frozen=True)
class PlanRequest:
    """Evidence supplied to this strategy's designer role."""

    round_number: int
    state: AgentRunState
    records: list[RoundRecord]
    carry: CarryOver
    profiler_summary: ProfilerSummary | None
    plateau_warning: str | None
    provisional_candidates: int
    profile_guidance: PlanGuidance


@dataclass(frozen=True)
class RoundSelection:
    """The hypothesis and plan chosen for one round."""

    state: AgentRunState
    hypothesis: Hypothesis
    plan: OrchestratorPlan
    planned_official_reason: str | None


@dataclass(frozen=True)
class AttemptRequest:
    """The selected plan and evidence for one combined role turn."""

    round_number: int
    plan: OrchestratorPlan
    planned_official_reason: str | None
    records: list[RoundRecord]
    active_hypothesis: Hypothesis
    last_profile_focus: str


@dataclass(frozen=True)
class SingleRound:
    """One selected hypothesis and the mutable attempt state for its round."""

    selection: RoundSelection
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
        previous = await ctx.state.load(AgentRunState)
        state = adopt_metric_space(previous or AgentRunState(), self.options.metric_space)
        self.state = state
        self.records = state.rounds
        self.history = RoundHistory(records=self.records)
        self.carry = CarryOver(regression_info=_terminal_workspace_notice(self.records))
        self.round_number = len(self.records) + 1
        self.last_response = None
        self.last_profile_focus = "general latency hotspots on /v1/completions"
        self.engine = HypothesisEngine.create(state)
        if previous != state:
            await self.ctx.state.commit(
                sequence=self.round_number,
                writes={"state.json": state},
                candidate=False,
                label="single: initialize policy state",
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

    async def select_hypothesis(self) -> SingleRound:
        """Choose a designer plan or continue the active hypothesis."""
        state = self.state
        engine = self.engine
        hypothesis = state.active_hypothesis
        number = self.round_number
        if hypothesis is None:
            provisional = _provisional_candidates_since_official(self.records)
            summary = self._previous_profile()
            plan = await self.turns.plan(
                PlanRequest(
                    round_number=number,
                    state=state,
                    records=self.records,
                    carry=self.carry,
                    profiler_summary=summary,
                    plateau_warning=_detect_plateau(self.records),
                    provisional_candidates=provisional,
                    profile_guidance=engine.guidance,
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
                raise SingleSessionError.missing_active()
            await self.ctx.state.commit(
                sequence=self.round_number,
                writes={"state.json": state},
                candidate=False,
                label=f"single: start hypothesis {plan.hypothesis_id}",
                publish=state,
            )
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
        reason = self._official_reason(requested=plan.request_official_evaluation)
        selected = RoundSelection(
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
            last_profile_focus=self.last_profile_focus,
        )
        attempt = AttemptState(
            agent_run_state=self.state,
            feedback=hypothesis.feedback,
            revalidation_required=hypothesis.gate_revalidation_pending,
        )
        return SingleRound(selected, request, attempt)

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

    def _official_reason(self, *, requested: bool) -> str | None:
        """Apply this strategy's accepted-candidate evaluation cadence."""
        if self.round_number == self.options.max_rounds:
            return "final_round"
        if requested:
            return "orchestrator_request"
        if (
            _provisional_candidates_since_official(self.records) + 1
            >= self.options.official_eval_every
        ):
            return "cadence"
        return None

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
        state = update_active_hypothesis(selection.state, hypothesis)
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": state},
            candidate=False,
            label=f"single: set hypothesis {hypothesis.hypothesis_id} parent",
            publish=state,
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
        attempt.judge = JudgeReviewed(response.verdict)
        if response.verdict is Verdict.FAIL:
            attempt.feedback = response.feedback
            selected.request.active_hypothesis.feedback = response.feedback
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
            return AttemptDecision.RETRY
        reason = self._official_reason(requested=selected.request.plan.request_official_evaluation)
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
            provisional_candidates=_provisional_candidates_since_official(self.records),
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
        return False

    async def commit_round(self, selected: SingleRound) -> None:
        """Commit the completed round and publish its observable record."""
        attempt = selected.attempt
        projection = self.terminal_policy.project_performance(selected.request, attempt)
        self.last_response = projection.next_single_response
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
        engine, carry, exhaustion_feedback = self.complete_policy_round(selected, record)
        if exhaustion_feedback is not None:
            issue_board.append_exhaustion_note(
                self.turns.progress_path,
                self.round_number,
                self.options.max_retries_per_round,
                exhaustion_feedback,
            )
        await self.ctx.state.commit(
            sequence=self.round_number,
            writes={"state.json": engine.state},
            publish=engine.state,
        )
        self.engine = engine
        self.state = engine.state
        self.records = engine.state.rounds
        self.history = RoundHistory(records=self.records)
        self.carry = carry
        self.round_number += 1

    def complete_policy_round(
        self, selected: SingleRound, record: RoundRecord
    ) -> tuple[HypothesisEngine, CarryOver, str | None]:
        """Advance a reviewed single-role hypothesis and the next designer handoff."""
        attempt = selected.attempt
        next_active = selected.selection.hypothesis.clone()
        if attempt.passed:
            active = None
        elif next_active.continuation_rounds < _MAX_CONTINUATION_ROUNDS:
            next_active.feedback = attempt.feedback
            next_active.next_step = None
            next_active.continuation_rounds += 1
            active = next_active
        else:
            active = None
        engine = self.engine.replace_state(attempt.agent_run_state).complete_round(
            record, next_active=active
        )
        carry = CarryOver()
        exhaustion_feedback: str | None = None
        if not attempt.passed:
            exhaustion_feedback = attempt.feedback or ""
            carry.exhaustion_info = (
                f"Round {record.round_number} did not pass after "
                f"{self.options.max_retries_per_round} attempts. Last judge feedback: "
                f"{attempt.feedback or '(empty)'}"
            )
        elif record.official_evaluation and record.candidate_retained is False:
            carry.regression_info = (
                f"Round {record.round_number}'s official candidate was not retained: "
                f"{record.perf_metric}"
                f"{(' ' + record.perf_unit) if record.perf_unit else ''}. "
                "Use its recorded parent and objective directions when choosing "
                "the next checkpoint."
            )
        return engine, carry, exhaustion_feedback

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
