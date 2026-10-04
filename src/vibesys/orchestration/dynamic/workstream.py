"""One workstream attempt: implement, review, evaluate, and record its round."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import RootModel

from vibesys.hypothesis import HypothesisOutcome
from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE
from vibesys.orchestration.dynamic.input_gate import benchmark_objectives
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CompleteIntent,
    DispatchIntent,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    PrepareIntent,
    awaiting_evaluation,
    step,
)
from vibesys.orchestration.dynamic.models import (
    EvaluationResult,
    ImplementerReply,
    ImplementerResult,
    JudgeReply,
    ReviewResult,
    VerifiedCandidate,
    WaitingForEvaluation,
    WorkstreamPhase,
)
from vibesys.orchestration.dynamic.prompts import (
    EvaluationLine,
    FailureTail,
    RepeatedFailureLine,
    render_agent_failures_feedback,
    render_evaluation_resume_bound,
    render_implementation,
    render_repeated_failure_feedback,
    render_review,
    render_trusted_evaluation_feedback,
)
from vibesys.orchestration.dynamic.transitions import (
    EvaluationDispatchStopped,
    InterruptedTurnReplaced,
    SettlementProposed,
)
from vibesys.orchestration.dynamic.transitions import step as envelope_step
from vibesys.orchestration.structured_turn import structured_turn
from vibesys.run.dynamic_suspension import (
    EvaluationAttemptBoundError,
    EvaluationSuspension,
    EvaluationSuspensionInvariantError,
    EvaluationSuspensionUnresolvedError,
    repeated_measurement_failure,
)
from vs_runtime.api import (
    AgentConversationOpenError,
    AgentConversationRequest,
    AgentEvaluationStageOutcome,
    AgentEvaluationStatus,
    InvocationRelease,
    RunCleanupError,
    RunStopped,
    RuntimeContractError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from vibesys.orchestration.dynamic.models import (
        DynamicOptions,
        DynamicState,
        DynamicWorkstream,
        EvidenceReference,
        SteerNote,
        WorkstreamPlan,
    )
    from vibesys.orchestration.dynamic.rounds import Rounds
    from vs_runtime.api import (
        AgentConversation,
        AgentEvaluation,
        AgentRole,
        CandidateWorkspace,
        Run,
    )

_READY_OUTCOMES = frozenset({HypothesisOutcome.NOMINATED, HypothesisOutcome.SUPPORTED})
# Phases with a retained implementation; an attempt resumes after it.
_IMPLEMENTED_PHASES = frozenset(
    {WorkstreamPhase.IMPLEMENTED, WorkstreamPhase.REVIEWED, WorkstreamPhase.EVALUATED}
)
# Agent-submitted evaluations shown to a reviewer, and the end of each failure
# message kept: an error's cause is usually stated last.
_EVALUATION_CLEANUP_FAILURE = "evaluation cleanup failed"
_REVIEWED_EVALUATIONS = 6
_FAILURE_TAIL_CHARS = 1500
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


class IncompleteCheckpointError(RuntimeError):
    """A post-implementation phase was persisted without its retained result."""

    @classmethod
    def at(cls, hypothesis_id: str, phase: WorkstreamPhase) -> IncompleteCheckpointError:
        """Describe the workstream whose checkpoint lacks its implementation."""
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
        resumed = item.implementer_started or bool(item.prior_attempt)
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


class InterruptResult(StrEnum):
    """What an interrupt request did to a workstream's implementer turn."""

    INTERRUPTED = "interrupted"  # The turn ends now; the next one starts with the notes.
    NO_LIVE_TURN = "no_live_turn"  # No implementer turn runs; notes wait for the next one.
    REFUNDS_SPENT = "refunds_spent"  # The refund bound is reached; notes wait likewise.


