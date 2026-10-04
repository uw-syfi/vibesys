"""Concurrent, durable hypothesis portfolio orchestration over :class:`Run`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from vibesys.hypothesis import (
    HypothesisSearch,
    HypothesisStrategy,
    OrchestratorPlan,
    normalize_hypothesis_title,
)
from vibesys.hypothesis import transitions as hypothesis_transitions
from vibesys.orchestration.dynamic.agent_loop import AgentLoop
from vibesys.orchestration.dynamic.agents import ORCHESTRATOR
from vibesys.orchestration.dynamic.control import (
    HostCore,
    HostLimits,
    Withdrawal,
    WorkerOutcome,
    WorkItem,
)
from vibesys.orchestration.dynamic.input_gate import InputGate
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    DispatchIntent,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    ObserveEvaluations,
    PrepareIntent,
    RecoveryStarted,
    ResumeAgentTurn,
    step,
    withdrawing,
)
from vibesys.orchestration.dynamic.models import (
    DurableStateCommitError,
    DynamicOptions,
    DynamicProfile,
    DynamicState,
    DynamicWorkstream,
    ImplementPortfolioPlan,
    PlannedWorkstream,
    PortfolioPlan,
    ProfilePlan,
    WorkstreamPhase,
    WorkstreamPlan,
    planned_id,
)
from vibesys.orchestration.dynamic.planner_driver import PlannerDriver
from vibesys.orchestration.dynamic.profiles import Profiles
from vibesys.orchestration.dynamic.prompts import (
    render_portfolio,
    render_portfolio_correction,
)
from vibesys.orchestration.dynamic.rounds import BuildableCandidate, Rounds, hypothesis_config
from vibesys.orchestration.dynamic.transitions import (
    EvaluationDispatchStopped,
    SettlementProposed,
    WithdrawRequested,
    validate_workstream_replacement,
)
from vibesys.orchestration.dynamic.transitions import step as envelope_step
from vibesys.orchestration.dynamic.workstream import (
    DynamicAttemptError,
    Workstreams,
    prompt_context,
    workstream_index,
)
from vibesys.orchestration.structured_turn import structured_turn
from vs_loop_state.api import HypothesisOutcome
from vs_runtime.api import (
    CandidateProfileStatus,
    Run,
    RunStatus,
    RuntimeContractError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from vibesys.hypothesis import HypothesisStrategyUpdate


_RECOVERABLE_PHASES = frozenset(
    {
        WorkstreamPhase.PENDING,
        WorkstreamPhase.IMPLEMENTING,
        WorkstreamPhase.IMPLEMENTED,
        WorkstreamPhase.REVIEWED,
        WorkstreamPhase.EVALUATED,
    }
)


class DynamicPlanningError(RuntimeError):
    """The planner scheduled no valid workstream after its correction, with none running."""

    def __init__(self, error: Exception | None) -> None:
        """Name the validation error the correction did not fix."""
        super().__init__(f"the planner scheduled no valid workstream after correction: {error}")


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
    def unbuildable_parent(cls, position: int, plan: WorkstreamPlan) -> DynamicPlanError:
        """Reject a parent that is not a listed buildable candidate."""
        field = f"workstreams[{position}].parent_hypothesis_id"
        if plan.continue_hypothesis:
            return cls(f"{field}: a continued hypothesis builds on its own candidate; use null")
        return cls(
            f"{field}: {plan.parent_hypothesis_id!r} is not a buildable candidate; name one "
            "listed under buildable candidates, or use null for the base revision"
        )

    @classmethod
    def unreproducible_parent(
        cls, position: int, hypothesis_id: str, reason: str, *, field: str = "parent_hypothesis_id"
    ) -> DynamicPlanError:
        """Reject a parent or profile target whose evaluated content cannot be reproduced."""
        use = "built on" if field == "parent_hypothesis_id" else "profiled"
        return cls(
            f"workstreams[{position}].{field}: {hypothesis_id!r} cannot be "
            f"{use} ({reason}); name a listed buildable candidate, or use null for the "
            "base revision"
        )

    @classmethod
    def profiling_unavailable(cls, position: int) -> DynamicPlanError:
        """Reject a profile workstream in a run that cannot produce profile evidence."""
        return cls(
            f"workstreams[{position}].kind: this run cannot produce trusted profile evidence; "
            "schedule only implement workstreams"
        )

    @classmethod
    def unprofilable_target(cls, position: int, plan: ProfilePlan) -> DynamicPlanError:
        """Reject a profile target that is not a listed buildable candidate."""
        return cls(
            f"workstreams[{position}].target_hypothesis_id: {plan.target_hypothesis_id!r} "
            "is not a buildable candidate; name one listed under buildable candidates, or "
            "use null for the base revision"
        )

    @classmethod
    def repeated_id(cls, position: int, identifier: str) -> DynamicPlanError:
        """Reject a later entry that repeats an ID an earlier entry of the plan uses."""
        return cls(
            f"workstreams[{position}]: {identifier!r} repeats an earlier entry's ID; "
            "merge the entries or give each its own ID"
        )

    @classmethod
    def reused_profile_id(cls, position: int, profile_id: str) -> DynamicPlanError:
        """Reject a profile ID that a hypothesis or an earlier profile already uses."""
        return cls(
            f"workstreams[{position}].profile_id: {profile_id!r} was already used; choose a new ID"
        )

    @classmethod
    def invalid_update(cls, error: ValueError) -> DynamicPlanError:
        """Reject a strategy update the hypothesis search cannot apply."""
        return cls(f"hypothesis_updates: {error}")

    @classmethod
    def in_flight_update(cls, position: int, hypothesis_id: str) -> DynamicPlanError:
        """Reject parking or abandoning a hypothesis whose workstream is still running."""
        return cls(
            f"hypothesis_updates[{position}].hypothesis_id: {hypothesis_id!r} is still "
            "running; park or abandon it only after its workstream finishes"
        )

    @classmethod
    def abandoned_continuation(cls, position: int, hypothesis_id: str) -> DynamicPlanError:
        """Reject continuing a hypothesis that is (or this plan makes) abandoned."""
        return cls(
            f"workstreams[{position}].hypothesis_id: {hypothesis_id!r} is abandoned and "
            "cannot be continued"
        )

    @classmethod
    def unchanged_blocked_task(cls, hypothesis_id: str) -> DynamicPlanError:
        """Reject re-dispatching a blocked hypothesis with the task that blocked it."""
        return cls(
            f"hypothesis {hypothesis_id!r} was blocked; continue it only with a task that "
            "removes the recorded blocker, or park or abandon it"
        )


@dataclass(frozen=True, slots=True)
class _ParentOptions:
    """The parents one planning call may name, and the candidates withheld from it."""

    offered: tuple[BuildableCandidate, ...]
    # Hypothesis ID to why its candidate cannot be reproduced.
    unreproducible: Mapping[str, str]

    def check(self, position: int, plan: WorkstreamPlan) -> None:
        """Reject ``plan``'s parent unless it is an offered candidate of a new hypothesis."""
        chosen = plan.parent_hypothesis_id
        if chosen is None:
            return
        if plan.continue_hypothesis:
            raise DynamicPlanError.unbuildable_parent(position, plan)
        if chosen in self.unreproducible:
            raise DynamicPlanError.unreproducible_parent(
                position, chosen, self.unreproducible[chosen]
            )
        if all(item.hypothesis_id != chosen for item in self.offered):
            raise DynamicPlanError.unbuildable_parent(position, plan)

    def check_target(self, position: int, plan: ProfilePlan) -> None:
        """Reject ``plan``'s target unless it is an offered candidate or the base revision."""
        chosen = plan.target_hypothesis_id
        if chosen is None:
            return
        if chosen in self.unreproducible:
            raise DynamicPlanError.unreproducible_parent(
                position, chosen, self.unreproducible[chosen], field="target_hypothesis_id"
            )
        if all(item.hypothesis_id != chosen for item in self.offered):
            raise DynamicPlanError.unprofilable_target(position, plan)

    def revision_for(self, chosen: str | None, base: str) -> str:
        """Return the revision of offered candidate ``chosen``, or ``base`` (checked already)."""
        if chosen is None:
            return base
        return next(item.revision for item in self.offered if item.hypothesis_id == chosen)


