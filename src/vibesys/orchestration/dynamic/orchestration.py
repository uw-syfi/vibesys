"""Concurrent, durable hypothesis portfolio orchestration over :class:`Run`."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE, ORCHESTRATOR
from vibesys.orchestration.dynamic.input_gate import InputGate, benchmark_objectives
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
from vibesys.orchestration.dynamic.rounds import Rounds, hypothesis_config
from vibesys.orchestration.hypothesis import HypothesisSearch, OrchestratorPlan
from vibesys.orchestration.hypothesis import transitions as hypothesis_transitions
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import (
    Run,
    RunStatus,
    StructuredResponseError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.orchestration.hypothesis import HypothesisStrategyUpdate
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
    def free_slots(cls, scheduled: int, capacity: int) -> DynamicPlanError:
        """Ask once to fill slots that would otherwise idle until a workstream finishes."""
        return cls(
            f"the portfolio schedules {scheduled} of {capacity} free slots, and a free slot "
            "idles until a running workstream finishes. Fill every free slot with an "
            "independent workstream; return the same portfolio only if no independent "
            "work would be useful now"
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
        resumed = item.budget.started or bool(item.prior_attempt)
        if not resumed or workspace.revision is None:
            return None
        return cls(
            revision=workspace.revision,
            remembered=item.candidate_revision or item.prior_revision,
        )


class DynamicAttemptError(RuntimeError):
    """One isolated workstream attempt failed after its failure was persisted."""

    def __init__(self, message: str, *, repeated: bool = False) -> None:
        """Record whether this failure repeats the previous attempt's, before any agent turn."""
        super().__init__(message)
        self.repeated = repeated

    @classmethod
    def from_cause(
        cls, hypothesis_id: str, cause: BaseException, *, repeated: bool = False
    ) -> DynamicAttemptError:
        """Describe the isolated slot and retain the original failure as the cause."""
        return cls(f"{hypothesis_id}: {cause}", repeated=repeated)