@dataclass(slots=True)
class Workstreams:
    """Runs workstream attempts against one run's durable state.

    Each phase transition is committed before the next stage starts, so a
    resumed attempt continues at its first incomplete stage. A finished
    workstream is recorded as a round through ``rounds``.
    """

    run: Run
    options: DynamicOptions
    state: DynamicState
    rounds: Rounds
    lock: asyncio.Lock
    commit: Callable[[str], Awaitable[None]]
    # Agent turns started per hypothesis in this process; an attempt that
    # started none failed in setup, before any agent could act.
    _agent_turns: dict[str, int] = field(default_factory=dict)
    # Each running implementer turn's workspace and how many evaluations that
    # workspace had submitted before the turn began.
    _live_turns: dict[str, tuple[CandidateWorkspace, int]] = field(default_factory=dict)
    # The interrupt signal of each running implementer turn.
    _interrupts: dict[str, asyncio.Event] = field(default_factory=dict)
    _completed_resumes: dict[int, str] = field(default_factory=dict)
    # Typed opening failures confirm these authorizations submitted no turn.
    _unused_dispatches: set[str] = field(default_factory=set)

    async def interrupt(self, hypothesis_id: str) -> InterruptResult:
        """End the running implementer turn early so its pending notes reach the next turn.

        The turn's worktree is kept as a work-in-progress revision, the turn is
        refunded through ``refund_interrupted`` (at most
        ``max_retries_per_round`` times per workstream), and the next turn
        resumes the same provider conversation. Evaluations the turn submitted
        keep running.
        """
        signal = self._interrupts.get(hypothesis_id)
        if signal is None or signal.is_set():
            return InterruptResult.NO_LIVE_TURN
        current = self.state.workstreams[workstream_index(self.state, hypothesis_id)]
        if current.budget.refund_interrupted(self.options.max_retries_per_round) is None:
            return InterruptResult.REFUNDS_SPENT
        async with self.lock:
            operation_id = (
                f"{hypothesis_id}/{current.sequence}/interrupt-{current.budget.refunded + 1}"
            )
            self.state.lifecycle, _ = step(
                self.state.lifecycle,
                PrepareIntent(
                    intent=LifecycleIntent(
                        operation_id=operation_id,
                        scope_id=hypothesis_id,
                        generation=current.sequence,
                        kind=IntentKind.INTERRUPT,
                    )
                ),
            )
            reduced, _ = envelope_step(self.state, DispatchIntent(operation_id=operation_id))
            self.state.lifecycle = reduced.lifecycle
            await self.commit(f"dynamic: {hypothesis_id} interrupt requested")
        signal.set()
        return InterruptResult.INTERRUPTED

    async def settle_withdrawn(
        self, hypothesis_id: str, *, terminal: bool, operation_id: str
    ) -> None:
        """Durably park (resumable) or cancel (terminal, round recorded) a workstream.

        A workstream whose round was recorded before the withdrawal landed
        keeps that result. Parking refunds an interrupted implementer turn
        (bounded by ``refund_interrupted``): the orchestrator, not the
        attempt, ended it.
        """
        index = workstream_index(self.state, hypothesis_id)
        if terminal:
            await self.rounds.cancel(index, operation_id)
            return
        async with self.lock:
            updated, _ = envelope_step(
                self.state,
                SettlementProposed(
                    operation_id=operation_id,
                    at_s=0.0,
                    retry_limit=self.options.max_retries_per_round,
                ),
            )
            self.state.workstreams = updated.workstreams
            self.state.lifecycle = updated.lifecycle
            await self.commit(f"dynamic: {hypothesis_id} parked")

    async def live_evaluations(self) -> dict[str, tuple[AgentEvaluation, ...]]:
        """Return the evaluations each running implementer turn has submitted so far.

        A turn can run for most of an hour; its durable state changes only when
        it ends, so these are the only current facts about its candidate.
        """
        live = dict(self._live_turns)
        return {
            hypothesis_id: (await self.run.evaluation.agent_evaluations(workspace))[before:]
            for hypothesis_id, (workspace, before) in live.items()
        }

    async def execute(self, plan: WorkstreamPlan) -> None:
        """Run one attempt of a workstream; every failure is a retryable attempt failure.

        Workspace creation and the pre-attempt transitions are inside the
        attempt boundary, so a transient worktree error spends a retry of this
        slot instead of ending the run.
        """
        index = workstream_index(self.state, plan.hypothesis_id)
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
        except (EvaluationSuspensionInvariantError, IncompleteCheckpointError):
            raise
        except EvaluationSuspensionUnresolvedError as error:
            await self._fail_suspension(index, error)
            raise DynamicAttemptError.from_cause(
                plan.hypothesis_id, error, repeated=True
            ) from error
        except RunStopped:
            await self._suspension().apply(EvaluationDispatchStopped())
            raise
        except asyncio.CancelledError:
            # Keep the durable phase: resume continues from the last checkpoint
            # and redoes an interrupted implementation.
            raise
        except Exception as error:
            if awaiting_evaluation(
                self.state.lifecycle, plan.hypothesis_id, self.state.workstreams[index].sequence
            ):
                await self._fail_suspension(index, EvaluationSuspensionUnresolvedError(str(error)))
                raise DynamicAttemptError.from_cause(
                    plan.hypothesis_id, error, repeated=True
                ) from error
            await self._block_unknown_turn(index, error)
            before_turn = self._agent_turns.get(plan.hypothesis_id, 0) == turns_at_start
            repeated = await self._record_failure(
                index, str(error), spent_at_start=spent_at_start, before_turn=before_turn
            )
            raise DynamicAttemptError.from_cause(
                plan.hypothesis_id, error, repeated=repeated
            ) from error
        finally:
            if workspace is not None:
                current = self.state.workstreams[index]
                owned = [
                    intent
                    for intent in self.state.lifecycle.intents.values()
                    if intent.scope_id == plan.hypothesis_id
                    and intent.generation == current.sequence
                    and intent.stage is not IntentStage.COMPLETED
                ]
                terminal = any(intent.kind is IntentKind.CANCEL for intent in owned)
                unresolved = any(
                    intent.kind in {IntentKind.PARK, IntentKind.INTERRUPT}
                    or (
                        intent.kind is IntentKind.TURN
                        and intent.stage in {IntentStage.DISPATCHED, IntentStage.BLOCKED}
                    )
                    for intent in owned
                )
                if unresolved and not terminal:
                    await self._keep_work_in_progress(index, workspace)
                await self._discard(plan.hypothesis_id, workspace)

    async def _block_unknown_turn(self, index: int, error: Exception) -> None:
        """Fence replacement work when dispatch acceptance cannot be inspected."""
        if isinstance(error, AgentConversationOpenError):
            hypothesis_id = self.state.workstreams[index].hypothesis_id
            self._agent_turns[hypothesis_id] -= 1
            if self._has_dispatched_turn(index):
                self._unused_dispatches.add(self._turn_invocation_id(index))
            return
        async with self.lock:
            current = self.state.workstreams[index]
            dispatched = [
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and intent.kind is IntentKind.TURN
                and intent.stage is IntentStage.DISPATCHED
            ]
            if not dispatched:
                return
            for intent in dispatched:
                reduced, _ = envelope_step(
                    self.state, BlockIntent(operation_id=intent.operation_id)
                )
                self.state.lifecycle = reduced.lifecycle
            await self.commit(f"dynamic: {current.hypothesis_id} dispatch outcome unresolved")
        message = f"{current.hypothesis_id}: unresolved provider dispatch requires reconciliation"
        raise RuntimeContractError(message) from error

    async def _fail_suspension(
        self, index: int, error: EvaluationSuspensionUnresolvedError
    ) -> None:
        """End only this attempt and preserve fences against unsafe provider replay."""
        async with self.lock:
            current = self.state.workstreams[index]
            for intent in tuple(self.state.lifecycle.intents.values()):
                if (intent.scope_id, intent.generation) == (
                    current.hypothesis_id,
                    current.sequence,
                ) and intent.stage is not IntentStage.COMPLETED:
                    reduced, _ = envelope_step(
                        self.state,
                        BlockIntent(
                            operation_id=intent.operation_id,
                            terminal_failure="evaluation_resume",
                        ),
                    )
                    self.state.lifecycle = reduced.lifecycle
            self.state.workstreams[index] = current.model_copy(
                update={
                    "phase": WorkstreamPhase.FAILED,
                    "last_error": str(error),
                    "budget": current.budget.exhaust(self.options.max_retries_per_round),
                    # A failed continuation supplies no completed scientific
                    # stage. Keep its captured revision and verified evidence,
                    # but never record an earlier reply as this attempt's result.
                    "implementation": None,
                    "review": None,
                    "evaluation": None,
                },
                deep=True,
            )
            await self.commit(f"dynamic: {current.hypothesis_id} evaluation resume failed")
        await self.rounds.record(index)

    async def _keep_work_in_progress(self, index: int, workspace: CandidateWorkspace) -> None:
        """Snapshot and retain a withdrawn attempt's worktree before it is discarded.

        Without a retained implementation the snapshot becomes the candidate
        revision, so a resumed (parked) workstream continues from it; with one,
        the implementation stays the resume point and the snapshot is only
        retained.
        """
        current = self.state.workstreams[index]
        revision = await workspace.snapshot(f"dynamic: {current.hypothesis_id} work in progress")
        await workspace.retain(
            revision, label=f"dynamic-{current.hypothesis_id}-wip-{current.budget.spent}"
        )
        if current.implementation is not None:
            return
        async with self.lock:
            current = self.state.workstreams[index]
            self.state.workstreams[index] = current.model_copy(
                update={"candidate_revision": revision}, deep=True
            )
            await self.commit(f"dynamic: {current.hypothesis_id} work in progress kept")

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
        async with self.lock:
            current = self.state.workstreams[index]
            repeated = before_turn and current.setup_failure and current.last_error == error
            changes: dict[str, object] = {"last_error": error, "setup_failure": before_turn}
            for intent in tuple(self.state.lifecycle.intents.values()):
                if (
                    intent.operation_id in self._unused_dispatches
                    and intent.scope_id == current.hypothesis_id
                    and intent.generation == current.sequence
                ):
                    reduced, _ = envelope_step(
                        self.state, CompleteIntent(operation_id=intent.operation_id)
                    )
                    self.state.lifecycle = reduced.lifecycle
                    steers.release_unused(self.state, current.hypothesis_id, intent.operation_id)
                    self._unused_dispatches.remove(intent.operation_id)
            if not before_turn:
                changes["implementer_started"] = True
            if current.phase is WorkstreamPhase.IMPLEMENTING:
                changes["phase"] = WorkstreamPhase.FAILED
            if spent_at_start is None or current.budget.spent == spent_at_start:
                changes["budget"] = current.budget.charge()
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            await self.commit(f"dynamic: {current.hypothesis_id} attempt failed")
        return repeated

    async def _open_attempt(self, index: int) -> CandidateWorkspace | None:
        """Settle durable bookkeeping and open the attempt's workspace, if work remains."""
        item = self.state.workstreams[index]
        if item.phase is WorkstreamPhase.EVALUATED and (
            item.evaluation is None or item.evaluation.accepted
        ):
            await self.rounds.record(index)
            return None
        if item.phase is WorkstreamPhase.IMPLEMENTING and not (
            awaiting_evaluation(self.state.lifecycle, item.hypothesis_id, item.sequence)
            or self._has_dispatched_turn(index)
        ):
            await self._refund_interrupted_attempt(index)
        reopening = next(
            (
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == item.hypothesis_id
                and intent.generation == item.sequence
                and intent.kind is IntentKind.REOPEN
                and intent.stage is not IntentStage.COMPLETED
            ),
            None,
        )
        if reopening is not None:
            await self.reopen_jobs(item.hypothesis_id, reopening.operation_id)
        # Keyed by hypothesis: every attempt and continuation of this
        # hypothesis works at one path, so its agent sessions resume. A retry
        # starts from this workstream's last retained candidate, as the next
        # in-process attempt would, so the review feedback applies to it.
        continuations = tuple(
            continuation
            for continuation in self.state.lifecycle.continuations.values()
            if continuation.scope_id == item.hypothesis_id
            and continuation.generation == item.sequence
        )
        revision = (
            continuations[-1].retained_revision
            if continuations
            and awaiting_evaluation(self.state.lifecycle, item.hypothesis_id, item.sequence)
            else item.candidate_revision or item.parent_revision
        )
        return await self.run.workspaces.create_candidate(
            revision,
            member_id=item.hypothesis_id,
        )

    async def reopen_jobs(self, hypothesis_id: str, operation_id: str) -> None:
        """Replay the idempotent opening of a deliberately resumed parked scope."""
        async with self.lock:
            reduced, _ = envelope_step(self.state, DispatchIntent(operation_id=operation_id))
            self.state.lifecycle = reduced.lifecycle
            await self.commit(f"dynamic: {hypothesis_id} reopen dispatched")
        await self.run.evaluation.reopen_jobs(hypothesis_id)
        async with self.lock:
            reduced, _ = envelope_step(self.state, CompleteIntent(operation_id=operation_id))
            self.state.lifecycle = reduced.lifecycle
            await self.commit(f"dynamic: {hypothesis_id} reopen completed")

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
            reason = str(error).strip() or type(error).__name__
            self.run.observations.note(
                f"dynamic workstream {hypothesis_id} workspace cleanup failed: {reason}"
            )

    async def _run_attempt(
        self,
        index: int,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
    ) -> None:
        item = self.state.workstreams[index]
        call = item.planning_call
        resume_implemented = item.phase in _IMPLEMENTED_PHASES
        # A session of this hypothesis may already exist and resume here; it
        # remembers edits that the recreated worktree no longer has.
        reset = _RecreatedWorktree.after(item, workspace)
        feedback = item.feedback
        completed = False
        if resume_implemented:
            completed, feedback = await self._assess(index, plan, workspace)
            await self._remember_feedback(index, feedback)
        spent = self.state.workstreams[index].budget.spent
        if not resume_implemented and (
            awaiting_evaluation(self.state.lifecycle, item.hypothesis_id, item.sequence)
            or self._has_dispatched_turn(index)
        ):
            spent -= 1  # Resume the already charged turn, including its final allowed attempt.
        for _attempt in range(spent, self.options.max_retries_per_round):
            if completed:
                break
            suspended = awaiting_evaluation(self.state.lifecycle, item.hypothesis_id, item.sequence)
            notes = () if suspended else await self._start_implementer_turn(index)
            submitted_before = len(await self.run.evaluation.agent_evaluations(workspace))
            self._live_turns[plan.hypothesis_id] = (workspace, submitted_before)
            try:
                implementation = await self._implement(
                    plan,
                    workspace,
                    feedback=feedback,
                    reset=reset,
                    notes=notes,
                )
            except EvaluationAttemptBoundError as error:
                completed, feedback = False, str(error)
                continue
            finally:
                del self._live_turns[plan.hypothesis_id]
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
            submitted = (await self.run.evaluation.agent_evaluations(workspace))[submitted_before:]
            await self._remember_verified(index, workspace, submitted, call=call)
            repeated = _repeated_failure(submitted, self.options.max_repeated_failures)
            if repeated is not None:
                # Resubmitting has stopped producing information; the
                # candidate is not reviewed or gated, and a retry starts from
                # the error.
                completed, feedback = False, repeated
                await self._update(index, phase=WorkstreamPhase.FAILED)
            else:
                completed, feedback = await self._assess(index, plan, workspace)
                if not completed:
                    # The next attempt is a new turn; the failures this one
                    # saw may live only in the session that just ended.
                    feedback = _with_agent_failures(feedback, submitted)
            await self._remember_feedback(index, feedback)
        if not completed:
            await self._update(index, phase=WorkstreamPhase.FAILED)
        await self.rounds.record(index)

    async def _remember_verified(
        self,
        index: int,
        workspace: CandidateWorkspace,
        submitted: Sequence[AgentEvaluation],
        *,
        call: int,
    ) -> None:
        """Retain and record the turn's latest revision whose content passed accuracy.

        The turn may edit past it, so the evaluated revision itself is kept,
        not the turn's final candidate: it is the content the trusted check saw.
        """
        verified = _verified_candidate(submitted, self._headline_metric())
        if verified is None:
            return
        await workspace.retain(
            verified.revision,
            label=f"dynamic-{self.state.workstreams[index].hypothesis_id}-accuracy-verified-call-{call}",
        )
        async with self.lock:
            current = self.state.workstreams[index]
            self.state.workstreams[index] = current.model_copy(
                update={
                    "verified": verified.model_copy(
                        update={"observation_sequence": current.sequence}
                    )
                },
                deep=True,
            )
            await self.commit(f"dynamic: {current.hypothesis_id} accuracy-verified candidate")

    def _headline_metric(self) -> str | None:
        objectives = self.options.metric_space.objectives
        return objectives[0].name if objectives else None

    async def _remember_feedback(self, index: int, feedback: str | None) -> None:
        """Persist correction guidance so a retry after a failure still receives it."""
        async with self.lock:
            current = self.state.workstreams[index]
            if current.feedback == feedback:
                return
            self.state.workstreams[index] = current.model_copy(
                update={"feedback": feedback}, deep=True
            )
            await self.commit(f"dynamic: {current.hypothesis_id} feedback")

    async def _refund_interrupted_attempt(self, index: int) -> None:
        """Uncount an implementation attempt that a stop or crash interrupted.

        A failed attempt is marked ``failed``; ``implementing`` at entry means
        the attempt never finished, so it must not consume the retry budget.
        The budget bounds the refunds, so an attempt that crashes the process
        every time eventually counts as failed and cannot loop forever.
        """
        async with self.lock:
            current = self.state.workstreams[index]
            refunded = current.budget.refund_interrupted(self.options.max_retries_per_round)
            # The attempt was charged, so its turn may have run.
            update: dict[str, object] = {"implementer_started": True}
            if refunded is None:
                update["phase"] = WorkstreamPhase.FAILED
                label = f"dynamic: {current.hypothesis_id} interrupted attempt counted"
            else:
                update |= {"phase": WorkstreamPhase.PENDING, "budget": refunded}
                label = f"dynamic: {current.hypothesis_id} resume interrupted"
            self.state.workstreams[index] = current.model_copy(update=update, deep=True)
            await self.commit(label)

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
            raise IncompleteCheckpointError.at(item.hypothesis_id, item.phase)
        if item.phase is WorkstreamPhase.EVALUATED and item.evaluation is not None:
            if item.evaluation.accepted:
                return True, None
            return False, _evaluation_feedback(item.evaluation)
        review = item.review
        if item.phase is WorkstreamPhase.IMPLEMENTED:
            try:
                review = await self._maybe_review(
                    plan, implementation, workspace, revision, item.sequence
                )
            except EvaluationAttemptBoundError as error:
                return False, str(error)
            if review is not None:
                await self._update(index, phase=WorkstreamPhase.REVIEWED, review=review)
        if review is not None and not review.passed:
            return False, review.feedback
        evaluation = await self._maybe_evaluate(implementation, review, workspace, revision)
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
        *,
        feedback: str | None,
        reset: _RecreatedWorktree | None,
        notes: Sequence[SteerNote],
    ) -> ImplementerResult:
        """Run implementer turns until one returns; an interrupted turn is followed by another."""
        index = workstream_index(self.state, plan.hypothesis_id)
        interrupted: str | None = None
        while True:
            signal = asyncio.Event()
            self._interrupts[plan.hypothesis_id] = signal
            try:
                result = await self._implementer_turn(
                    plan,
                    workspace,
                    feedback=feedback,
                    reset=reset,
                    notes=notes,
                    interrupted_revision=interrupted,
                    signal=signal,
                )
            finally:
                del self._interrupts[plan.hypothesis_id]
            if result is not None:
                return result
            interrupted = await workspace.snapshot(
                f"dynamic: {plan.hypothesis_id} interrupted turn"
            )
            await workspace.retain(
                interrupted,
                label=f"dynamic-{plan.hypothesis_id}-interrupted-"
                f"{self.state.workstreams[index].budget.refunded}",
            )
            notes = await self._start_implementer_turn(
                index, refund_interrupted=True, interrupted_revision=interrupted
            )

    def _session_generation(self, hypothesis_id: str) -> int | None:
        """Separate explicit generations after a durably settled resume failure.

        Earlier keys remain fenced and inspectable. Existing generations keep
        their historical key so a restart never changes dispatch identity.
        """
        item = self.state.workstreams[workstream_index(self.state, hypothesis_id)]
        settled_failures = {
            record.round_number
            for record in self.state.search.rounds
            if record.hypothesis_id == hypothesis_id and not record.passed
        }
        if any(
            intent.scope_id == hypothesis_id
            and intent.generation < item.sequence
            and intent.generation in settled_failures
            and intent.terminal_failure == "evaluation_resume"
            for intent in self.state.lifecycle.intents.values()
        ):
            return item.sequence
        return None

    async def _implementer_turn(  # noqa: PLR0913  # lint-waiver: LW-261005 [PLR0913]; each argument is one input of the rendered turn or its interrupt signal; bundling them in a one-use container would only move the same fields.
        self,
        plan: WorkstreamPlan,
        workspace: CandidateWorkspace,
        *,
        feedback: str | None,
        reset: _RecreatedWorktree | None,
        notes: Sequence[SteerNote],
        interrupted_revision: str | None,
        signal: asyncio.Event,
    ) -> ImplementerResult | None:
        """Run one implementer turn; return ``None`` when ``signal`` ended it first."""
        index = workstream_index(self.state, plan.hypothesis_id)
        session = self.run.agents.prepare_conversation(
            AgentConversationRequest(
                role=IMPLEMENTER,
                workspace=workspace,
                member_id=plan.hypothesis_id,
                generation=self._session_generation(plan.hypothesis_id),
            )
        )
        self._agent_turns[plan.hypothesis_id] = self._agent_turns.get(plan.hypothesis_id, 0) + 1
        item = self.state.workstreams[index]
        suspended = awaiting_evaluation(self.state.lifecycle, item.hypothesis_id, item.sequence)
        try:
            if suspended:
                reply = await self._resume_suspended(index, workspace, session)
                return ImplementerResult.model_validate(reply)
            notes = await self._dispatch_turn(index, IMPLEMENTER)
            invocation_id = self._turn_invocation_id(index)
            turn = asyncio.create_task(
                self._suspension().initial_turn(
                    workspace,
                    session,
                    render_implementation(
                        hypothesis_id=plan.hypothesis_id,
                        **prompt_context(self.run),
                        hypothesis=plan.hypothesis,
                        task=plan.task,
                        pass_criteria=plan.pass_criteria,
                        parent_revision=item.parent_revision,
                        evidence=_references_text(plan.evidence),
                        feedback=feedback,
                        prior_attempt=item.prior_attempt,
                        worktree_revision=reset.revision if reset is not None else None,
                        prior_revision=reset.remembered if reset is not None else None,
                        notes=notes,
                        interrupted_revision=interrupted_revision,
                    ),
                    RootModel[ImplementerReply],
                    invocation_id=invocation_id,
                )
            )
            interrupt = asyncio.create_task(signal.wait())
            try:
                await asyncio.wait({turn, interrupt}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                interrupt.cancel()
                if not turn.done():
                    turn.cancel()
                await asyncio.gather(turn, interrupt, return_exceptions=True)
                authority = self._release_authority(index, interrupted=signal.is_set())
                if turn.cancelled() and authority is not None:
                    session.authorize_release(authority)
            if turn.cancelled() and signal.is_set():
                await self._acknowledge_turn(index)
                return None
            result = turn.result().root
            if isinstance(result, WaitingForEvaluation):
                result = await self._suspend(index, workspace, session, result)
            else:
                await self._acknowledge_turn(index)
            return ImplementerResult.model_validate(result)
        finally:
            await session.close()

    def _release_authority(
        self, index: int, *, interrupted: bool = False
    ) -> InvocationRelease | None:
        """Authorize release only for a durable withdrawal or explicit interrupt."""
        item = self.state.workstreams[index]
        withdrawn = any(
            intent.scope_id == item.hypothesis_id
            and intent.generation == item.sequence
            and intent.kind in {IntentKind.PARK, IntentKind.CANCEL}
            and intent.stage is not IntentStage.COMPLETED
            for intent in self.state.lifecycle.intents.values()
        )
        if (interrupted or withdrawn) and self._has_dispatched_turn(index):
            return InvocationRelease(invocation_id=self._turn_invocation_id(index))
        return None

    def _suspension(self) -> EvaluationSuspension:
        return EvaluationSuspension(
            self.run, self.state, self.lock, self.commit, self.options.max_repeated_failures
        )

    async def _suspend(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentConversation,
        reply: WaitingForEvaluation,
    ) -> ImplementerResult | ReviewResult:
        suspension = self._suspension()
        await suspension.yield_turn(index, workspace, session, reply)
        return await self._resume_suspended(index, workspace, session)

    async def _resume_suspended(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentConversation,
    ) -> ImplementerResult | ReviewResult:
        try:
            reply, operation_id = await self._suspension().run_wait(index, workspace, session)
        except (
            EvaluationSuspensionInvariantError,
            EvaluationSuspensionUnresolvedError,
            EvaluationAttemptBoundError,
        ):
            raise
        except Exception as error:
            # The evaluation/session boundary can fail with an undocumented
            # transport error. Keep host invariants and durable-write errors
            # distinct; neither authorizes an isolated attempt outcome.
            raise EvaluationSuspensionUnresolvedError(str(error)) from error
        self._completed_resumes[index] = operation_id
        return reply

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
            revision
            == self.state.workstreams[
                workstream_index(self.state, plan.hypothesis_id)
            ].parent_revision
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
        submitted = await self.run.evaluation.agent_evaluations(workspace)
        index = workstream_index(self.state, plan.hypothesis_id)
        suspended = awaiting_evaluation(self.state.lifecycle, plan.hypothesis_id, sequence)
        if not suspended:
            await self._prepare_turn(index, JUDGE)
        session = self.run.agents.prepare_conversation(
            AgentConversationRequest(
                role=JUDGE,
                workspace=workspace,
                member_id=plan.hypothesis_id,
                generation=self._session_generation(plan.hypothesis_id),
                invocation_id=None if suspended else self._prepared_turn_invocation_id(index),
            )
        )
        self._agent_turns[plan.hypothesis_id] = self._agent_turns.get(plan.hypothesis_id, 0) + 1
        try:
            index = workstream_index(self.state, plan.hypothesis_id)
            if suspended:
                reply = await self._resume_suspended(index, workspace, session)
                return ReviewResult.model_validate(reply)
            notes = await self._dispatch_turn(index, JUDGE)
            result = await structured_turn(
                session,
                render_review(
                    hypothesis_id=plan.hypothesis_id,
                    **prompt_context(self.run),
                    hypothesis=plan.hypothesis,
                    pass_criteria=plan.pass_criteria,
                    candidate_revision=revision,
                    summary=implementation.summary,
                    evidence=_references_text(implementation.evidence),
                    evaluations=_evaluation_lines(submitted[-_REVIEWED_EVALUATIONS:]),
                    notes=notes,
                ),
                RootModel[JudgeReply],
            )
            reply = result.root
            if isinstance(reply, WaitingForEvaluation):
                reply = await self._suspend(index, workspace, session, reply)
            else:
                await self._acknowledge_turn(index)
            return ReviewResult.model_validate(reply)
        finally:
            authority = None if suspended else self._release_authority(index)
            try:
                if authority is not None:
                    session.authorize_release(authority)
            finally:
                await session.close()

    async def _maybe_evaluate(
        self,
        implementation: ImplementerResult,
        review: ReviewResult | None,
        workspace: CandidateWorkspace,
        revision: str,
    ) -> EvaluationResult | None:
        # Every review-passed ready candidate is evaluated; `official_eval_every`
        # does not apply. Workstreams are parallel branches, so an unevaluated
        # candidate could never be adopted or built on (unlike a sequential
        # loop, where the next round builds on a provisional checkpoint).
        candidate_ready = implementation.outcome in _READY_OUTCOMES
        if not candidate_ready or review is None or not review.passed:
            return None
        available = self.run.facts.accuracy_configured or self.run.facts.benchmark_configured
        if not available:
            return None
        try:
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
        except BaseExceptionGroup as group:
            # A stop ends both evaluations; the run ends with its one typed
            # RunStopped, not a task group of them.
            _, other = group.split(RunStopped)
            if other is None:
                raise RunStopped from group
            if all(isinstance(failure, RunCleanupError) for failure in other.exceptions):
                raise RunCleanupError(_EVALUATION_CLEANUP_FAILURE, group.exceptions) from group
            raise
        accuracy = accuracy_task.result() if accuracy_task is not None else None
        benchmark = benchmark_task.result() if benchmark_task is not None else None
        return EvaluationResult(
            revision=revision,
            accuracy_passed=accuracy.passed if accuracy is not None else None,
            accuracy_feedback=accuracy.feedback if accuracy is not None else None,
            benchmark_passed=benchmark.passed if benchmark is not None else None,
            benchmark_feedback=benchmark.feedback if benchmark is not None else None,
            metric_name=benchmark.metric_name if benchmark is not None else None,
            metric_value=benchmark.metric_value if benchmark is not None else None,
            metric_direction=benchmark.metric_direction if benchmark is not None else None,
            metric_unit=benchmark.metric_unit if benchmark is not None else None,
            metrics=dict(benchmark.row or {}) if benchmark is not None else {},
            partial_measurement=benchmark.partial_measurement if benchmark is not None else None,
        )

    async def _start_implementer_turn(
        self,
        index: int,
        *,
        refund_interrupted: bool = False,
        interrupted_revision: str | None = None,
    ) -> tuple[SteerNote, ...]:
        """Persist the charge and replacement revision before session setup."""
        async with self.lock:
            current = self.state.workstreams[index]
            budget = current.budget
            changes: dict[str, object] = {"phase": WorkstreamPhase.IMPLEMENTING}
            if not refund_interrupted and any(
                intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and intent.kind is IntentKind.TURN
                and intent.stage is IntentStage.DISPATCHED
                for intent in self.state.lifecycle.intents.values()
            ):
                return ()
            if refund_interrupted:
                if interrupted_revision is None:
                    message = "interrupted replacement requires a retained revision"
                    raise ValueError(message)
                updated, _ = envelope_step(
                    self.state,
                    InterruptedTurnReplaced(
                        scope_id=current.hypothesis_id,
                        revision=interrupted_revision,
                        retry_limit=self.options.max_retries_per_round,
                    ),
                )
                self.state.workstreams = updated.workstreams
                self.state.lifecycle = updated.lifecycle
                self.state.agent = updated.agent
                await self.commit(f"dynamic: {current.hypothesis_id} implementing")
                return ()
            changes["budget"] = budget.charge()
            prepared = [
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and intent.kind is IntentKind.TURN
                and intent.stage is IntentStage.PREPARED
            ]
            if not prepared:
                sequence = current.invocation_sequence + 1
                invocation_id = _invocation_id(current.hypothesis_id, IMPLEMENTER, sequence)
                changes["invocation_sequence"] = sequence
                self.state.lifecycle, _ = step(
                    self.state.lifecycle,
                    PrepareIntent(
                        intent=LifecycleIntent(
                            operation_id=invocation_id,
                            scope_id=current.hypothesis_id,
                            generation=current.sequence,
                            kind=IntentKind.TURN,
                            invocation_id=invocation_id,
                        )
                    ),
                )
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            if self.state.agent is not None:
                pending_turns = [
                    intent
                    for intent in self.state.lifecycle.intents.values()
                    if intent.scope_id == current.hypothesis_id
                    and intent.generation == current.sequence
                    and intent.kind is IntentKind.TURN
                    and intent.stage is IntentStage.PREPARED
                ]
                if pending_turns:
                    steers.reserve(
                        self.state, current.hypothesis_id, pending_turns[-1].operation_id
                    )
            await self.commit(f"dynamic: {current.hypothesis_id} implementing")
        return ()

    async def _prepare_turn(self, index: int, role: AgentRole) -> None:
        """Persist the turn and note reservation before creating a session."""
        async with self.lock:
            current = self.state.workstreams[index]
            prepared = [
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and intent.kind is IntentKind.TURN
                and intent.stage in {IntentStage.PREPARED, IntentStage.DISPATCHED}
            ]
            if prepared:
                return
            sequence = current.invocation_sequence + 1
            invocation_id = _invocation_id(current.hypothesis_id, role, sequence)
            self.state.workstreams[index] = current.model_copy(
                update={"invocation_sequence": sequence}
            )
            self.state.lifecycle, _ = step(
                self.state.lifecycle,
                PrepareIntent(
                    intent=LifecycleIntent(
                        operation_id=invocation_id,
                        scope_id=current.hypothesis_id,
                        generation=current.sequence,
                        kind=IntentKind.TURN,
                        invocation_id=invocation_id,
                    )
                ),
            )
            steers.reserve(self.state, current.hypothesis_id, invocation_id)
            await self.commit(f"dynamic: {current.hypothesis_id} {role.id} prepared")

    async def _dispatch_turn(self, index: int, role: AgentRole) -> tuple[SteerNote, ...]:
        """Reserve notes and durably authorize the prepared conversation to dispatch."""
        async with self.lock:
            current = self.state.workstreams[index]
            prepared = [
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and intent.kind is IntentKind.TURN
                and intent.stage in {IntentStage.PREPARED, IntentStage.DISPATCHED}
            ]
            if not prepared:
                message = f"{current.hypothesis_id}: dispatch has no prepared invocation"
                raise RuntimeError(message)
            invocation_id = prepared[-1].operation_id
            notes = steers.reserve(self.state, current.hypothesis_id, invocation_id)
            if prepared[-1].stage is IntentStage.PREPARED:
                reduced, _ = envelope_step(self.state, DispatchIntent(operation_id=invocation_id))
                self.state.lifecycle = reduced.lifecycle
                await self.commit(f"dynamic: {current.hypothesis_id} {role.id} dispatch authorized")
        return notes

    def _has_dispatched_turn(self, index: int) -> bool:
        current = self.state.workstreams[index]
        return any(
            intent.scope_id == current.hypothesis_id
            and intent.generation == current.sequence
            and intent.kind is IntentKind.TURN
            and intent.stage is IntentStage.DISPATCHED
            for intent in self.state.lifecycle.intents.values()
        )

    def _prepared_turn_invocation_id(self, index: int) -> str:
        current = self.state.workstreams[index]
        return next(
            intent.operation_id
            for intent in reversed(tuple(self.state.lifecycle.intents.values()))
            if intent.scope_id == current.hypothesis_id
            and intent.generation == current.sequence
            and intent.kind is IntentKind.TURN
            and intent.stage in {IntentStage.PREPARED, IntentStage.DISPATCHED}
        )

    def _turn_invocation_id(self, index: int) -> str:
        current = self.state.workstreams[index]
        return next(
            intent.operation_id
            for intent in reversed(tuple(self.state.lifecycle.intents.values()))
            if intent.scope_id == current.hypothesis_id
            and intent.generation == current.sequence
            and intent.kind is IntentKind.TURN
            and intent.stage is IntentStage.DISPATCHED
        )

    async def _acknowledge_turn(self, index: int) -> None:
        """Record the accepted turn and its note delivery in one durable write."""
        async with self.lock:
            current = self.state.workstreams[index]
            active = [
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and intent.kind is IntentKind.TURN
                and intent.stage is IntentStage.DISPATCHED
            ]
            if not active:
                return
            intent = active[-1]
            steers.mark_delivered(self.state, current.hypothesis_id, intent.operation_id)
            self.state.lifecycle, _ = step(
                self.state.lifecycle, CompleteIntent(operation_id=intent.operation_id)
            )
            await self.commit(f"dynamic: {current.hypothesis_id} turn acknowledged")

    async def _update(  # noqa: PLR0913  # lint-waiver: LW-092703 [PLR0913]; optional fields are explicit transition outputs and avoid an untyped mutation mapping at the durability boundary.
        self,
        index: int,
        *,
        phase: WorkstreamPhase,
        candidate_revision: str | None = None,
        implementation: ImplementerResult | None = None,
        review: ReviewResult | None = None,
        evaluation: EvaluationResult | None = None,
        clear_downstream: bool = False,
    ) -> None:
        async with self.lock:
            current = self.state.workstreams[index]
            changes: dict[str, object] = {"phase": phase}
            if candidate_revision is not None:
                changes["candidate_revision"] = candidate_revision
            if implementation is not None:
                changes["implementation"] = implementation
                changes["implementer_started"] = True
            if clear_downstream:
                changes["review"] = None
                changes["evaluation"] = None
            if review is not None:
                changes["review"] = review
            if evaluation is not None:
                changes["evaluation"] = evaluation
            self.state.workstreams[index] = current.model_copy(update=changes, deep=True)
            completed_resume = self._completed_resumes.pop(index, None)
            if completed_resume is not None:
                updated, _ = envelope_step(
                    self.state, CompleteIntent(operation_id=completed_resume)
                )
                self.state.lifecycle = updated.lifecycle
            await self.commit(f"dynamic: {current.hypothesis_id} {phase.value}")


def prompt_context(run: Run) -> dict[str, object]:
    """Return the run facts every role prompt states inline.

    The objective is inlined rather than cited by path: the effective
    objective lives in run state that agent sandboxes hide. The environment
    notes carry facts such as read-only inputs and where trusted evaluation
    runs, without which agents plan edits that fail. The offered skills are
    named with their descriptions because a generic pointer to "installed
    skills" did not lead agents to load them.
    """
    return {
        "objective": run.facts.objective,
        "environment_notes": run.facts.environment_notes,
        "skills": run.facts.skills,
    }


def workstream_index(state: DynamicState, hypothesis_id: str) -> int:
    """Return the position of ``hypothesis_id`` in the durable workstream list."""
    return next(
        index for index, item in enumerate(state.workstreams) if item.hypothesis_id == hypothesis_id
    )


def _invocation_id(hypothesis_id: str, role: AgentRole, sequence: int) -> str:
    """Return the host's stable id of one worker turn, known before the turn starts.

    Invocation sequence is durable and independent of refundable attempt
    charges. A replacement turn therefore cannot reuse an earlier delivery.
    """
    return f"{hypothesis_id}/{role.id}/invocation-{sequence}"


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


def _failure_tail(failure: str) -> FailureTail:
    if len(failure) <= _FAILURE_TAIL_CHARS:
        return FailureTail(text=failure, truncated=False)
    return FailureTail(text=failure[-_FAILURE_TAIL_CHARS:], truncated=True)


def _evaluation_lines(evaluations: Sequence[AgentEvaluation]) -> list[EvaluationLine]:
    """List agent-submitted evaluations, oldest first, each failure cut to its tail."""
    return [
        EvaluationLine(
            revision=item.revision,
            kinds=item.kinds,
            status=item.status.value,
            failure=None if item.failure is None else _failure_tail(item.failure),
        )
        for item in evaluations
    ]


def _verified_candidate(
    evaluations: Sequence[AgentEvaluation], headline: str | None
) -> VerifiedCandidate | None:
    """Return the latest evaluated revision whose accuracy stage passed, if any."""
    for item in reversed(evaluations):
        stages = {stage.kind: stage for stage in item.stages}
        accuracy = stages.get("accuracy")
        if (
            item.content_digest is None
            or accuracy is None
            or accuracy.outcome is not AgentEvaluationStageOutcome.PASSED
        ):
            continue
        benchmark = stages.get("benchmark")
        metrics = benchmark.metrics if benchmark is not None else ()
        metric = next(
            (entry for entry in metrics if entry.name == headline),
            metrics[0] if metrics else None,
        )
        return VerifiedCandidate(
            revision=item.revision,
            content_digest=item.content_digest,
            benchmark_passed=(
                benchmark.outcome is AgentEvaluationStageOutcome.PASSED
                if benchmark is not None
                else None
            ),
            metric_name=metric.name if metric is not None else None,
            metric_value=metric.value if metric is not None else None,
            metric_unit=metric.unit if metric is not None else None,
            metric_direction=metric.direction if metric is not None else None,
            partial_measurement=benchmark.partial_measurement if benchmark is not None else None,
        )
    return None


def _repeated_failure(evaluations: Sequence[AgentEvaluation], limit: int) -> str | None:
    """Return attempt-ending feedback when the last ``limit`` finished evaluations failed alike."""
    repeated = repeated_measurement_failure(evaluations)
    if repeated is not None and repeated.count >= limit:
        return render_evaluation_resume_bound(
            RepeatedFailureLine(
                kind=repeated.kind.value,
                stage=repeated.stage.value if repeated.stage else None,
                count=repeated.count,
                signature=repeated.signature,
                instruction=repeated.instruction,
            )
        )
    # AgentEvaluation.signature is the established public traceback identity,
    # including supplied signatures whose failure tail no longer holds a full
    # traceback. Keep that contract while measurement policy uses stage data.
    finished = [
        item
        for item in evaluations
        if item.status in {AgentEvaluationStatus.PASSED, AgentEvaluationStatus.FAILED}
    ]
    last = finished[-limit:]
    if len(last) < limit:
        return None
    signature = last[-1].signature
    failure = last[-1].failure
    if signature is None or failure is None or any(item.signature != signature for item in last):
        return None
    return render_repeated_failure_feedback(
        limit=limit, signature=signature, failure=_failure_tail(failure)
    )


def _with_agent_failures(
    feedback: str | None, evaluations: Sequence[AgentEvaluation]
) -> str | None:
    """Append the failures of an attempt's own evaluations to its correction guidance."""
    failed = [item for item in evaluations if item.status is AgentEvaluationStatus.FAILED]
    if not failed:
        return feedback
    return render_agent_failures_feedback(feedback=feedback, evaluations=_evaluation_lines(failed))


def _evaluation_feedback(result: EvaluationResult) -> str:
    """Render trusted gate failures as compact correction guidance."""
    return render_trusted_evaluation_feedback(
        [
            message
            for passed, message in (
                (result.local_validation_passed, result.local_validation_feedback),
                (result.accuracy_passed, result.accuracy_feedback),
                (result.benchmark_passed, result.benchmark_feedback),
            )
            if passed is False and message
        ]
    )


__all__ = [
    "DynamicAttemptError",
    "IncompleteCheckpointError",
    "Workstreams",
    "prompt_context",
    "workstream_index",
]
