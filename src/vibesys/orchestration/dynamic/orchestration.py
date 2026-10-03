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
from vibesys.orchestration.metrics import Measurement, MetricComparison
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome, RoundRecord
from vs_runtime.api import (
    BenchmarkObjective,
    MetricDirection,
    Run,
    RunStatus,
    StructuredResponseError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_runtime.api import AgentSession, CandidateWorkspace


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
# Phases with a retained implementation; an attempt resumes after it.
_IMPLEMENTED_PHASES = frozenset(
    {WorkstreamPhase.IMPLEMENTED, WorkstreamPhase.REVIEWED, WorkstreamPhase.EVALUATED}
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
_MAX_HISTORY_SUMMARY_CHARS = 600
_MAX_HISTORY_REVIEW_CHARS = 600


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
    def in_flight_continuation(cls, hypothesis_id: str) -> DynamicPlanError:
        """Reject scheduling a hypothesis whose workstream is still running."""
        return cls(f"hypothesis {hypothesis_id!r} is still in flight")

    @classmethod
    def unchanged_blocked_task(cls, hypothesis_id: str) -> DynamicPlanError:
        """Reject re-dispatching a blocked hypothesis with the task that blocked it."""
        return cls(
            f"hypothesis {hypothesis_id!r} was blocked; continue it only with a task that "
            "removes the recorded blocker, or park or abandon it"
        )

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


@dataclass(frozen=True, slots=True)
class _RecreatedWorktree:
    """A worktree created fresh for a hypothesis whose agent session may resume."""

    revision: str
    # The revision the hypothesis's previous attempt ended at, if known.
    remembered: str | None

    @classmethod
    def after(
        cls, item: DynamicWorkstream, workspace: CandidateWorkspace
    ) -> _RecreatedWorktree | None:
        """Return the reset facts when an earlier implementer turn of ``item`` exists."""
        resumed = item.attempts > 0 or item.refunded_attempts > 0 or bool(item.prior_attempt)
        if not resumed or workspace.revision is None:
            return None
        return cls(
            revision=workspace.revision,
            remembered=item.candidate_revision or item.prior_revision,
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
    # The input measurement runs beside the first workstreams; only
    # candidate decisions and adoption wait for it.
    _input_measurement: asyncio.Task[None] | None = None
    # Executed input-benchmark failures seen by this process. The first is
    # measured again before it is recorded as a property of the input.
    _input_failures: int = 0

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
        """Keep every slot busy within the workstream budget, then adopt the best.

        A slot that frees is refilled by a planning call for the free slots
        instead of idling until its slowest sibling finishes, and that call sees
        the newest results. Work durably scheduled before a stop resumes first.
        """
        running: dict[asyncio.Task[None], WorkstreamPlan] = {}
        try:
            try:
                await self._fill_slots(running)
            except asyncio.CancelledError:
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)
                raise
            except Exception:
                # A stop lands at a refill checkpoint. Finish in-flight
                # workstreams first so none of their agent work is lost; they
                # persist their own phases, and resume settles any that failed.
                await asyncio.gather(*running, return_exceptions=True)
                raise
            await self._select_and_adopt()
        finally:
            await self._stop_input_measurement()
        return RunStatus.SUCCEEDED

    async def _fill_slots(self, running: dict[asyncio.Task[None], WorkstreamPlan]) -> None:
        """Run workstreams until the budget is spent; ``running`` tracks live tasks."""
        # Keyed by sequence: a continuation of a hypothesis is a new
        # workstream with its own retry budget.
        failures: dict[int, int] = {}
        fatal: list[BaseException] = []
        await self.run.control.checkpoint()
        # Start before recovered work: a resume with no budget or no free slot
        # never plans, and its candidates still need the input to beat.
        self._start_input_measurement()
        for plan in self._recoverable_plans():
            running[self._start(plan)] = plan
        refill = True
        while True:
            free = min(self._capacity() - len(running), self._remaining_budget())
            if refill and not fatal and free > 0:
                in_flight = frozenset(plan.hypothesis_id for plan in running.values())
                for plan in await self._schedule(free, in_flight):
                    running[self._start(plan)] = plan
            refill = False
            if not running:
                break
            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                plan = running.pop(task)
                refill = True
                if await self._settle(plan, task, failures, fatal):
                    running[self._start(plan)] = plan
        if fatal:
            raise fatal[0]

    async def _schedule(
        self, capacity: int, in_flight: frozenset[str]
    ) -> tuple[WorkstreamPlan, ...]:
        """Plan and durably record new workstreams for ``capacity`` free slots."""
        await self.run.control.checkpoint()
        self._start_input_measurement()
        epoch = self.state.next_epoch
        portfolio = await self._plan(capacity=capacity, in_flight=in_flight)
        await self._record_plans(epoch, portfolio)
        return tuple(portfolio.workstreams)

    def _start(self, plan: WorkstreamPlan) -> asyncio.Task[None]:
        return asyncio.create_task(self._execute_workstream(plan))

    def _remaining_budget(self) -> int:
        """Return how many more workstreams the run may schedule.

        Sequences are unique and increase with every scheduled workstream, so
        the largest one counts the workstreams scheduled so far.
        """
        scheduled = max((item.sequence for item in self.state.workstreams), default=0)
        return self.options.max_rounds * self.options.max_in_flight - scheduled

    def _start_input_measurement(self) -> None:
        """Measure the input in the background unless a measurement is running.

        Candidates take far longer to reach a decision than the input takes to
        measure, so the first planning call need not wait for it. A failed
        measurement starts again at the next planning call or candidate decision.
        """
        if self._input_measurement is None or self._input_measurement.done():
            self._input_measurement = asyncio.create_task(self._measure_baseline())

    async def _input_measured(self) -> None:
        """Wait for an input reading before judging a candidate against it.

        A measurement that ended without a reading starts again, so a decision
        made after a failed measurement still gets one more chance to be gated.
        """
        if self.state.baseline is None:
            self._start_input_measurement()
        if self._input_measurement is not None:
            await asyncio.shield(self._input_measurement)

    async def _stop_input_measurement(self) -> None:
        task = self._input_measurement
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _measure_baseline(self) -> None:
        """Benchmark the input revision once per run; resume reuses the stored reading.

        Without it, the first accepted candidate has nothing to beat, so a
        regression could be adopted or built on. A failed measurement is
        retried before the next epoch rather than failing the run.
        """
        if (
            self.state.baseline is not None
            or self.state.winner_revision is not None
            or not self.run.facts.benchmark_configured
        ):
            return
        revision = self._root_revision()
        try:
            benchmark = await self.run.evaluation.benchmark(
                self.run.workspaces.root,
                objectives=self._objectives(),
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-031002 [BLE001]; an input-measurement failure is retried before the next epoch.
            # > The runtime does not normalize evaluator transport failures to one
            # > exception type, so a narrower catch would let a transient Slurm or
            # > provider error end the search; propagating instead fails the run
            # > before any candidate work, for a measurement that can be retried.
            self.run.observations.note(f"dynamic input baseline measurement failed: {error}")
            return
        if not benchmark.passed and not benchmark.executed:
            # The benchmark never ran (provisioning or infrastructure), which
            # says nothing about the input; measure again before the next epoch.
            self.run.observations.note(
                f"dynamic input baseline benchmark did not run: {benchmark.feedback}"
            )
            return
        if not benchmark.passed:
            self._input_failures += 1
            if self._input_failures == 1:
                # It ran beside agent work, so an OOM or a server start timeout
                # may be contention, not the input; measure again before
                # recording a verdict that disables the input gate.
                self.run.observations.note(
                    f"dynamic input baseline benchmark failed; measuring again: "
                    f"{benchmark.feedback}"
                )
                return
        async with self._state_lock:
            self.state.baseline = EvaluationResult(
                revision=revision,
                benchmark_passed=benchmark.passed,
                benchmark_feedback=benchmark.feedback,
                metric_name=benchmark.metric_name,
                metric_value=benchmark.metric_value,
                metric_direction=benchmark.metric_direction,
                metric_unit=benchmark.metric_unit,
                metrics=dict(benchmark.row or {}),
            )
            await self._commit(label="dynamic: measure input baseline")
        if not benchmark.passed:
            # The benchmark ran and rejected the input twice (for example, it
            # lacks a capability the benchmark requires). That is a property
            # of the input, so it is recorded and never re-measured.
            self.run.observations.note(
                "dynamic input does not satisfy the benchmark; candidates need only a "
                "passing trusted benchmark"
            )

    def _beats_baseline(self, metrics: dict[str, float], headline: Measurement | None) -> bool:
        """Return whether a candidate materially beats the measured input.

        With configured objectives the candidate must dominate the input row
        (no worse within noise on any axis, better on one). Without them the
        headline must be better than the input's beyond noise. A missing or
        failed input reading gates nothing.
        """
        baseline = self.state.baseline
        if baseline is None or baseline.benchmark_passed is not True:
            return True
        space = self.options.metric_space
        if space.objectives:
            if not space.complete(baseline.metrics):
                return True
            return space.dominates(metrics, baseline.metrics)
        if baseline.metric_name is None or baseline.metric_value is None or headline is None:
            return True
        comparison = space.compare(
            headline,
            Measurement(
                metric=baseline.metric_name,
                value=baseline.metric_value,
                direction=(
                    baseline.metric_direction.value
                    if baseline.metric_direction is not None
                    else None
                ),
            ),
        )
        return comparison in {MetricComparison.BETTER, MetricComparison.INCOMPARABLE}

    def _objectives(self) -> tuple[BenchmarkObjective, ...]:
        return tuple(
            BenchmarkObjective(name=item.name, direction=MetricDirection(item.direction))
            for item in self.options.metric_space.objectives
        )

    def _capacity(self) -> int:
        return self.options.max_in_flight

    async def _settle(
        self,
        plan: WorkstreamPlan,
        task: asyncio.Task[None],
        failures: dict[int, int],
        fatal: list[BaseException],
    ) -> bool:
        """Handle one finished workstream task; return whether to retry it now."""
        result = task.exception()
        if result is None:
            return False
        self.run.observations.note(f"dynamic workstream {plan.hypothesis_id} failed: {result}")
        index = self._index(plan.hypothesis_id)
        item = self.state.workstreams[index]
        if not isinstance(result, DynamicAttemptError):
            fatal.append(result)
            return False
        failures[item.sequence] = failures.get(item.sequence, 0) + 1
        retries = self.options.max_retries_per_round
        # A failure after a retained implementation keeps its checkpoint, so
        # the retry resumes at the failed stage.
        retained = item.phase is not WorkstreamPhase.FAILED
        if failures[item.sequence] < retries and (retained or item.attempts < retries):
            return True
        await self._give_up(index)
        return False

    async def _give_up(self, index: int) -> None:
        """Mark a slot failed with its durable retry budget spent.

        Leaving ``attempts`` below the budget would make resume treat the slot
        as retryable and reimplement it from its parent.
        """
        async with self._state_lock:
            current = self.state.workstreams[index]
            self.state.workstreams[index] = current.model_copy(
                update={
                    "phase": WorkstreamPhase.FAILED,
                    "attempts": max(current.attempts, self.options.max_retries_per_round),
                },
                deep=True,
            )
            await self._commit(label=f"dynamic: {current.hypothesis_id} retries exhausted")

    def _recoverable_plans(self) -> tuple[WorkstreamPlan, ...]:
        """Recover work durably scheduled but not completed before interruption.

        A workstream whose round is recorded has finished, whatever its phase;
        a rejected candidate is a final outcome the planner decides about, and
        only a crashed attempt is retried.
        """
        recorded = {record.round_number for record in self.state.search.rounds}
        return tuple(
            item.plan
            for item in self.state.workstreams
            if item.sequence not in recorded
            and (
                item.phase in _RECOVERABLE_PHASES
                or (
                    item.phase is WorkstreamPhase.FAILED
                    and item.attempts < self.options.max_retries_per_round
                )
            )
        )

    async def _plan(
        self,
        *,
        capacity: int,
        in_flight: frozenset[str],
    ) -> PortfolioPlan:
        session = await self.run.agents.create_session(
            ORCHESTRATOR,
            workspace=self.run.workspaces.root,
        )
        try:
            prompt = render_portfolio(
                capacity=capacity,
                in_flight=len(in_flight),
                remaining=self._remaining_budget(),
                **self._prompt_context(),
                root_revision=self._base_revision(),
                baseline=(
                    json.dumps(_compact_evaluation(self.state.baseline), separators=(",", ":"))
                    if self.state.baseline is not None and self.state.baseline.benchmark_passed
                    else ""
                ),
                input_failure=(
                    _bounded_optional(
                        self.state.baseline.benchmark_feedback or "no feedback provided",
                        _MAX_HISTORY_REVIEW_CHARS,
                    )
                    if self.state.baseline is not None and not self.state.baseline.benchmark_passed
                    else ""
                ),
                history=self._history_projection(),
                older_ids=", ".join(
                    item.hypothesis_id for item in self.state.workstreams[:-_MAX_HISTORY_ROWS]
                ),
            )
            first_error: DynamicPlanError | ValidationError | None = None
            for attempt in range(2):
                message = (
                    prompt if attempt == 0 else f"{prompt}\n\nCorrection required: {first_error}"
                )
                plan = await _structured_turn(session, message, PortfolioPlan)
                try:
                    self._validate_plan(plan, capacity=capacity, in_flight=in_flight)
                except (DynamicPlanError, ValidationError) as error:
                    first_error = error
                else:
                    return plan
            raise first_error or DynamicPlanError("portfolio planning failed")
        finally:
            await session.close()

    def _validate_plan(
        self,
        portfolio: PortfolioPlan,
        *,
        capacity: int,
        in_flight: frozenset[str],
    ) -> None:
        if len(portfolio.workstreams) > capacity:
            message = (
                f"portfolio requested {len(portfolio.workstreams)} workstreams, "
                f"but capacity is {capacity}"
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
            if plan.hypothesis_id in in_flight:
                raise DynamicPlanError.in_flight_continuation(plan.hypothesis_id)
            if prior is None and plan.continue_hypothesis:
                raise DynamicPlanError.unknown_continuation(plan.hypothesis_id)
            if prior is not None and not plan.continue_hypothesis:
                raise DynamicPlanError.reused_id(plan.hypothesis_id)
            if prior is not None and prior.phase is WorkstreamPhase.EVALUATED:
                raise DynamicPlanError.terminal_continuation(plan.hypothesis_id)
            if (
                prior is not None
                and prior.implementation is not None
                and prior.implementation.outcome is HypothesisOutcome.BLOCKED
                and plan.task.strip() == prior.plan.task.strip()
            ):
                raise DynamicPlanError.unchanged_blocked_task(plan.hypothesis_id)

    async def _record_plans(self, epoch: int, portfolio: PortfolioPlan) -> None:
        parent = self._base_revision()
        async with self._state_lock:
            by_id = {item.hypothesis_id: index for index, item in enumerate(self.state.workstreams)}
            # Workstreams branch from `parent`, not from the previous round as in
            # a sequential loop, so record that lineage directly.
            parent_round = next(
                (
                    record.round_number
                    for record in reversed(self.state.search.rounds)
                    if record.commit == parent
                ),
                None,
            )
            self.state.search = hypothesis_transitions.apply_strategy_updates(
                self.state.search,
                portfolio.hypothesis_updates,
            )
            # A slot that failed before recording a round still owns its
            # sequence; reusing it would alias that slot in the winner lookup.
            sequence = max(
                (
                    *(record.round_number for record in self.state.search.rounds),
                    *(item.sequence for item in self.state.workstreams),
                ),
                default=0,
            )
            for plan in portfolio.workstreams:
                sequence += 1
                index = by_id.get(plan.hypothesis_id)
                if index is None:
                    started = hypothesis_transitions.start_hypothesis(
                        self.state.search,
                        _orchestrator_plan(plan, portfolio.reasoning),
                        started_round=sequence,
                        parent_round=parent_round,
                        parent_commit=parent,
                    )
                    self.state.search = hypothesis_transitions.finish_hypothesis(started)
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
                    # The candidate path is keyed by hypothesis, so a
                    # continuation resumes its provider session. The summary
                    # still covers a session the provider could not resume.
                    prior_attempt=(
                        json.dumps(
                            _history_row(self.state.workstreams[index]),
                            separators=(",", ":"),
                        )
                        if index is not None
                        else ""
                    ),
                    prior_revision=(
                        self.state.workstreams[index].candidate_revision
                        if index is not None
                        else None
                    ),
                )
                if index is None:
                    self.state.workstreams.append(workstream)
                else:
                    self.state.workstreams[index] = workstream
            self.state.next_epoch = epoch + 1
            await self._commit(label=f"dynamic: schedule planning call {epoch}")

    async def _execute_workstream(self, plan: WorkstreamPlan) -> None:
        """Run one attempt of a workstream; every failure is a retryable attempt failure.

        Workspace creation and the pre-attempt transitions are inside the
        attempt boundary, so a transient worktree error spends a retry of this
        slot instead of ending the run.
        """
        index = self._index(plan.hypothesis_id)
        workspace: CandidateWorkspace | None = None
        try:
            workspace = await self._open_attempt(index)
            if workspace is not None:
                await self._run_attempt(index, plan, workspace)
        except asyncio.CancelledError:
            # Keep the durable phase: resume continues from the last checkpoint
            # and redoes an interrupted implementation.
            raise
        except Exception as error:
            if self.state.workstreams[index].phase is WorkstreamPhase.IMPLEMENTING:
                await self._update(index, phase=WorkstreamPhase.FAILED)
            raise DynamicAttemptError.from_cause(plan.hypothesis_id, error) from error
        finally:
            if workspace is not None:
                await self._discard(plan.hypothesis_id, workspace)

    async def _open_attempt(self, index: int) -> CandidateWorkspace | None:
        """Settle durable bookkeeping and open the attempt's workspace, if work remains."""
        item = self.state.workstreams[index]
        if item.phase is WorkstreamPhase.EVALUATED and (
            item.evaluation is None or item.evaluation.accepted
        ):
            await self._record_hypothesis_round(index)
            return None
        if item.phase is WorkstreamPhase.IMPLEMENTING:
            await self._refund_interrupted_attempt(index)
        # Keyed by hypothesis: every attempt and continuation of this
        # hypothesis works at one path, so its agent sessions resume. A retry
        # starts from this workstream's last retained candidate, as the next
        # in-process attempt would, so the review feedback applies to it.
        return await self.run.workspaces.create_candidate(
            item.candidate_revision or item.parent_revision,
            member_id=item.hypothesis_id,
        )

    async def _discard(self, hypothesis_id: str, workspace: CandidateWorkspace) -> None:
        """Release an attempt's workspace without replacing the attempt's result.

        The result is already durable when cleanup runs: raising here would
        mask the attempt's own error or make a recorded workstream retry. A
        leaked worktree resurfaces as a creation error inside the next attempt
        boundary of the same hypothesis.
        """
        try:
            await workspace.discard()
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-031017 [BLE001]; cleanup after a durable result must not replace that result.
            # > Narrowing to one type would let another cleanup failure (an
            # > ExceptionGroup from the runtime's teardown) end the run or retry
            # > a recorded workstream; the error is reported, not dropped.
            self.run.observations.note(
                f"dynamic workstream {hypothesis_id} workspace cleanup failed: {error}"
            )

    async def _run_attempt(
        self,
        index: int,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
    ) -> None:
        item = self.state.workstreams[index]
        epoch = item.epoch
        parent = item.parent_revision
        resume_implemented = item.phase in _IMPLEMENTED_PHASES
        # A session of this hypothesis may already exist and resume here; it
        # remembers edits that the recreated worktree no longer has.
        reset = _RecreatedWorktree.after(item, workspace)
        feedback = item.feedback
        completed = False
        if resume_implemented:
            completed, feedback = await self._resume_implemented(
                index,
                plan,
                workspace,
                epoch,
            )
            await self._remember_feedback(index, feedback)
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
                reset=reset,
            )
            reset = None
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
            await self._remember_feedback(index, feedback)
        if not completed:
            await self._update(index, phase=WorkstreamPhase.FAILED)
        await self._record_hypothesis_round(index)

    async def _remember_feedback(self, index: int, feedback: str | None) -> None:
        """Persist correction guidance so a retry after a failure still receives it."""
        async with self._state_lock:
            current = self.state.workstreams[index]
            if current.feedback == feedback:
                return
            self.state.workstreams[index] = current.model_copy(
                update={"feedback": feedback}, deep=True
            )
            await self._commit(label=f"dynamic: {current.hypothesis_id} feedback")

    async def _refund_interrupted_attempt(self, index: int) -> None:
        """Uncount an implementation attempt that a stop or crash interrupted.

        A failed attempt is marked ``failed``; ``implementing`` at entry means
        the attempt never finished, so it must not consume the retry budget.
        At most ``max_retries_per_round`` interruptions are refunded per
        workstream; beyond that the interrupted attempt counts as failed, so
        an attempt that crashes the process every time cannot loop forever.
        """
        async with self._state_lock:
            current = self.state.workstreams[index]
            if current.refunded_attempts >= self.options.max_retries_per_round:
                update: dict[str, object] = {"phase": WorkstreamPhase.FAILED}
                label = f"dynamic: {current.hypothesis_id} interrupted attempt counted"
            else:
                update = {
                    "phase": WorkstreamPhase.PENDING,
                    "attempts": max(current.attempts - 1, 0),
                    "refunded_attempts": current.refunded_attempts + 1,
                }
                label = f"dynamic: {current.hypothesis_id} resume interrupted"
            self.state.workstreams[index] = current.model_copy(update=update, deep=True)
            await self._commit(label=label)

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
        reset: _RecreatedWorktree | None,
    ) -> ImplementerResult:
        session = await self.run.agents.create_session(
            IMPLEMENTER,
            workspace=workspace,
            member_id=plan.hypothesis_id,
        )
        try:
            return await _structured_turn(
                session,
                render_implementation(
                    hypothesis_id=plan.hypothesis_id,
                    **self._prompt_context(),
                    hypothesis=plan.hypothesis,
                    task=plan.task,
                    pass_criteria=plan.pass_criteria,
                    parent_revision=parent_revision,
                    evidence=_references_text(plan.evidence),
                    feedback=feedback,
                    prior_attempt=self.state.workstreams[
                        self._index(plan.hypothesis_id)
                    ].prior_attempt,
                    worktree_revision=reset.revision if reset is not None else None,
                    prior_revision=reset.remembered if reset is not None else None,
                ),
                ImplementerResult,
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
        # A blocked attempt or an unchanged tree leaves nothing to assess;
        # reviewing it spends a judge turn on an empty candidate.
        unchanged = (
            revision == self.state.workstreams[self._index(plan.hypothesis_id)].parent_revision
        )
        if implementation.outcome is HypothesisOutcome.BLOCKED or unchanged:
            return None
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
            return await _structured_turn(
                session,
                render_review(
                    hypothesis_id=plan.hypothesis_id,
                    **self._prompt_context(),
                    hypothesis=plan.hypothesis,
                    pass_criteria=plan.pass_criteria,
                    candidate_revision=revision,
                    summary=implementation.summary,
                    evidence=_references_text(implementation.evidence),
                ),
                ReviewResult,
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
        # Every review-passed ready candidate is evaluated; `official_eval_every`
        # does not apply. Workstreams are parallel branches, so an unevaluated
        # candidate could never be adopted or built on (unlike a sequential
        # loop, where the next round builds on a provisional checkpoint).
        candidate_ready = implementation.outcome in _READY_OUTCOMES
        if not candidate_ready or review is None or not review.passed:
            return None
        available = self.run.facts.accuracy_configured or self.run.facts.benchmark_configured
        has_local_recipe = implementation.validation_recipe_artifact is not None
        if not available and not has_local_recipe:
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
                    self.run.evaluation.benchmark(workspace, objectives=self._objectives()),
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
            if review is not None:
                changes["review"] = review
            if evaluation is not None:
                changes["evaluation"] = evaluation
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            await self._commit(label=f"dynamic: {current.hypothesis_id} {phase.value}")

    async def _record_hypothesis_round(self, index: int) -> None:
        """Commit one workstream result through shared hypothesis transitions."""
        if self.state.workstreams[index].evaluation is not None:
            # The candidate decision compares against the input measurement.
            await self._input_measured()
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
                headline=(
                    Measurement(
                        metric=evaluation.metric_name,
                        value=evaluation.metric_value,
                        direction=direction,
                    )
                    if framework_metric
                    else None
                ),
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
        await self._input_measured()
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
        # Filter again here: a round recorded before the input baseline was
        # measured was not gated by it.
        winner = search.best(
            [
                record
                for record in self.state.search.rounds
                if self._beats_baseline(
                    dict(record.metrics),
                    hypothesis_transitions.headline_measurement(record),
                )
            ],
            space=self.options.metric_space,
        )
        if winner is None:
            return None
        return next(
            (item for item in self.state.workstreams if item.sequence == winner.round_number),
            None,
        )

    def _candidate_decision(
        self,
        *,
        accepted: bool | None,
        metrics: dict[str, float],
        headline: Measurement | None,
    ) -> tuple[CandidateDisposition, bool | None]:
        """Apply the shared noise aware frontier policy to one evaluated candidate."""
        if accepted is False:
            return CandidateDisposition.DISCARD, False
        if accepted is not True:
            return CandidateDisposition.UNASSESSED, None
        space = self.options.metric_space
        comparable = space.complete(metrics) if space.objectives else headline is not None
        if not comparable:
            return CandidateDisposition.UNASSESSED, None
        if not self._beats_baseline(metrics, headline):
            return CandidateDisposition.DISCARD, False
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
        rows = [_history_row(item) for item in self.state.workstreams[-_MAX_HISTORY_ROWS:]]
        return json.dumps(rows, separators=(",", ":"))

    def _base_revision(self) -> str:
        """Return the revision fresh hypotheses build on: the best trusted one so far.

        Branching every hypothesis from the original root would keep accepted
        improvements from compounding; the final winner could then contain at
        most one hypothesis's change.
        """
        winner = self._winner()
        if winner is not None and winner.candidate_revision is not None:
            return winner.candidate_revision
        return self._root_revision()

    def _root_revision(self) -> str:
        revision = self.run.workspaces.root.revision
        if revision is None:
            message = "dynamic orchestration requires a recorded root revision"
            raise RuntimeError(message)
        return revision

    def _prompt_context(self) -> dict[str, str]:
        """Return the run facts every role prompt states inline.

        The objective is inlined rather than cited by path: the effective
        objective lives in run state that agent sandboxes hide. The environment
        notes carry facts such as read-only inputs and where trusted evaluation
        runs, without which agents plan edits that fail.
        """
        return {
            "objective": self.run.facts.objective,
            "environment_notes": self.run.facts.environment_notes,
        }

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


async def _structured_turn[ResponseT: BaseModel](
    session: AgentSession,
    message: str,
    response: type[ResponseT],
) -> ResponseT:
    """Run one turn, asking the same conversation once to re-emit an unparseable reply.

    The follow-up keeps the agent's completed work and workspace edits; failing
    the turn instead would discard an implementation attempt or the whole run.
    """
    try:
        return await session.turn(message, response=response)
    except StructuredResponseError as error:
        correction = (
            f"Correction required: {error}. Do not redo the work. Return only the "
            f"schema-valid {response.__name__} JSON for the work already completed."
        )
        return await session.turn(correction, response=response)


def _history_row(item: DynamicWorkstream) -> dict[str, object]:
    """Project one workstream's latest attempt as bounded decision facts."""
    return {
        "hypothesis_id": item.hypothesis_id,
        "title": item.plan.title,
        "phase": item.phase.value,
        "outcome": item.implementation.outcome.value if item.implementation is not None else None,
        "summary": (
            _bounded_optional(
                item.implementation.summary,
                _MAX_HISTORY_SUMMARY_CHARS,
            )
            if item.implementation is not None
            else None
        ),
        "next_step": item.implementation.next_step if item.implementation is not None else "",
        "review": _compact_review(item.review),
        "revision": _bounded_optional(
            item.candidate_revision,
            _MAX_HISTORY_REVISION_CHARS,
        ),
        "evidence": [ref.model_dump(mode="json") for ref in _evidence(item)],
        "evaluation": _compact_evaluation(item.evaluation),
    }


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


def _compact_review(review: ReviewResult | None) -> dict[str, object] | None:
    """Project the verdict and its reason so the planner can avoid a rejected path."""
    if review is None:
        return None
    reason = review.feedback or review.analysis
    return {
        "passed": review.passed,
        "reason": _bounded_optional(reason, _MAX_HISTORY_REVIEW_CHARS),
    }


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
    if not run.workspaces.supports_parallel_candidates:
        # Every workstream needs an isolated candidate workspace; failing here
        # avoids spending a planner turn on work that cannot start.
        message = (
            "dynamic orchestration needs isolated candidate workspaces, but this run "
            "environment does not support parallel candidates"
        )
        raise RuntimeError(message)
    dynamic = await _DynamicRun.open(run, options)
    return await dynamic.execute()


__all__ = ["DynamicPlanError", "orchestrate"]