@dataclass(slots=True)
class _DynamicRun:
    run: Run
    options: DynamicOptions
    state: DynamicState
    _state_lock: asyncio.Lock
    # Agent turns started per hypothesis in this process; an attempt that
    # started none failed in setup, before any agent could act.
    _agent_turns: dict[str, int] = field(default_factory=dict)
    input_gate: InputGate = field(init=False)
    rounds: Rounds = field(init=False)

    def __post_init__(self) -> None:
        self.input_gate = InputGate(
            self.run,
            self.options,
            self.state,
            lock=self._state_lock,
            commit=self._commit_labeled,
        )
        self.rounds = Rounds(
            self.options,
            self.state,
            self.input_gate,
            lock=self._state_lock,
            commit=self._commit_labeled,
        )

    @classmethod
    async def open(cls, run: Run, options: DynamicOptions) -> _DynamicRun:
        """Restore the policy aggregate and complete an interrupted adoption."""
        state = await run.state.load(DynamicState) or DynamicState()
        search = HypothesisSearch(hypothesis_config(options))
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
            await self.input_gate.stop()
        return RunStatus.SUCCEEDED

    async def _fill_slots(self, running: dict[asyncio.Task[None], WorkstreamPlan]) -> None:
        """Run workstreams until the budget is spent; ``running`` tracks live tasks."""
        fatal: list[BaseException] = []
        await self.run.control.checkpoint()
        # Start before recovered work: a resume with no budget or no free slot
        # never plans, and its candidates still need the input to beat.
        self.input_gate.start()
        for plan in self._recoverable_plans():
            running[self._start(plan)] = plan
        refill = True
        while True:
            free = min(self.options.max_in_flight - len(running), self._remaining_budget())
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
                if await self._settle(plan, task, fatal):
                    running[self._start(plan)] = plan
        if fatal:
            raise fatal[0]

    async def _schedule(
        self, capacity: int, in_flight: frozenset[str]
    ) -> tuple[WorkstreamPlan, ...]:
        """Plan and durably record new workstreams for ``capacity`` free slots."""
        await self.run.control.checkpoint()
        self.input_gate.start()
        call = self.state.next_planning_call
        portfolio = await self._plan(capacity=capacity, in_flight=in_flight)
        await self._record_plans(call, portfolio)
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

    async def _settle(
        self,
        plan: WorkstreamPlan,
        task: asyncio.Task[None],
        fatal: list[BaseException],
    ) -> bool:
        """Handle one finished workstream task; return whether to retry it now.

        The failed attempt is already charged to the workstream's durable
        budget, so the retry decision here and on resume is the same.
        """
        result = task.exception()
        if result is None:
            return False
        self.run.observations.note(f"dynamic workstream {plan.hypothesis_id} failed: {result}")
        index = self._index(plan.hypothesis_id)
        item = self.state.workstreams[index]
        if not isinstance(result, DynamicAttemptError):
            fatal.append(result)
            return False
        # A failure after a retained implementation keeps its checkpoint, so
        # the retry resumes at the failed stage.
        if not result.repeated and item.budget.remaining(self.options.max_retries_per_round) > 0:
            return True
        await self._give_up(index)
        return False

    async def _give_up(self, index: int) -> None:
        """Mark a slot failed with its durable retry budget spent.

        Leaving budget unspent would make resume treat the slot as retryable
        and reimplement it from its parent.
        """
        async with self._state_lock:
            current = self.state.workstreams[index]
            self.state.workstreams[index] = current.model_copy(
                update={
                    "phase": WorkstreamPhase.FAILED,
                    "budget": current.budget.exhaust(self.options.max_retries_per_round),
                },
                deep=True,
            )
            await self._commit(label=f"dynamic: {current.hypothesis_id} retries exhausted")
        await self.rounds.record(index)

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
                    and item.budget.remaining(self.options.max_retries_per_round) > 0
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
                **self.rounds.planner_context(),
            )
            first_error: DynamicPlanError | ValidationError | None = None
            # A valid plan that leaves slots free; kept if the planner, asked
            # once to fill them, still finds no independent work.
            underfilled: PortfolioPlan | None = None
            for attempt in range(2):
                message = (
                    prompt if attempt == 0 else f"{prompt}\n\nCorrection required: {first_error}"
                )
                plan = await _structured_turn(session, message, PortfolioPlan)
                try:
                    self._validate_plan(plan, capacity=capacity, in_flight=in_flight)
                except (DynamicPlanError, ValidationError) as error:
                    first_error = error
                    continue
                if attempt == 0 and len(plan.workstreams) < capacity:
                    # A free slot idles until a running workstream finishes,
                    # which can take a whole implementer turn.
                    underfilled = plan
                    first_error = DynamicPlanError.free_slots(len(plan.workstreams), capacity)
                    continue
                return plan
            if underfilled is not None:
                return underfilled
            # The planner could not correct its plan. Ending the run would
            # discard every in-flight workstream; keep the valid part instead,
            # leaving a slot idle when none of its workstreams is valid.
            self.run.observations.note(
                f"dynamic plan still invalid after correction: {first_error}"
            )
            return self._valid_part(plan, capacity=capacity, in_flight=in_flight)
        finally:
            await session.close()

    def _valid_part(
        self,
        portfolio: PortfolioPlan,
        *,
        capacity: int,
        in_flight: frozenset[str],
    ) -> PortfolioPlan:
        """Return ``portfolio`` without the strategy updates and workstreams that fail validation.

        Each part is kept only if the parts kept before it plus itself still
        validate, so the result is valid as a whole. It may schedule nothing.
        """
        updates: list[HypothesisStrategyUpdate] = []
        for update in portfolio.hypothesis_updates:
            try:
                hypothesis_transitions.apply_strategy_updates(self.state.search, (*updates, update))
            except ValueError as error:
                self.run.observations.note(f"dynamic plan: dropped strategy update: {error}")
                continue
            updates.append(update)
        kept = portfolio.model_copy(
            update={"workstreams": (), "hypothesis_updates": tuple(updates)}
        )
        for plan in portfolio.workstreams:
            candidate = kept.model_copy(update={"workstreams": (*kept.workstreams, plan)})
            try:
                self._validate_plan(candidate, capacity=capacity, in_flight=in_flight)
            except DynamicPlanError as error:
                self.run.observations.note(f"dynamic plan: dropped workstream: {error}")
                continue
            kept = candidate
        return kept

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

    async def _record_plans(self, call: int, portfolio: PortfolioPlan) -> None:
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
                    sequence=sequence,
                    planning_call=call,
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
                            self.rounds.history_row(self.state.workstreams[index]),
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
            self.state.next_planning_call = call + 1
            await self._commit(label=f"dynamic: schedule planning call {call}")

    async def _execute_workstream(self, plan: WorkstreamPlan) -> None:
        """Run one attempt of a workstream; every failure is a retryable attempt failure.

        Workspace creation and the pre-attempt transitions are inside the
        attempt boundary, so a transient worktree error spends a retry of this
        slot instead of ending the run.
        """
        index = self._index(plan.hypothesis_id)
        workspace: CandidateWorkspace | None = None
        turns_at_start = self._agent_turns.get(plan.hypothesis_id, 0)
        # Attempts spent when the work began; unchanged at a failure means no
        # implementer turn charged this attempt.
        spent_at_start: int | None = None
        try:
            workspace = await self._open_attempt(index)
            spent_at_start = self.state.workstreams[index].budget.spent
            if workspace is not None:
                await self._run_attempt(index, plan, workspace)
        except asyncio.CancelledError:
            # Keep the durable phase: resume continues from the last checkpoint
            # and redoes an interrupted implementation.
            raise
        except Exception as error:
            before_turn = self._agent_turns.get(plan.hypothesis_id, 0) == turns_at_start
            repeated = await self._record_failure(
                index, str(error), spent_at_start=spent_at_start, before_turn=before_turn
            )
            raise DynamicAttemptError.from_cause(
                plan.hypothesis_id, error, repeated=repeated
            ) from error
        finally:
            if workspace is not None:
                await self._discard(plan.hypothesis_id, workspace)

    async def _record_failure(
        self, index: int, error: str, *, spent_at_start: int | None, before_turn: bool
    ) -> bool:
        """Persist a failed attempt; return whether it repeats a setup failure.

        A failed turn ends ``failed``, and an attempt that no implementer turn
        charged is charged here. A setup failure (no agent turn started) that
        repeats the previous attempt's setup failure is deterministic: retrying
        it spends the slot's budget on the same error, so the slot gives up at
        once. The error text is kept for the planner, which otherwise sees only
        ``failed``.
        """
        async with self._state_lock:
            current = self.state.workstreams[index]
            repeated = before_turn and current.setup_failure and current.last_error == error
            changes: dict[str, object] = {"last_error": error, "setup_failure": before_turn}
            if current.phase is WorkstreamPhase.IMPLEMENTING:
                changes["phase"] = WorkstreamPhase.FAILED
            if spent_at_start is None or current.budget.spent == spent_at_start:
                changes["budget"] = current.budget.charge()
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            await self._commit(label=f"dynamic: {current.hypothesis_id} attempt failed")
        return repeated

    async def _open_attempt(self, index: int) -> CandidateWorkspace | None:
        """Settle durable bookkeeping and open the attempt's workspace, if work remains."""
        item = self.state.workstreams[index]
        if item.phase is WorkstreamPhase.EVALUATED and (
            item.evaluation is None or item.evaluation.accepted
        ):
            await self.rounds.record(index)
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
        call = item.planning_call
        parent = item.parent_revision
        resume_implemented = item.phase in _IMPLEMENTED_PHASES
        # A session of this hypothesis may already exist and resume here; it
        # remembers edits that the recreated worktree no longer has.
        reset = _RecreatedWorktree.after(item, workspace)
        feedback = item.feedback
        completed = False
        if resume_implemented:
            completed, feedback = await self._assess(index, plan, workspace)
            await self._remember_feedback(index, feedback)
        for _attempt in range(
            self.state.workstreams[index].budget.spent,
            self.options.max_retries_per_round,
        ):
            if completed:
                break
            await self._update(
                index,
                phase=WorkstreamPhase.IMPLEMENTING,
                charge=True,
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
                f"dynamic: {plan.hypothesis_id} implementation planning call {call}"
            )
            implementation = _bind_evidence_revision(implementation, revision)
            await workspace.retain(
                revision,
                label=f"dynamic-{plan.hypothesis_id}-call-{call}",
            )
            await self._update(
                index,
                phase=WorkstreamPhase.IMPLEMENTED,
                candidate_revision=revision,
                implementation=implementation,
                clear_downstream=True,
            )
            completed, feedback = await self._assess(index, plan, workspace)
            await self._remember_feedback(index, feedback)
        if not completed:
            await self._update(index, phase=WorkstreamPhase.FAILED)
        await self.rounds.record(index)

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
        The budget bounds the refunds, so an attempt that crashes the process
        every time eventually counts as failed and cannot loop forever.
        """
        async with self._state_lock:
            current = self.state.workstreams[index]
            refunded = current.budget.refund_interrupted(self.options.max_retries_per_round)
            if refunded is None:
                update: dict[str, object] = {"phase": WorkstreamPhase.FAILED}
                label = f"dynamic: {current.hypothesis_id} interrupted attempt counted"
            else:
                update = {"phase": WorkstreamPhase.PENDING, "budget": refunded}
                label = f"dynamic: {current.hypothesis_id} resume interrupted"
            self.state.workstreams[index] = current.model_copy(update=update, deep=True)
            await self._commit(label=label)

    async def _assess(
        self,
        index: int,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
    ) -> tuple[bool, str | None]:
        """Run every assessment stage after the workstream's durable phase.

        A fresh implementation (``implemented``) is reviewed and then
        evaluated; a resumed one continues at its first incomplete stage, and an
        evaluated one only reports its verdict. Returns whether the candidate is
        complete and, if not, the correction guidance for the next attempt.
        """
        item = self.state.workstreams[index]
        implementation = item.implementation
        revision = item.candidate_revision
        if implementation is None or revision is None:
            raise DynamicPlanError.incomplete_checkpoint(item.hypothesis_id, item.phase)
        if item.phase is WorkstreamPhase.EVALUATED and item.evaluation is not None:
            if item.evaluation.accepted:
                return True, None
            return False, _evaluation_feedback(item.evaluation)
        review = item.review
        if item.phase is WorkstreamPhase.IMPLEMENTED:
            review = await self._maybe_review(
                plan, implementation, workspace, revision, item.sequence
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
            item.planning_call,
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
        self._agent_turns[plan.hypothesis_id] = self._agent_turns.get(plan.hypothesis_id, 0) + 1
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
        sequence: int,
    ) -> ReviewResult | None:
        # A blocked attempt or an unchanged tree leaves nothing to assess;
        # reviewing it spends a judge turn on an empty candidate.
        unchanged = (
            revision == self.state.workstreams[self._index(plan.hypothesis_id)].parent_revision
        )
        if implementation.outcome is HypothesisOutcome.BLOCKED or unchanged:
            return None
        # A candidate that may be promoted is always reviewed. Another
        # terminal outcome is reviewed on every `judge_every`-th workstream,
        # counted by its sequence (its round number), as a sequential loop
        # counts rounds; planning calls are not batches under slot refill.
        promotable = implementation.outcome in _READY_OUTCOMES
        due = promotable or (
            implementation.outcome in _TERMINAL_OUTCOMES
            and sequence % self.options.judge_every == 0
        )
        if not due:
            return None
        session = await self.run.agents.create_session(
            JUDGE,
            workspace=workspace,
            member_id=plan.hypothesis_id,
        )
        self._agent_turns[plan.hypothesis_id] = self._agent_turns.get(plan.hypothesis_id, 0) + 1
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
        planning_call: int,
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
                    f"progress/validation/dynamic-{plan.hypothesis_id}-call-{planning_call}.json"
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
                        workspace, objectives=benchmark_objectives(self.options)
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
        charge: bool = False,
        candidate_revision: str | None = None,
        implementation: ImplementerResult | None = None,
        review: ReviewResult | None = None,
        evaluation: EvaluationResult | None = None,
        clear_downstream: bool = False,
    ) -> None:
        async with self._state_lock:
            current = self.state.workstreams[index]
            changes: dict[str, object] = {"phase": phase}
            if charge:
                changes["budget"] = current.budget.charge()
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

    async def _select_and_adopt(self) -> None:
        await self.input_gate.measured()
        winner = self.rounds.winner()
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

    def _base_revision(self) -> str:
        """Return the revision fresh hypotheses build on: the best trusted one so far.

        Branching every hypothesis from the original root would keep accepted
        improvements from compounding; the final winner could then contain at
        most one hypothesis's change.
        """
        winner = self.rounds.winner()
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

    async def _commit_labeled(self, label: str) -> None:
        await self._commit(label=label)

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


def _bind_evidence_revision(result: ImplementerResult, revision: str) -> ImplementerResult:
    evidence = tuple(
        reference
        if reference.revision is not None
        else reference.model_copy(update={"revision": revision})
        for reference in result.evidence
    )
    return result.model_copy(update={"evidence": evidence})


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


def _orchestrator_plan(plan: WorkstreamPlan, reasoning: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=plan.hypothesis_id,
        hypothesis=plan.hypothesis,
        title=plan.title,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
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