@dataclass(slots=True)
class _DynamicRun:
    run: Run
    options: DynamicOptions
    state: DynamicState
    _state_lock: asyncio.Lock
    # Whether the run provisions a profiler and its evaluation executor
    # produces profile evidence, asked once when the run opens.
    _can_profile: bool
    input_gate: InputGate = field(init=False)
    rounds: Rounds = field(init=False)
    workstreams: Workstreams = field(init=False)
    profiles: Profiles = field(init=False)
    # Run-elapsed seconds, the only time source of the host core and the round book.
    _clock: Callable[[], float] = field(init=False)

    def __post_init__(self) -> None:
        self._clock = _elapsed_clock()
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
            clock=self._clock,
        )
        self.workstreams = Workstreams(
            self.run,
            self.options,
            self.state,
            self.rounds,
            lock=self._state_lock,
            commit=self._commit_labeled,
        )
        self.profiles = Profiles(
            self.run, self.state, lock=self._state_lock, commit=self._commit_labeled
        )

    @classmethod
    async def open(cls, run: Run, options: DynamicOptions) -> _DynamicRun:
        """Restore the policy aggregate and complete an interrupted adoption."""
        state = await run.state.load(DynamicState) or DynamicState()
        search = HypothesisSearch(hypothesis_config(options))
        state.search = search.resume(state.search, options.metric_space)
        can_profile = run.facts.profiler_id != "none" and await run.evaluation.can_profile()
        dynamic = cls(run, options, state, asyncio.Lock(), can_profile)
        await dynamic._recover_intents()
        await dynamic._recover_legacy_releases()
        if state.adoption_pending:
            await dynamic._finish_adoption()
        return dynamic

    async def execute(self) -> RunStatus:
        """Keep every slot busy within the workstream budget, then adopt the best.

        A slot that frees is refilled by a planning call for the free slots
        instead of idling until its slowest sibling finishes, and that call sees
        the newest results. Work durably scheduled before a stop resumes first.
        """
        try:
            await self.search_loop().run(self.recoverable())
            self._raise_blocked()
            await self._select_and_adopt()
        finally:
            await self.input_gate.stop()
        return RunStatus.SUCCEEDED

    def search_loop(self) -> AgentLoop[PlannedWorkstream]:
        """Return the planner-mode search over this run, with the run as its workers.

        The loop shares the run's clock, so the core and the durable records
        it settles agree on run-elapsed time.
        """
        core = HostCore[PlannedWorkstream](
            HostLimits(
                max_in_flight=self.options.max_in_flight,
                start_budget=self._remaining_budget(),
                # One bound for every role's turn faults: a planning call gets
                # as many attempts as a workstream does.
                turn_attempts=self.options.max_retries_per_round,
            )
        )
        driver = PlannerDriver[PlannedWorkstream](plan=self._schedule, land_stop=self._checkpoint)
        return AgentLoop(core, driver, self, clock=self._clock)

    def recoverable(self) -> tuple[WorkItem[PlannedWorkstream], ...]:
        """Return the work durably scheduled before a restart, which resumes first."""
        return tuple(_item(plan) for plan in self._recoverable_plans())

    async def _checkpoint(self) -> None:
        """Land a pending stop; start measuring the input before any work starts."""
        await self.run.control.checkpoint()
        # Start before recovered work: a resume with no budget or no free slot
        # never plans, and its candidates still need the input to beat.
        self.input_gate.start()

    async def _schedule(
        self, capacity: int, in_flight: frozenset[str]
    ) -> tuple[WorkItem[PlannedWorkstream], ...]:
        """Plan and durably record new workstreams for ``capacity`` free slots."""
        call = self.state.next_planning_call
        parents = await self._parent_options()
        portfolio = await self._plan(capacity=capacity, in_flight=in_flight, parents=parents)
        await self._record_plans(call, portfolio, parents)
        return tuple(_item(plan) for plan in portfolio.workstreams)

    def run_worker(self, plan: PlannedWorkstream) -> Coroutine[object, object, None]:
        """Return one attempt of ``plan`` (the ``Workers`` port of the loop shell)."""
        if isinstance(plan, ProfilePlan):
            return self.profiles.execute(plan)
        return self.workstreams.execute(plan)

    def _remaining_budget(self) -> int:
        """Return how many more workstreams the run may schedule.

        The budget bounds agent work: ``max_rounds * max_in_flight``
        workstreams, each one round of the shared hypothesis search. A
        continued hypothesis counts like a new one, because it runs its own
        implementer turns with a fresh retry budget and records its own round;
        not counting it would let a planner that keeps continuing run forever,
        and would exceed the search's round limit. Sequences are unique and
        increase with every scheduled workstream, so the largest one counts
        the workstreams scheduled so far. A profile workstream shares the
        sequence: it occupies a slot and an agent turn like any workstream,
        except one that ended unsupported. That one ran no capture, only a
        short profiler turn finding none possible, and it stops further
        profiles, so refunding it costs at most the profiles already in
        flight; charging it would let a run that cannot profile spend its
        implement budget on nothing, as profiles alone once did.
        """
        total = self.options.max_rounds * self.options.max_in_flight
        return total - self.state.scheduled() + self.state.unsupported_profiles()

    async def classify(self, plan: PlannedWorkstream, error: BaseException | None) -> WorkerOutcome:
        """Classify one finished attempt of ``plan`` for the host core.

        The failed attempt is already charged to the workstream's durable
        budget, so the retry decision here and on resume is the same. A
        profile records every way it can end as its outcome, so an error that
        escapes it is fatal.
        """
        if error is None:
            if isinstance(plan, ProfilePlan) and self._profile_unsupported(plan):
                return WorkerOutcome.REFUNDED
            return WorkerOutcome.COMPLETED
        self.run.observations.note(f"dynamic workstream {planned_id(plan)} failed: {error}")
        if isinstance(plan, ProfilePlan) or not isinstance(error, DynamicAttemptError):
            return WorkerOutcome.FATAL
        item = self.state.workstreams[workstream_index(self.state, plan.hypothesis_id)]
        # A failure after a retained implementation keeps its checkpoint, so
        # the retry resumes at the failed stage.
        if not error.repeated and item.budget.remaining(self.options.max_retries_per_round) > 0:
            return WorkerOutcome.RETRYABLE
        return WorkerOutcome.EXHAUSTED

    async def give_up(self, plan: PlannedWorkstream) -> None:
        """Record ``plan`` failed with its retries spent (the ``Workers`` port)."""
        if isinstance(plan, ProfilePlan):
            message = f"profile {plan.profile_id!r} has no retry budget to give up"
            raise TypeError(message)
        await self._give_up(workstream_index(self.state, plan.hypothesis_id))

    def can_withdraw(self, worker_id: str) -> bool:
        """A durable completed round or profile wins the withdrawal race."""
        for item in self.state.workstreams:
            if item.hypothesis_id == worker_id:
                return not any(
                    record.round_number == item.sequence for record in self.state.search.rounds
                )
        return not any(
            item.profile_id == worker_id and item.outcome is not None
            for item in self.state.profiles
        )

    def _withdrawal_intent(
        self, plan: PlannedWorkstream, withdrawal: Withdrawal
    ) -> LifecycleIntent:
        scope_id = planned_id(plan)
        entries = [*self.state.workstreams, *self.state.profiles]
        generation = next(item.sequence for item in entries if planned_id(item.plan) == scope_id)
        kind = IntentKind.CANCEL if withdrawal is Withdrawal.CANCEL else IntentKind.PARK
        return LifecycleIntent(
            operation_id=f"{scope_id}/{generation}/{kind.value}",
            scope_id=scope_id,
            generation=generation,
            kind=kind,
        )

    async def withdraw(self, plan: PlannedWorkstream, withdrawal: Withdrawal) -> None:
        """Persist withdrawal authority before the loop cancels its worker task."""
        intent = self._withdrawal_intent(plan, withdrawal)
        async with self._state_lock:
            reduced, _ = envelope_step(
                self.state, WithdrawRequested(scope_id=intent.scope_id, kind=intent.kind)
            )
            self.state.lifecycle = reduced.lifecycle
            await self._commit(label=f"dynamic: prepare {intent.operation_id}")

    async def settle(self, plan: PlannedWorkstream, withdrawal: Withdrawal) -> None:
        """Replay idempotent cleanup, then atomically acknowledge settlement."""
        intent = self._withdrawal_intent(plan, withdrawal)
        if intent.operation_id not in self.state.lifecycle.intents:
            await self.withdraw(plan, withdrawal)
        if self.state.lifecycle.intents[intent.operation_id].stage is IntentStage.COMPLETED:
            return
        async with self._state_lock:
            reduced, _ = envelope_step(self.state, DispatchIntent(operation_id=intent.operation_id))
            self.state.lifecycle = reduced.lifecycle
            await self._commit(label=f"dynamic: dispatch {intent.operation_id}")
        await self.run.evaluation.release_jobs(planned_id(plan))
        terminal = withdrawal is Withdrawal.CANCEL
        if isinstance(plan, ProfilePlan):
            await self.profiles.settle_withdrawn(
                plan, terminal=terminal, operation_id=intent.operation_id
            )
        else:
            await self.workstreams.settle_withdrawn(
                plan.hypothesis_id, terminal=terminal, operation_id=intent.operation_id
            )

    async def _recover_intents(self) -> None:
        """Reconcile unfinished lifecycle requests before admitting ordinary work."""
        async with self._state_lock:
            reduced, _ = envelope_step(self.state, EvaluationDispatchStopped(stopped=False))
            reduced, pending = envelope_step(reduced, RecoveryStarted())
            self.state.lifecycle = reduced.lifecycle
            await self._commit(label="dynamic: recover evaluation dispatch")
        for intent in pending:
            if isinstance(intent, ObserveEvaluations | ResumeAgentTurn):
                # Recovered workers own observation and same-session continuation.
                continue
            entries = [*self.state.workstreams, *self.state.profiles]
            owner = next(
                (item for item in entries if planned_id(item.plan) == intent.scope_id), None
            )
            if owner is None or owner.sequence != intent.generation:
                # An old acknowledgement must never cancel or settle a newer
                # scope generation. Preserve the stale request for inspection.
                reduced, _ = envelope_step(
                    self.state, BlockIntent(operation_id=intent.operation_id)
                )
                self.state.lifecycle = reduced.lifecycle
                await self._commit(label=f"dynamic: stale {intent.operation_id} blocked")
                continue
            if intent.kind in {IntentKind.PARK, IntentKind.CANCEL}:
                plan = owner.plan
                withdrawal = (
                    Withdrawal.CANCEL if intent.kind is IntentKind.CANCEL else Withdrawal.PARK
                )
                await self.settle(plan, withdrawal)
            elif intent.kind is IntentKind.REOPEN:
                await self.workstreams.reopen_jobs(intent.scope_id, intent.operation_id)
            elif intent.kind is IntentKind.TURN and intent.stage is IntentStage.DISPATCHED:
                # The worker reopens its keyed session and inspects the initial
                # invocation journal. Only a recorded reply can advance it.
                continue
            elif intent.kind is IntentKind.INTERRUPT:
                # The provider API has no acceptance inspection. Preserve the
                # reservation and fence unsafe replacement dispatch on restart.
                reduced, _ = envelope_step(
                    self.state, BlockIntent(operation_id=intent.operation_id)
                )
                self.state.lifecycle = reduced.lifecycle
                await self._commit(label=f"dynamic: reconcile {intent.operation_id} blocked")
        self._raise_blocked()

    async def _recover_legacy_releases(self) -> None:
        """Reconcile legacy cleanup, preserving an unknown withdrawal disposition.

        Older hosts closed admission before recording park versus cancel. Keep
        retained WIP, but block ordinary recovery and candidate adoption until
        that missing disposition can be reconciled.
        """
        for plan in self._recoverable_plans():
            scope_id = planned_id(plan)
            if not await self.run.evaluation.jobs_released(scope_id):
                continue
            await self.withdraw(plan, Withdrawal.PARK)
            intent = self._withdrawal_intent(plan, Withdrawal.PARK)
            async with self._state_lock:
                reduced, _ = envelope_step(
                    self.state, DispatchIntent(operation_id=intent.operation_id)
                )
                self.state.lifecycle = reduced.lifecycle
                await self._commit(label=f"dynamic: reconcile legacy {scope_id}")
            await self.run.evaluation.release_jobs(scope_id)
            async with self._state_lock:
                if isinstance(plan, ProfilePlan):
                    reduced, _ = envelope_step(
                        self.state, BlockIntent(operation_id=intent.operation_id)
                    )
                    self.state.lifecycle = reduced.lifecycle
                else:
                    reduced, _ = envelope_step(
                        self.state,
                        SettlementProposed(
                            operation_id=intent.operation_id,
                            at_s=self._clock(),
                            retry_limit=self.options.max_retries_per_round,
                            unresolved=True,
                        ),
                    )
                    self.state.workstreams = reduced.workstreams
                    self.state.lifecycle = reduced.lifecycle
                    self.state.agent = reduced.agent
                await self._commit(label=f"dynamic: legacy {scope_id} disposition unknown")
        self._raise_blocked()

    def _raise_blocked(self) -> None:
        """Unresolved dispatch cannot produce a successful run or adoption."""
        settled_failures = {
            (record.hypothesis_id, record.round_number)
            for record in self.state.search.rounds
            if not record.passed
        }
        unresolved = [
            intent.operation_id
            for intent in self.state.lifecycle.intents.values()
            if intent.stage is IntentStage.BLOCKED
            and not (
                intent.terminal_failure == "evaluation_resume"
                and (intent.scope_id, intent.generation) in settled_failures
            )
        ]
        if unresolved:
            message = "unresolved lifecycle operation requires reconciliation: " + ", ".join(
                unresolved
            )
            raise RuntimeContractError(message)

    def _profile_unsupported(self, plan: ProfilePlan) -> bool:
        """Return whether ``plan`` ended unsupported, which refunds its start."""
        return any(
            item.profile_id == plan.profile_id
            and item.outcome is not None
            and item.outcome.status is CandidateProfileStatus.UNSUPPORTED
            for item in self.state.profiles
        )

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

    def _recoverable_plans(self) -> tuple[PlannedWorkstream, ...]:
        """Recover work durably scheduled but not completed before interruption.

        A workstream whose round is recorded has finished, whatever its phase;
        a rejected candidate is a final outcome the planner decides about, and
        only a crashed attempt is retried. A profile without an outcome runs.
        """
        recorded = {record.round_number for record in self.state.search.rounds}
        blocked = {
            intent.scope_id
            for intent in self.state.lifecycle.intents.values()
            if intent.stage is IntentStage.BLOCKED
        }
        profiles = tuple(
            item.plan
            for item in self.state.profiles
            if item.outcome is None
            and item.profile_id not in blocked
            and not any(
                intent.scope_id == item.profile_id
                and intent.generation == item.sequence
                and intent.kind is IntentKind.PARK
                and intent.stage is IntentStage.COMPLETED
                for intent in self.state.lifecycle.intents.values()
            )
            and not withdrawing(self.state.lifecycle, item.profile_id)
        )
        return profiles + tuple(
            item.plan
            for item in self.state.workstreams
            if item.sequence not in recorded
            and item.hypothesis_id not in blocked
            and not withdrawing(self.state.lifecycle, item.hypothesis_id)
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
        parents: _ParentOptions,
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
                "profiling": self._profiling_available(),
                **self.rounds.planner_context(
                    await self.workstreams.live_evaluations(), parents.offered
                ),
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
                plan = await structured_turn(
                    session,
                    message,
                    PortfolioPlan if self._profiling_available() else ImplementPortfolioPlan,
                )
                try:
                    self._validate_plan(
                        plan, capacity=capacity, in_flight=in_flight, parents=parents
                    )
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
            valid = self._valid_part(plan, capacity=capacity, in_flight=in_flight, parents=parents)
            if not valid.workstreams and not in_flight:
                # Nothing runs and nothing was scheduled: ending here would
                # report a finished search that never searched.
                raise DynamicPlanningError(first_error)
            return valid
        finally:
            await session.close()

    def _valid_part(
        self,
        portfolio: PortfolioPlan,
        *,
        capacity: int,
        in_flight: frozenset[str],
        parents: _ParentOptions,
    ) -> PortfolioPlan:
        """Return ``portfolio`` without the strategy updates and workstreams that fail validation.

        Each part is kept only if the parts kept before it plus itself still
        validate, so the result is valid as a whole. It may schedule nothing.
        """
        updates: list[HypothesisStrategyUpdate] = []
        for update in portfolio.hypothesis_updates:
            try:
                self._validate_updates(
                    portfolio.model_copy(
                        update={"workstreams": (), "hypothesis_updates": (*updates, update)}
                    ),
                    in_flight=in_flight,
                )
            except DynamicPlanError as error:
                self.run.observations.note(f"dynamic plan: dropped strategy update: {error}")
                continue
            updates.append(update)
        kept = portfolio.model_copy(
            update={"workstreams": (), "hypothesis_updates": tuple(updates)}
        )
        for plan in portfolio.workstreams:
            candidate = kept.model_copy(update={"workstreams": (*kept.workstreams, plan)})
            try:
                self._validate_plan(
                    candidate, capacity=capacity, in_flight=in_flight, parents=parents
                )
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
        parents: _ParentOptions,
    ) -> None:
        if len(portfolio.workstreams) > capacity:
            message = (
                f"portfolio requested {len(portfolio.workstreams)} workstreams, "
                f"but capacity is {capacity}"
            )
            raise DynamicPlanError(message)
        abandoned = self._validate_updates(portfolio, in_flight=in_flight)
        # A repeated ID is checked here, not in the reply schema, so that a
        # plan still repeating one after its correction keeps its first entry
        # and its other workstreams (r19, r20) instead of faulting the turn.
        seen: set[str] = set()
        for position, plan in enumerate(portfolio.workstreams):
            if planned_id(plan) in seen:
                raise DynamicPlanError.repeated_id(position, planned_id(plan))
            seen.add(planned_id(plan))
            if isinstance(plan, ProfilePlan):
                self._validate_profile(position, plan, parents)
            else:
                parents.check(position, plan)
                self._validate_implement(position, plan, abandoned=abandoned, in_flight=in_flight)

    def _validate_implement(
        self,
        position: int,
        plan: WorkstreamPlan,
        *,
        abandoned: frozenset[str],
        in_flight: frozenset[str],
    ) -> None:
        validate_workstream_replacement(self.state, plan.hypothesis_id)
        if any(item.profile_id == plan.hypothesis_id for item in self.state.profiles):
            raise DynamicPlanError.reused_id(plan.hypothesis_id)
        prior = next(
            (item for item in self.state.workstreams if item.hypothesis_id == plan.hypothesis_id),
            None,
        )
        if plan.hypothesis_id in abandoned:
            raise DynamicPlanError.abandoned_continuation(position, plan.hypothesis_id)
        if plan.hypothesis_id in in_flight:
            raise DynamicPlanError.in_flight_continuation(plan.hypothesis_id)
        if prior is None and plan.continue_hypothesis:
            raise DynamicPlanError.unknown_continuation(plan.hypothesis_id)
        if prior is not None and not plan.continue_hypothesis:
            raise DynamicPlanError.reused_id(plan.hypothesis_id)
        if prior is not None and prior.phase in {
            WorkstreamPhase.EVALUATED,
            WorkstreamPhase.CANCELLED,
        }:
            raise DynamicPlanError.terminal_continuation(plan.hypothesis_id)
        if (
            prior is not None
            and prior.implementation is not None
            and prior.implementation.outcome is HypothesisOutcome.BLOCKED
            and plan.task.strip() == prior.plan.task.strip()
        ):
            raise DynamicPlanError.unchanged_blocked_task(plan.hypothesis_id)

    def _validate_profile(self, position: int, plan: ProfilePlan, parents: _ParentOptions) -> None:
        if not self._profiling_available():
            raise DynamicPlanError.profiling_unavailable(position)
        used = {
            *(item.hypothesis_id for item in self.state.workstreams),
            *(item.profile_id for item in self.state.profiles),
        }
        if plan.profile_id in used:
            raise DynamicPlanError.reused_profile_id(position, plan.profile_id)
        parents.check_target(position, plan)

    def _profiling_available(self) -> bool:
        """Return whether a profile workstream can produce trusted profile evidence now.

        The run must be able to profile, and no profile may have ended
        unsupported: that outcome shows the run cannot, whatever it declared.
        """
        return self._can_profile and self.state.unsupported_profiles() == 0

    def _validate_updates(
        self, portfolio: PortfolioPlan, *, in_flight: frozenset[str]
    ) -> frozenset[str]:
        """Check the plan's strategy updates; return the hypotheses abandoned after them.

        A running workstream's direction is unfinished, so it cannot be parked
        or abandoned until it records its round.
        """
        for position, update in enumerate(portfolio.hypothesis_updates):
            if update.hypothesis_id in in_flight:
                raise DynamicPlanError.in_flight_update(position, update.hypothesis_id)
        try:
            updated = hypothesis_transitions.apply_strategy_updates(
                self.state.search,
                portfolio.hypothesis_updates,
            )
        except ValueError as error:
            raise DynamicPlanError.invalid_update(error) from error
        return frozenset(
            item.hypothesis_id
            for item in updated.hypotheses
            if item.strategy is HypothesisStrategy.ABANDONED
        )

    async def _record_plans(
        self, call: int, portfolio: PortfolioPlan, parents: _ParentOptions
    ) -> None:
        base = self._base_revision()
        async with self._state_lock:
            by_id = {item.hypothesis_id: index for index, item in enumerate(self.state.workstreams)}
            self.state.search = hypothesis_transitions.apply_strategy_updates(
                self.state.search,
                portfolio.hypothesis_updates,
            )
            for update in portfolio.hypothesis_updates:
                if update.hypothesis_id in by_id:
                    self.state.workstreams[
                        by_id[update.hypothesis_id]
                    ].strategy_reason_kind = update.reason_kind
            # A slot that failed before recording a round still owns its
            # sequence; reusing it would alias that slot in the winner lookup.
            sequence = max(
                (
                    *(record.round_number for record in self.state.search.rounds),
                    self.state.scheduled(),
                ),
                default=0,
            )
            for plan in portfolio.workstreams:
                sequence += 1
                if isinstance(plan, ProfilePlan):
                    self.state.profiles.append(
                        DynamicProfile(
                            profile_id=plan.profile_id,
                            sequence=sequence,
                            planning_call=call,
                            plan=plan,
                            revision=parents.revision_for(plan.target_hypothesis_id, base),
                        )
                    )
                    continue
                index = by_id.get(plan.hypothesis_id)
                if index is not None:
                    # Continuing a parked direction makes it available again;
                    # its row would otherwise read parked while it runs.
                    self.state.search = hypothesis_transitions.reopen_parked_hypothesis(
                        self.state.search, plan.hypothesis_id
                    )
                parent = parents.revision_for(plan.parent_hypothesis_id, base)
                if index is None:
                    started = hypothesis_transitions.start_hypothesis(
                        self.state.search,
                        _orchestrator_plan(plan, portfolio.reasoning),
                        started_round=sequence,
                        # Workstreams branch from `parent`, not from the
                        # previous round as in a sequential loop, so record
                        # that lineage directly.
                        parent_round=next(
                            (
                                record.round_number
                                for record in reversed(self.state.search.rounds)
                                if record.commit == parent
                            ),
                            None,
                        ),
                        parent_commit=parent,
                    )
                    self.state.search = hypothesis_transitions.finish_hypothesis(started)
                workstream = DynamicWorkstream(
                    hypothesis_id=plan.hypothesis_id,
                    sequence=sequence,
                    planning_call=call,
                    plan=plan,
                    measured_iterations=(
                        self.rounds.measured_iterations(self.state.workstreams[index])
                        if index is not None
                        else ()
                    ),
                    lineage_parent_id=(
                        (
                            self.state.workstreams[index].lineage_parent_id
                            or self.state.workstreams[index].plan.parent_hypothesis_id
                        )
                        if index is not None
                        else plan.parent_hypothesis_id
                    ),
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
                    invocation_sequence=(
                        self.state.workstreams[index].invocation_sequence
                        if index is not None
                        else 0
                    ),
                    # Still buildable once the continuation finishes.
                    verified=(
                        self.state.workstreams[index].verified if index is not None else None
                    ),
                )
                if index is None:
                    self.state.workstreams.append(workstream)
                else:
                    self.state.workstreams[index] = workstream
                    if any(
                        intent.scope_id == plan.hypothesis_id
                        and intent.generation < sequence
                        and intent.kind is IntentKind.PARK
                        and intent.stage is IntentStage.COMPLETED
                        for intent in self.state.lifecycle.intents.values()
                    ):
                        self.state.lifecycle, _ = step(
                            self.state.lifecycle,
                            PrepareIntent(
                                intent=LifecycleIntent(
                                    operation_id=f"{plan.hypothesis_id}/{sequence}/reopen",
                                    scope_id=plan.hypothesis_id,
                                    generation=sequence,
                                    kind=IntentKind.REOPEN,
                                )
                            ),
                        )
            self.state.next_planning_call = call + 1
            await self._commit(label=f"dynamic: schedule planning call {call}")

    async def _parent_options(self) -> _ParentOptions:
        """Return the buildable candidates whose revisions still reproduce their content.

        A candidate verified by an agent-submitted evaluation is offered only if
        its retained revision exports to the content digest that evaluation
        recorded; a framework-evaluated candidate only if its revision exports.
        One that fails is withheld and a plan naming it is corrected, never
        silently given the base revision.
        """
        offered: list[BuildableCandidate] = []
        unreproducible: dict[str, str] = {}
        for candidate in self.rounds.buildable():
            try:
                patch = await self.run.workspaces.export_patch(candidate.revision)
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-231001 [BLE001]; any export failure means the revision cannot be materialized, which the plan correction reports; narrowing to one runtime error type would let another end the run.
                unreproducible[candidate.hypothesis_id] = (
                    f"its revision cannot be exported: {error}"
                )
                continue
            digest = hashlib.sha256(patch.encode()).hexdigest()
            if candidate.content_digest is not None and digest != candidate.content_digest:
                unreproducible[candidate.hypothesis_id] = (
                    "its revision no longer holds the content that passed accuracy"
                )
                continue
            offered.append(candidate)
        for hypothesis_id, reason in unreproducible.items():
            self.run.observations.note(
                f"dynamic: buildable candidate {hypothesis_id} withheld: {reason}"
            )
        return _ParentOptions(offered=tuple(offered), unreproducible=unreproducible)

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
        committed = False
        try:
            await self.run.state.commit(
                self.state,
                workspace=self.run.workspaces.root if workspace else None,
                label=label,
            )
            committed = True
        finally:
            if not committed:
                # A failed or cancelled write may have reached the durable
                # store. Reload before any retry and fence ordinary attempts.
                try:
                    durable = await self.run.state.load(DynamicState) or DynamicState()
                    for name in DynamicState.model_fields:
                        setattr(self.state, name, getattr(durable, name))
                finally:
                    # An unreadable store still forbids further dispatch. Its
                    # transport failure cannot become an ordinary retry.
                    raise DurableStateCommitError(label)


def _item(plan: PlannedWorkstream) -> WorkItem[PlannedWorkstream]:
    return WorkItem(planned_id(plan), plan)


def _elapsed_clock() -> Callable[[], float]:
    """Return a clock of seconds elapsed since this call, for the host core."""
    origin = time.monotonic()
    return lambda: time.monotonic() - origin


def _orchestrator_plan(plan: WorkstreamPlan, reasoning: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=plan.hypothesis_id,
        hypothesis=plan.hypothesis,
        title=normalize_hypothesis_title(plan.title),
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


__all__ = ["DurableStateCommitError", "DynamicPlanError", "DynamicPlanningError", "orchestrate"]
