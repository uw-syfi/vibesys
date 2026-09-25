"""Durable round control for the single strategy over public host capabilities."""

from __future__ import annotations

import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from vibesys.agent_run import issue_board
from vibesys.agent_run.attempts import (
    AttemptDecision,
    AttemptState,
    JudgeReviewed,
    PerformanceProjection,
)
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
from vibesys.evaluators.gates import (
    GATE_RECORD_TAIL_CHARS,
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)
from vibesys.events import (
    CoreEventType,
    EventStatus,
    ExperimentsChangedData,
    RoundFinishedData,
    RunConfiguredData,
)
from vibesys.loops.single.hypothesis import HypothesisEngine
from vibesys.loops.single.turns import SingleAgentTurns
from vibesys.orchestration.runtime import MeasurementOptions
from vibesys.render.sink import output_sink
from vibesys.schemas import ProfilerSummary, Verdict
from vs_agent.api import RoundProgress
from vs_loop_state.api import RoundHistory

_MAX_CONTINUATION_ROUNDS = 2

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vibesys.agent_run.options import AgentOrchestrationOptions
    from vibesys.agent_run.state import Hypothesis
    from vibesys.events import ExperimentsChangeReason
    from vibesys.orchestration.runtime import RunContext
    from vibesys.schemas import OrchestratorPlan
    from vs_loop_state.api import RoundRecord


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


class SingleSessionError(RuntimeError):
    """A durable single strategy invariant failed."""

    def __init__(self, detail: str) -> None:
        """Preserve the precise failed invariant."""
        super().__init__(detail)

    @classmethod
    def missing_active(cls) -> SingleSessionError:
        """Report that the accepted plan produced no active state."""
        return cls("designer plan did not create an active hypothesis")

    @classmethod
    def missing_rollback(cls) -> SingleSessionError:
        """Report an incomplete rollback decision."""
        return cls("rollback resolution omitted a commit")

    @classmethod
    def exhausted_attempts(cls, round_number: int, first: int, limit: int) -> SingleSessionError:
        """Report a resume cursor beyond the paid attempt limit."""
        return cls(
            f"Round {round_number} already persisted {first - 1} attempts, "
            f"exhausting max_retries_per_round={limit}"
        )

    @classmethod
    def missing_gate_reason(cls) -> SingleSessionError:
        """Report an invalid official gate transition."""
        return cls("official gate requested without a reason")

    @classmethod
    def missing_baseline(cls) -> SingleSessionError:
        """Report that no trusted revision can be restored."""
        return cls("no trusted retained candidate or input baseline is available")

    @classmethod
    def missing_winner_commit(cls) -> SingleSessionError:
        """Report a selected record without a candidate revision."""
        return cls("selected candidate has no commit")


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
        output_sink().run_configured(
            RunConfiguredData(
                run_log_path=str(ctx.environment.run_log_path),
                project_root=str(ctx.request.project_root),
                objective=turns.objective,
            )
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
            await self._save_state(state, label="single: initialize policy state")

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
            await self._save_state(state, label=f"single: start hypothesis {plan.hypothesis_id}")
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
        memory = self._memory_paths()
        await self.workspace.restore(rollback, clean=True, preserve_paths=memory)
        hypothesis.revert_applied = True
        hypothesis.revert_commit = rollback
        hypothesis.parent_commit = rollback
        state = update_active_hypothesis(selection.state, hypothesis)
        await self._save_state(
            state, label=f"single: set hypothesis {hypothesis.hypothesis_id} parent"
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
        """Persist a paid turn marker before invoking the combined agent."""
        self.ctx.log(f"\n--- attempt {retry}/{self.options.max_retries_per_round} ---\n")
        attempt = selected.attempt
        attempt.retry = retry
        attempt.official_reason = None
        await self._save_state(
            attempt.agent_run_state,
            label=f"single: start round {self.round_number} attempt {retry}",
        )
        issue_board.write_implementer_start_marker(
            self.turns.progress_path, self.round_number, retry
        )
        await self.workspace.snapshot(f"round-{self.round_number}-retry-{retry}-paid-marker")
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
            await self._checkpoint_active(selected)
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

    async def _checkpoint_active(self, selected: SingleRound) -> None:
        state = update_active_hypothesis(
            selected.attempt.agent_run_state, selected.request.active_hypothesis
        )
        selected.attempt.agent_run_state = state
        await self._save_state(
            state, label=f"single: checkpoint hypothesis {selected.request.plan.hypothesis_id}"
        )

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
                result=AccuracyGateResult(
                    command=command,
                    passed=True,
                    output=(
                        "Reused the prior framework-owned PASS for this exact candidate commit; "
                        "a later gate, not accuracy, caused the retry."
                    ),
                    feedback=None,
                    executed=False,
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
            result=replace(result, output=result.output[-GATE_RECORD_TAIL_CHARS:]),
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
            result=replace(result, output=result.output[-GATE_RECORD_TAIL_CHARS:]),
            metric_name=result.outcome.metric_name or (spec.metric if spec else None),
        )
        await self.workspace.snapshot(f"round-{number}-retry-{retry}-framework-benchmark")
        return result

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
        await self.ctx.state.checkpoint(
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
        self._announce_experiments("round_persisted", engine.state)
        self.ctx.events.emit(
            CoreEventType.ROUND_FINISHED,
            status=EventStatus.COMPLETED if attempt.passed else EventStatus.FAILED,
            round_label=f"round-{record.round_number}",
            data=RoundFinishedData(
                attempts=attempt.retry,
                judge_verdict="pass" if attempt.passed else "fail",
                perf_metric=projection.metric,
                perf_unit=projection.unit,
                profile_skipped=projection.profile_skipped,
            ),
        )

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
                raise SingleSessionError.missing_baseline()
            await self.workspace.restore(baseline, clean=True, preserve_paths=self._memory_paths())
            await self.workspace.snapshot("single: restore trusted input baseline")
            self.ctx.log(f"\nNo evaluated winner was retained. Restored baseline {baseline[:12]}.")
            return True
        if winner.commit is None:
            raise SingleSessionError.missing_winner_commit()
        await self.workspace.retain(f"selected-round-{winner.round_number:04d}", winner.commit)
        await self.workspace.restore(winner.commit, clean=True, preserve_paths=self._memory_paths())
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
