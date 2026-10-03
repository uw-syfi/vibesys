"""Concurrent, durable hypothesis portfolio orchestration over :class:`Run`."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from vibesys.orchestration.dynamic.agents import ORCHESTRATOR
from vibesys.orchestration.dynamic.input_gate import InputGate
from vibesys.orchestration.dynamic.models import (
    DynamicOptions,
    DynamicState,
    DynamicWorkstream,
    PortfolioPlan,
    WorkstreamPhase,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.prompts import (
    render_portfolio,
    render_portfolio_correction,
)
from vibesys.orchestration.dynamic.rounds import Rounds, hypothesis_config
from vibesys.orchestration.dynamic.workstream import (
    DynamicAttemptError,
    Workstreams,
    prompt_context,
    structured_turn,
    workstream_index,
)
from vibesys.orchestration.hypothesis import HypothesisSearch, OrchestratorPlan
from vibesys.orchestration.hypothesis import transitions as hypothesis_transitions
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import (
    Run,
    RunStatus,
)

if TYPE_CHECKING:
    from vibesys.orchestration.hypothesis import HypothesisStrategyUpdate


_RECOVERABLE_PHASES = frozenset(
    {
        WorkstreamPhase.PENDING,
        WorkstreamPhase.IMPLEMENTING,
        WorkstreamPhase.IMPLEMENTED,
        WorkstreamPhase.REVIEWED,
        WorkstreamPhase.EVALUATED,
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


@dataclass(slots=True)
class _DynamicRun:
    run: Run
    options: DynamicOptions
    state: DynamicState
    _state_lock: asyncio.Lock
    input_gate: InputGate = field(init=False)
    rounds: Rounds = field(init=False)
    workstreams: Workstreams = field(init=False)

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
        self.workstreams = Workstreams(
            self.run,
            self.options,
            self.state,
            self.rounds,
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
        return asyncio.create_task(self.workstreams.execute(plan))

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
        index = workstream_index(self.state, plan.hypothesis_id)
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
            context: dict[str, object] = {
                "capacity": capacity,
                "in_flight": len(in_flight),
                "remaining": self._remaining_budget(),
                **prompt_context(self.run),
                "root_revision": self._base_revision(),
                **self.rounds.planner_context(),
            }
            first_error: DynamicPlanError | ValidationError | None = None
            # A valid plan that leaves slots free; kept if the planner, asked
            # once to fill them, still finds no independent work.
            underfilled: PortfolioPlan | None = None
            for attempt in range(2):
                message = (
                    render_portfolio(**context)
                    if attempt == 0
                    else render_portfolio_correction(
                        error=None if first_error is None else str(first_error),
                        scheduled=0 if underfilled is None else len(underfilled.workstreams),
                        **context,
                    )
                )
                plan = await structured_turn(session, message, PortfolioPlan)
                try:
                    self._validate_plan(plan, capacity=capacity, in_flight=in_flight)
                except (DynamicPlanError, ValidationError) as error:
                    first_error = error
                    continue
                if attempt == 0 and len(plan.workstreams) < capacity:
                    # A free slot idles until a running workstream finishes,
                    # which can take a whole implementer turn.
                    underfilled = plan
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

    async def _commit_labeled(self, label: str) -> None:
        await self._commit(label=label)

    async def _commit(self, *, workspace: bool = False, label: str) -> None:
        self.state.experiment_revision += 1
        await self.run.state.commit(
            self.state,
            workspace=self.run.workspaces.root if workspace else None,
            label=label,
        )


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
