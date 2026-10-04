"""Persist core suspension requests before calling evaluation and session interfaces.

EvaluationSuspension returns a completed reply and its pending acknowledgement.
The caller commits that acknowledgement with the scientific stage result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypedDict

from pydantic import RootModel

from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.agents import IMPLEMENTER, JUDGE
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CancelEvaluation,
    CompleteIntent,
    DependencyContinuation,
    DispatchIntent,
    EvaluationDependency,
    EvaluationOutcome,
    InspectEvaluation,
    IntentKind,
    IntentStage,
    ObserveEvaluations,
    ProfilerDependency,
    RecoveryStarted,
    ResumeAgentTurn,
    awaiting_evaluation,
)
from vibesys.orchestration.dynamic.models import (
    ImplementerReply,
    ImplementerResult,
    JudgeReply,
    PortfolioPlan,
    WaitingForEvaluation,
    WaitingForProfiler,
)
from vibesys.orchestration.dynamic.prompts import (
    EvaluationResumeLine,
    RepeatedFailureLine,
    render_evaluation_history_unavailable,
    render_evaluation_no_progress,
    render_evaluation_resume,
    render_evaluation_resume_bound,
    render_evaluation_wait_error,
)
from vibesys.orchestration.dynamic.transitions import (
    AttemptBoundReached,
    DeadlineReached,
    EnvelopeEvent,
    EvaluationInspected,
    EvaluationObserved,
    EvaluationSettled,
    ProfilerSettled,
    WorkerAwaitingEvaluation,
    evaluation_wait_reopen,
    step,
)
from vibesys.run.attempt_evaluations import AttemptEvaluationCursors
from vibesys.run.evaluation_backend import SemanticEvaluationStage, agent_evaluation
from vibesys.run.validated_turn import validated_conversation, validated_turn
from vs_evaluation.api import (
    MAX_STAGE_SUMMARY_TAIL_CHARS,
    EvaluationAgentAccessError,
    EvaluationAgentRole,
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationOperationSnapshot,
    EvaluationPending,
    EvaluationStageOutcome,
    EvaluationState,
    EvaluationUnknown,
    EvidenceKind,
    EvidenceOutcome,
    FailureKind,
    OwnedEvaluationDependencies,
    ProfilerOperation,
    ProfilerOperationState,
    RepeatedFailure,
    StageState,
    StoredEvaluation,
    TrustedEvidence,
    detect_repeated_failure,
    evaluation_principal,
)
from vs_runtime.api import (
    AgentConversationOpenError,
    AgentEvaluationStatus,
    Completed,
    InvocationConflictError,
    RuntimeContractError,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    SessionTransportUnavailableError,
    StructuredResponseError,
    Unknown,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from pydantic import BaseModel

    from vibesys.orchestration.dynamic.lifecycle import LifecycleRequest
    from vibesys.orchestration.dynamic.models import DynamicState, ReviewResult
    from vs_evaluation.api import EvaluationSettlementObservation
    from vs_prompts.api import RenderedPrompt
    from vs_runtime.api import (
        AgentConversation,
        AgentEvaluation,
        CandidateWorkspace,
        Evaluation,
        InvocationOutcome,
        Run,
    )


@dataclass(slots=True)
class PlanningObservations:
    """Host-read evaluation history and immutable citation identities for pure planning."""

    live: dict[str, tuple[AgentEvaluation, ...]]
    evidence_revisions: dict[str, str]

    def bind_session(self, session: AgentConversation, evaluation: Evaluation) -> AgentConversation:
        """Refresh capture identities after every reply, before pure plan validation."""

        async def refresh(_response: PortfolioPlan) -> None:
            self.evidence_revisions = await evaluation.evidence_revisions()

        return validated_conversation(session, PortfolioPlan, refresh)


async def gather_planning_observations(
    run: Run, live_turns: dict[str, tuple[CandidateWorkspace, int]]
) -> PlanningObservations:
    """Read provider state in the run shell before projecting planner decision inputs."""
    live = {
        hypothesis_id: (await run.evaluation.agent_evaluations(workspace))[before:]
        for hypothesis_id, (workspace, before) in live_turns.items()
    }
    return PlanningObservations(
        live=live, evidence_revisions=await run.evaluation.evidence_revisions()
    )


class EvaluationSuspensionUnresolvedError(RuntimeContractError):
    """Durable continuation remains owned until acceptance is reconciled."""


class AttemptBoundKind(StrEnum):
    """Typed reason a continuation no longer authorizes an agent turn."""

    REPEATED_FAILURE = "repeated_failure"
    NO_NEW_EVALUATION = "no_new_evaluation"
    HISTORY_UNAVAILABLE = "history_unavailable"


class EvaluationAttemptBoundError(RuntimeContractError):
    """A settled evaluation reached the attempt's configured continuation bound."""

    def __init__(self, reason: str, repeated: RepeatedFailure | None = None) -> None:
        """Preserve the typed classifier result for the attempt owner."""
        super().__init__(reason)
        self.repeated = repeated
        self.kind = (
            AttemptBoundKind.REPEATED_FAILURE if repeated else AttemptBoundKind.NO_NEW_EVALUATION
        )


class EvaluationAttemptHistoryUnavailableError(EvaluationAttemptBoundError):
    """An older charged attempt has no authoritative submission-history boundary."""

    def __init__(self, reason: str) -> None:
        """End the old attempt; a new attempt writes its cursor before agent execution."""
        super().__init__(reason)
        self.kind = AttemptBoundKind.HISTORY_UNAVAILABLE


class EvaluationSuspensionInvariantError(RuntimeContractError):
    """Host continuation identity or dispatch authority is inconsistent."""


@dataclass(slots=True)
class EvaluationSuspension:
    """Execute only requests authorized by the atomically committed envelope."""

    run: Run
    state: DynamicState
    lock: asyncio.Lock
    commit: Callable[[str], Awaitable[None]]
    max_repeated_failures: int = 3

    def log_error(self, index: int, error: Exception) -> None:
        """Record the original cause before lifecycle classification can wrap it."""
        cause = f"{type(error).__name__}: {error}"
        logging.getLogger("vibesys.orchestration.dynamic.workstream").exception(
            "dynamic workstream %s failed: %s", self.state.workstreams[index].hypothesis_id, cause
        )

    def unresolved_evaluation(self, index: int, error: Exception) -> bool:
        """Known completed agent rejections cannot imply ambiguous provider dispatch."""
        if isinstance(error, EvaluationAgentAccessError | StructuredResponseError):
            return False
        current = self.state.workstreams[index]
        return awaiting_evaluation(self.state.lifecycle, current.hypothesis_id, current.sequence)

    async def block_unknown_turn(self, index: int, error: Exception) -> None:
        """Acknowledge typed agent rejections; fence ambiguous provider acceptance."""
        if isinstance(error, AgentConversationOpenError):
            return
        known = isinstance(error, EvaluationAgentAccessError | StructuredResponseError)
        async with self.lock:
            current = self.state.workstreams[index]
            dispatched = [
                intent
                for intent in self.state.lifecycle.intents.values()
                if intent.scope_id == current.hypothesis_id
                and intent.generation == current.sequence
                and (intent.kind is IntentKind.TURN or (known and intent.kind is IntentKind.RESUME))
                and intent.stage is IntentStage.DISPATCHED
            ]
            if not dispatched:
                return
            for intent in dispatched:
                event = (
                    CompleteIntent(operation_id=intent.operation_id)
                    if known
                    else BlockIntent(operation_id=intent.operation_id)
                )
                reduced, _ = step(self.state, event)
                for name in type(self.state).model_fields:
                    setattr(self.state, name, getattr(reduced, name))
            await self.commit(
                f"dynamic: {current.hypothesis_id} turn acknowledged"
                if known
                else f"dynamic: {current.hypothesis_id} dispatch outcome unresolved"
            )
        if not known:
            message = (
                f"{current.hypothesis_id}: unresolved provider dispatch requires reconciliation"
            )
            raise RuntimeContractError(message) from error

    async def bind_evidence_reply[ReplyT: BaseModel](self, reply: ReplyT) -> ReplyT:
        """Preserve measured revision attribution before the core binds local references."""
        result = reply.root if isinstance(reply, RootModel) else reply
        if not isinstance(result, ImplementerResult):
            return reply
        evidence = []
        for reference in result.evidence:
            revision = await self.run.evaluation.evidence_revision(reference.location)
            evidence.append(reference if revision is None else reference.with_revision(revision))
        bound = result.model_copy(update={"evidence": tuple(evidence)})
        return (
            reply.model_copy(update={"root": bound})
            if isinstance(reply, RootModel)
            else reply.model_copy(update={"evidence": tuple(evidence)})
        )

    @property
    def cursors(self) -> AttemptEvaluationCursors:
        """Open the run-owned host ledger without adding legacy kernel state fields."""
        return AttemptEvaluationCursors(self.run.state.namespace("dynamic-attempt-evaluations"))

    async def initial_turn[ReplyT: BaseModel](
        self,
        workspace: CandidateWorkspace,
        session: AgentConversation,
        message: RenderedPrompt,
        response: type[ReplyT],
        invocation_id: str,
    ) -> ReplyT:
        """Persist the attempt history cursor before any initial provider execution."""
        if workspace.id is None:
            message_text = "attempt history requires an owned workspace"
            raise EvaluationSuspensionInvariantError(message_text)
        cursor = self.cursors.read(invocation_id)
        if cursor is not None and cursor.workspace_id != workspace.id:
            message_text = "initial attempt cursor belongs to another workspace"
            raise EvaluationSuspensionInvariantError(message_text)
        history = await self.run.evaluation.settlements().submission_history(workspace.id)
        if cursor is None:
            cursor = self.cursors.record(
                invocation_id=invocation_id,
                workspace_id=workspace.id,
                preceding_handles=tuple(report.handle_id for report in history),
            )
        elif (
            tuple(report.handle_id for report in history[: cursor.submitted_before])
            != cursor.preceding_handles
        ):
            message_text = "initial attempt evaluation history prefix changed"
            raise EvaluationSuspensionInvariantError(message_text)

        async def validate(reply: ReplyT) -> None:
            await self.validate_reply(session, reply)

        reply = await validated_turn(
            session, message, response, invocation_id=invocation_id, validate_response=validate
        )
        if isinstance(reply, RootModel) and not isinstance(
            reply.root, WaitingForEvaluation | WaitingForProfiler
        ):
            history = await self.run.evaluation.settlements().submission_history(workspace.id)
            if (
                tuple(report.handle_id for report in history[: cursor.submitted_before])
                != cursor.preceding_handles
            ):
                reason = render_evaluation_history_unavailable()
                await self.apply(AttemptBoundReached(operation_id=invocation_id, reason=reason))
                raise EvaluationAttemptHistoryUnavailableError(reason)
            repeated = _repeated_snapshots(
                tuple(_report_snapshot(report) for report in history[cursor.submitted_before :])
            )
            await self._enforce_bound(invocation_id, repeated)
        return await self.bind_evidence_reply(reply)

    def validated_session[ReplyT: BaseModel](
        self, session: AgentConversation, response: type[ReplyT]
    ) -> AgentConversation:
        """Bind semantic acceptance to the existing structured conversation boundary."""

        async def validate(reply: ReplyT) -> None:
            await self.validate_reply(session, reply)

        return validated_conversation(session, response, validate)

    async def validate_reply(self, session: AgentConversation, reply: BaseModel) -> None:
        """Authorize structured yield handles before acknowledging provider completion."""
        result = reply.root if isinstance(reply, RootModel) else reply
        if not isinstance(result, WaitingForEvaluation | WaitingForProfiler):
            if isinstance(result, ImplementerResult):
                for reference in result.evidence:
                    revision = await self.run.evaluation.evidence_revision(reference.location)
                    if revision is not None:
                        try:
                            reference.with_revision(revision)
                        except ValueError as error:
                            raise StructuredResponseError(
                                session.role.id, type(reply), detail=str(error)
                            ) from error
            return
        scope_id = session.workspace.id
        if scope_id is None:
            message = "evaluation wait requires an owned workspace"
            raise EvaluationSuspensionInvariantError(message)
        role = {
            IMPLEMENTER.id: EvaluationAgentRole.IMPLEMENTER,
            JUDGE.id: EvaluationAgentRole.JUDGE,
        }[session.role.id]
        principal = evaluation_principal(role, session.member_id, scope_id)
        validate = (
            self.run.evaluation.validate_profiler_wait
            if isinstance(result, WaitingForProfiler)
            else self.run.evaluation.validate_wait
        )
        await validate(result.handles, scope_id=scope_id, principal_id=principal)

    async def apply(self, event: EnvelopeEvent) -> tuple[LifecycleRequest, ...]:
        """Commit the entire reducer result before returning its requests."""
        async with self.lock:
            new_profiler_wait = (
                isinstance(event, WorkerAwaitingEvaluation)
                and event.continuation.continuation_id not in self.state.lifecycle.continuations
                and any(
                    isinstance(dependency, ProfilerDependency)
                    for dependency in event.continuation.dependencies
                )
            )
            try:
                updated, requests = step(self.state, event)
            except (ValueError, KeyError) as error:
                raise EvaluationSuspensionInvariantError(str(error)) from error
            for name in type(self.state).model_fields:
                setattr(self.state, name, getattr(updated, name))
            await self.commit(f"dynamic: evaluation suspension {type(event).__name__}")
            if new_profiler_wait and isinstance(event, WorkerAwaitingEvaluation):
                self.run.observations.note(
                    f"dynamic profiler wait: continuation={event.continuation.continuation_id} "
                    f"operations={','.join(dependency.handle for dependency in event.continuation.dependencies)}"
                )
            return requests

    async def yield_turn(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentConversation,
        reply: WaitingForEvaluation | WaitingForProfiler,
    ) -> None:
        """Validate submitted captures, retain WIP, then persist the yielded turn."""
        await self.validate_reply(session, reply)
        if workspace.id is None:
            message = "evaluation suspension requires an owned candidate workspace"
            raise EvaluationSuspensionInvariantError(message)
        if isinstance(reply, WaitingForProfiler):
            await self._yield_profiler(index, workspace, session, reply)
            return
        dependencies = OwnedEvaluationDependencies(
            scope_id=workspace.id,
            generation=await self.run.evaluation.submitted_generation(
                reply.handles[0], scope_id=workspace.id
            ),
            handles=reply.handles,
        )
        observations = await self.run.evaluation.settlements().observe(dependencies)
        captured = tuple(
            [
                EvaluationDependency(
                    handle=observation.handle_id,
                    scope_id=observation.scope_id,
                    generation=observation.generation,
                    candidate_revision=await self.run.evaluation.submitted_revision(
                        observation.handle_id
                    ),
                    **_fingerprints(observation),
                )
                for observation in observations
            ]
        )
        try:
            deadline_at_s = min(
                [await self.run.evaluation.submitted_deadline(handle) for handle in reply.handles]
            )
        except RuntimeContractError as error:
            await self.apply(BlockIntent(operation_id=self._active_invocation(index)))
            raise EvaluationSuspensionUnresolvedError(str(error)) from error
        await self._persist_yield(
            index, workspace, session, captured, (dependencies.generation, deadline_at_s)
        )

    async def _yield_profiler(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentConversation,
        reply: WaitingForProfiler,
    ) -> None:
        if workspace.id is None:
            message = "profiler wait requires an owned workspace"
            raise EvaluationSuspensionInvariantError(message)
        principal = evaluation_principal(
            EvaluationAgentRole.IMPLEMENTER, session.member_id, workspace.id
        )
        operations = tuple(
            [
                await self.run.evaluation.profiler_operation(
                    handle, principal_id=principal, scope_id=workspace.id
                )
                for handle in reply.handles
            ]
        )
        captured = tuple(
            ProfilerDependency(
                handle=operation.operation_id,
                scope_id=workspace.id,
                generation=0,
                candidate_revision=operation.candidate_snapshot_id,
                principal_id=principal,
                request_digest=operation.request_digest,
            )
            for operation in operations
        )
        # The profiler operation owns terminal settlement, while run control and
        # provider limits bound execution. It has no evaluation deadline.
        await self._persist_yield(index, workspace, session, captured, (0, None))

    async def _persist_yield(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentConversation,
        captured: tuple[EvaluationDependency | ProfilerDependency, ...],
        identity: tuple[int, float | None],
    ) -> None:
        generation, deadline_at_s = identity
        item = self.state.workstreams[index]
        if workspace.id is None:
            message = "dependency wait requires an owned workspace"
            raise EvaluationSuspensionInvariantError(message)
        revision = await workspace.snapshot(f"dynamic: {item.hypothesis_id} suspended WIP")
        await workspace.retain(revision, label=f"dynamic-{item.hypothesis_id}-suspended")
        invocation_id = self._active_invocation(index)
        role = "implementer" if session.role.id == "dynamic-implementer" else "judge"
        settled = {
            dependency.handle: (
                prior.settlements[dependency.handle],
                prior.evidence_ids.get(dependency.handle, ()),
            )
            for prior in self.state.lifecycle.continuations.values()
            for dependency in captured
            if dependency in prior.dependencies and dependency.handle in prior.settlements
        }
        continuation = DependencyContinuation(
            continuation_id=f"{invocation_id}/evaluation",
            scope_id=item.hypothesis_id,
            generation=item.sequence,
            role=role,
            session_key=str(session.session_key),
            yielded_invocation_id=invocation_id,
            retained_revision=revision,
            original_stage="implementing" if role == "implementer" else "implemented",
            evaluation_scope_id=workspace.id,
            evaluation_generation=generation,
            dependencies=captured,
            deadline_at_s=deadline_at_s,
            settlements={handle: outcome for handle, (outcome, _) in settled.items()},
            evidence_ids={handle: evidence for handle, (_, evidence) in settled.items()},
        )
        await self.apply(WorkerAwaitingEvaluation(continuation=continuation))

    def _active_invocation(self, index: int) -> str:
        item = self.state.workstreams[index]
        active = next(
            (
                intent
                for intent in reversed(tuple(self.state.lifecycle.intents.values()))
                if intent.scope_id == item.hypothesis_id
                and intent.generation == item.sequence
                and intent.kind in {IntentKind.TURN, IntentKind.RESUME}
                and intent.stage is IntentStage.DISPATCHED
            ),
            None,
        )
        if active is None:
            message = "evaluation suspension requires a dispatched agent turn"
            raise EvaluationSuspensionInvariantError(message)
        return active.operation_id

    async def reopen_evaluation_wait(
        self, continuation_id: str, resolved_cancelled_handles: tuple[str, ...]
    ) -> None:
        """Recover terminal requester results and reopen the same charged suspended attempt."""
        continuation = self.state.lifecycle.continuations[continuation_id]
        for dependency in continuation.dependencies:
            if isinstance(dependency, ProfilerDependency):
                await self._settle_profiler(continuation, dependency, wait=False)
                continue
            report = await self._read_report(
                dependency.handle, scope_id=dependency.scope_id, generation=dependency.generation
            )
            outcome = _stored_outcome(report)
            if outcome is None:
                message = f"parked evaluation {dependency.handle!r} remains unresolved"
                raise EvaluationSuspensionUnresolvedError(message)
            evidence_ids = await self.run.evaluation.accepted_evidence_ids(dependency.handle)
            _artifact_refs(report, evidence_ids, dependency)
            await self.apply(
                EvaluationSettled(
                    continuation_id=continuation_id,
                    scope_id=dependency.scope_id,
                    generation=dependency.generation,
                    handle=dependency.handle,
                    candidate_digest=dependency.candidate_digest,
                    evaluator_digest=dependency.evaluator_digest,
                    workload_digest=dependency.workload_digest,
                    environment_digest=dependency.environment_digest,
                    outcome=outcome,
                    evidence_ids=evidence_ids,
                    at_s=self.run.evaluation.current_time(),
                )
            )
        await self.apply(evaluation_wait_reopen(continuation_id, resolved_cancelled_handles))
        operation_id = f"{continuation.park_operation_id}/reopen"
        await self.apply(DispatchIntent(operation_id=operation_id))
        await self.run.evaluation.reopen_jobs(continuation.scope_id)
        await self.apply(CompleteIntent(operation_id=operation_id))

    async def run_wait(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentConversation,
    ) -> tuple[ImplementerResult | ReviewResult, str]:
        """Observe without invoking agents, then execute one same-session continuation."""
        item = self.state.workstreams[index]
        while True:
            requests = await self.apply(RecoveryStarted())
            owned = tuple(
                request
                for request in requests
                if isinstance(
                    request,
                    ObserveEvaluations | ResumeAgentTurn | CancelEvaluation | InspectEvaluation,
                )
                and request.scope_id == item.hypothesis_id
                and request.generation == item.sequence
            )
            if not owned:
                message = "evaluation continuation has no dispatch authority"
                raise EvaluationSuspensionUnresolvedError(message)
            request = owned[0]
            if isinstance(request, CancelEvaluation | InspectEvaluation):
                await self._terminate(request)
                continue
            if isinstance(request, ObserveEvaluations):
                await self._observe(request)
                continue
            repeated = await self._attempt_failure(request, workspace)
            await self._enforce_bound(request.operation_id, repeated)
            try:
                reply = await self._resume(request, session, repeated)
            except (EvaluationAgentAccessError, StructuredResponseError) as error:
                reason = f"{type(error).__name__}: {error}"
                await self.apply(
                    AttemptBoundReached(operation_id=request.operation_id, reason=reason)
                )
                raise EvaluationAttemptBoundError(reason) from error
            if isinstance(reply, WaitingForEvaluation | WaitingForProfiler):
                known = {
                    dependency.handle
                    for continuation in _attempt_chain(self.state, request.continuation)
                    for dependency in continuation.dependencies
                }
                if not set(reply.handles) - known:
                    reason = render_evaluation_no_progress()
                    await self.apply(
                        AttemptBoundReached(operation_id=request.operation_id, reason=reason)
                    )
                    raise EvaluationAttemptBoundError(reason)
                await self.yield_turn(index, workspace, session, reply)
                continue
            await self._enforce_bound(
                request.operation_id, await self._attempt_failure(request, workspace)
            )
            return reply, request.operation_id

    async def _enforce_bound(self, operation_id: str, repeated: RepeatedFailure | None) -> None:
        if repeated is not None and repeated.count >= self.max_repeated_failures:
            reason = render_evaluation_resume_bound(_repeat_line(repeated))
            await self.apply(AttemptBoundReached(operation_id=operation_id, reason=reason))
            raise EvaluationAttemptBoundError(reason, repeated)

    async def _attempt_failure(
        self, request: ResumeAgentTurn, workspace: CandidateWorkspace
    ) -> RepeatedFailure | None:
        """Read complete attempt submissions from its immutable durable cursor after restart."""
        chain = _attempt_chain(self.state, request.continuation)
        root_id = chain[0].yielded_invocation_id
        cursor = self.cursors.read(root_id)
        if cursor is None and request.continuation.role == "judge":
            # Host TURN identities encode their declared role. The latest
            # implementer preceding this judge owns the same charged attempt.
            judge_sequence = _invocation_sequence(root_id)
            roots = tuple(
                (sequence, intent.operation_id)
                for intent in self.state.lifecycle.intents.values()
                if intent.kind is IntentKind.TURN
                and intent.scope_id == request.scope_id
                and intent.generation == request.generation
                and f"/{IMPLEMENTER.id}/" in intent.operation_id
                and judge_sequence is not None
                and (sequence := _invocation_sequence(intent.operation_id)) is not None
                and sequence < judge_sequence
            )
            if roots:
                _, root_id = max(roots)
            cursor = self.cursors.read(root_id)
        if cursor is None:
            reason = render_evaluation_history_unavailable()
            await self.apply(AttemptBoundReached(operation_id=request.operation_id, reason=reason))
            raise EvaluationAttemptHistoryUnavailableError(reason)
        if cursor.workspace_id != workspace.id:
            message = "attempt evaluation cursor belongs to another workspace"
            raise EvaluationSuspensionInvariantError(message)
        history = await self.run.evaluation.settlements().submission_history(cursor.workspace_id)
        if (
            tuple(report.handle_id for report in history[: cursor.submitted_before])
            != cursor.preceding_handles
        ):
            reason = render_evaluation_history_unavailable()
            await self.apply(AttemptBoundReached(operation_id=request.operation_id, reason=reason))
            raise EvaluationAttemptHistoryUnavailableError(reason)
        handles = dict.fromkeys(
            dependency.handle
            for continuation in chain
            for dependency in continuation.dependencies
            if dependency.handle in continuation.settlements
        )
        reports = tuple(
            [
                await self._read_report(
                    dependency.handle,
                    scope_id=dependency.scope_id,
                    generation=dependency.generation,
                )
                for continuation in chain
                for dependency in continuation.dependencies
                if isinstance(dependency, EvaluationDependency) and dependency.handle in handles
            ]
        )
        by_handle = {report.handle_id: report for report in reports}
        for continuation in chain:
            for dependency in continuation.dependencies:
                if isinstance(dependency, EvaluationDependency) and dependency.handle in by_handle:
                    _validate_report(continuation, dependency, by_handle[dependency.handle])
        return _repeated_snapshots(
            tuple(_report_snapshot(report) for report in history[cursor.submitted_before :])
        )

    async def _observe(self, request: ObserveEvaluations) -> None:
        if request.stage is IntentStage.PREPARED:
            authorized = await self.apply(DispatchIntent(operation_id=request.operation_id))
            if not any(isinstance(item, ObserveEvaluations) for item in authorized):
                message = "evaluation observation dispatch is fenced"
                raise EvaluationSuspensionInvariantError(message)
        continuation = request.continuation
        profilers = tuple(
            item for item in continuation.dependencies if isinstance(item, ProfilerDependency)
        )
        if profilers:
            await self._observe_profilers(continuation, profilers)
            return
        unsettled = tuple(
            dependency.handle
            for dependency in continuation.dependencies
            if dependency.handle not in continuation.settlements
        )
        dependencies = OwnedEvaluationDependencies(
            scope_id=continuation.evaluation_scope_id,
            generation=continuation.evaluation_generation,
            handles=unsettled,
        )
        observed = await self.run.evaluation.settlements().observe(dependencies)
        await self._record_observations(continuation, observed)
        if self.state.lifecycle.continuations[continuation.continuation_id].ready_to_resume:
            return
        if continuation.deadline_at_s is None:
            message = "evaluation dependency lost its deadline"
            raise EvaluationSuspensionInvariantError(message)
        waiting = asyncio.create_task(self.run.evaluation.settlements().wait_any(dependencies))
        deadline = asyncio.create_task(self.run.evaluation.wait_until(continuation.deadline_at_s))
        try:
            done, _ = await asyncio.wait({waiting, deadline}, return_when=asyncio.FIRST_COMPLETED)
            if deadline in done:
                await deadline
                await self.apply(
                    DeadlineReached(
                        continuation_id=continuation.continuation_id,
                        at_s=self.run.evaluation.current_time(),
                    )
                )
            else:
                await self._record_observations(continuation, await waiting)
        finally:
            for task in (waiting, deadline):
                if not task.done():
                    task.cancel()
            await asyncio.gather(waiting, deadline, return_exceptions=True)

    async def _observe_profilers(
        self, continuation: DependencyContinuation, dependencies: tuple[ProfilerDependency, ...]
    ) -> None:
        for dependency in dependencies:
            if dependency.handle not in continuation.settlements:
                await self._settle_profiler(continuation, dependency, wait=True)

    async def _settle_profiler(
        self, continuation: DependencyContinuation, dependency: ProfilerDependency, *, wait: bool
    ) -> None:
        observe = (
            self.run.evaluation.wait_profiler if wait else self.run.evaluation.profiler_operation
        )
        operation = await observe(
            dependency.handle, principal_id=dependency.principal_id, scope_id=dependency.scope_id
        )
        if not operation.state.terminal and not wait:
            return
        if not operation.state.terminal:
            message = f"profiler operation {dependency.handle!r} remains unresolved"
            raise EvaluationSuspensionUnresolvedError(message)
        await self.apply(
            ProfilerSettled(
                continuation_id=continuation.continuation_id,
                handle=dependency.handle,
                principal_id=dependency.principal_id,
                request_digest=operation.request_digest,
                outcome=_profiler_outcome(operation),
            )
        )

    async def _record_observations(
        self,
        continuation: DependencyContinuation,
        observations: tuple[EvaluationSettlementObservation, ...],
    ) -> None:
        for observation in observations:
            outcome = _terminal_outcome(observation)
            if (
                observation.handle_id
                in self.state.lifecycle.continuations[continuation.continuation_id].settlements
            ):
                continue
            identity: _ObservationIdentity = dict(
                continuation_id=continuation.continuation_id,
                scope_id=observation.scope_id,
                generation=observation.generation,
                handle=observation.handle_id,
                at_s=self.run.evaluation.current_time(),
                observation_state=_observation_state(observation),
                stage=observation.stage,
                queued_seconds=observation.queued_seconds,
                ran_seconds=observation.ran_seconds,
                pending_reason=observation.pending_reason,
                estimated_start_s=observation.estimated_start_s,
                **_fingerprints(observation),
            )
            event = (
                EvaluationObserved(**identity)
                if outcome is None
                else EvaluationSettled(
                    **identity,
                    outcome=outcome,
                    evidence_ids=await self.run.evaluation.accepted_evidence_ids(
                        observation.handle_id
                    ),
                )
            )
            await self.apply(event)
            if isinstance(observation.result, EvaluationUnknown) and not (
                self.state.lifecycle.continuations[continuation.continuation_id].ready_to_resume
            ):
                raise EvaluationSuspensionUnresolvedError(observation.result.detail)

    async def _terminate(self, request: CancelEvaluation | InspectEvaluation) -> None:
        if request.stage is IntentStage.PREPARED:
            authorized = await self.apply(DispatchIntent(operation_id=request.operation_id))
            matching = tuple(item for item in authorized if isinstance(item, type(request)))
            if not matching:
                message = "evaluation termination dispatch is fenced"
                raise EvaluationSuspensionInvariantError(message)
        try:
            dependency = next(
                item for item in request.continuation.dependencies if item.handle == request.handle
            )
            if isinstance(dependency, ProfilerDependency):
                if isinstance(request, CancelEvaluation):
                    await self.run.evaluation.cancel_profiler(
                        request.handle,
                        principal_id=dependency.principal_id,
                        scope_id=dependency.scope_id,
                    )
                    await self.apply(CompleteIntent(operation_id=request.operation_id))
                else:
                    operation = await self.run.evaluation.profiler_operation(
                        request.handle,
                        principal_id=dependency.principal_id,
                        scope_id=dependency.scope_id,
                    )
                    await self.apply(
                        EvaluationInspected(
                            operation_id=request.operation_id,
                            outcome=_profiler_outcome(operation)
                            if operation.state.terminal
                            else EvaluationOutcome.UNKNOWN,
                            observation_state="running",
                        )
                    )
                return
            if isinstance(request, CancelEvaluation):
                await self.run.evaluation.cancel_submitted(
                    request.handle, scope_id=request.continuation.evaluation_scope_id
                )
                await self.apply(CompleteIntent(operation_id=request.operation_id))
            else:
                observations = await self.run.evaluation.settlements().inspect(
                    OwnedEvaluationDependencies(
                        scope_id=request.continuation.evaluation_scope_id,
                        generation=request.continuation.evaluation_generation,
                        handles=(request.handle,),
                    )
                )
                observation = observations[0]
                await self.apply(
                    EvaluationInspected(
                        operation_id=request.operation_id,
                        outcome=_terminal_outcome(observation) or EvaluationOutcome.UNKNOWN,
                        observation_state=_observation_state(observation),
                    )
                )
        except EvaluationSuspensionInvariantError:
            raise
        except (RuntimeContractError, OSError, ValueError):
            await self.apply(BlockIntent(operation_id=request.operation_id))

    async def _resume(
        self, request: ResumeAgentTurn, session: AgentConversation, repeated: RepeatedFailure | None
    ) -> ImplementerResult | ReviewResult | WaitingForEvaluation | WaitingForProfiler:
        continuation = request.continuation
        if str(session.session_key) != continuation.session_key:
            message = "evaluation resume changed its session key"
            raise EvaluationSuspensionInvariantError(message)
        response = (
            RootModel[ImplementerReply]
            if continuation.role == "implementer"
            else RootModel[JudgeReply]
        )
        reports = {
            dependency.handle: await self._read_report(
                dependency.handle,
                scope_id=dependency.scope_id,
                generation=dependency.generation,
            )
            for dependency in continuation.dependencies
            if isinstance(dependency, EvaluationDependency)
            and dependency.handle in continuation.settlements
        }
        for dependency in continuation.dependencies:
            if not isinstance(dependency, EvaluationDependency) or dependency.handle not in reports:
                continue
            _validate_report(continuation, dependency, reports[dependency.handle])
        artifacts = {
            dependency.handle: _artifact_refs(
                reports[dependency.handle],
                continuation.evidence_ids.get(dependency.handle, ()),
                dependency,
            )
            for dependency in continuation.dependencies
            if isinstance(dependency, EvaluationDependency) and dependency.handle in reports
        }
        profiler_results = tuple(
            [
                await self._profiler_resume_line(dependency)
                for dependency in continuation.dependencies
                if isinstance(dependency, ProfilerDependency)
            ]
        )
        if request.reconcile_only:
            outcome = session.inspect(request.invocation_id)
        else:
            authorized = await self.apply(DispatchIntent(operation_id=request.operation_id))
            if not any(isinstance(item, ResumeAgentTurn) for item in authorized):
                message = "evaluation resume dispatch is fenced"
                raise EvaluationSuspensionInvariantError(message)
            notes = tuple(
                note
                for note in steers.pending(self.state, request.scope_id)
                if note.reserved_to == request.invocation_id
            )
            outcome = await self._continue_session(
                session,
                request,
                render_evaluation_resume(
                    role=continuation.role,
                    retained_revision=continuation.retained_revision,
                    results=tuple(
                        EvaluationResumeLine(
                            handle_id=dependency.handle,
                            status=agent_evaluation(reports[dependency.handle]).status.value
                            if dependency.handle in reports
                            else "timed_out",
                            candidate_revision=dependency.candidate_revision,
                            evaluator_revision=dependency.evaluator_digest,
                            evidence_ids=continuation.evidence_ids.get(dependency.handle, ()),
                            artifact_refs=artifacts.get(dependency.handle, ()),
                            detail=_trusted_report(reports[dependency.handle]).model_dump_json()
                            if dependency.handle in reports
                            else continuation.timed_out.model_dump_json()
                            if continuation.timed_out is not None
                            else "",
                            repeated_failure=_repeat_line(repeated) if repeated else None,
                            diagnostics=tuple(
                                stage.model_dump_json()
                                for stage in reports[dependency.handle].stage_results
                                if stage.state is not StageState.SUCCEEDED
                                and stage.result is not None
                            )
                            if dependency.handle in reports
                            else (),
                        )
                        for dependency in continuation.dependencies
                        if isinstance(dependency, EvaluationDependency)
                    )
                    + profiler_results,
                    notes=notes,
                    timed_out=continuation.timed_out,
                ),
                response,
            )
        if not isinstance(outcome, Completed):
            if isinstance(outcome, Unknown):
                await self.apply(BlockIntent(operation_id=request.operation_id))
            message = "evaluation resume acceptance requires reconciliation"
            raise EvaluationSuspensionUnresolvedError(message)
        try:
            reply = response.model_validate_json(outcome.result.text)
        except ValueError as error:
            await self.apply(BlockIntent(operation_id=request.operation_id))
            raise EvaluationSuspensionUnresolvedError(str(error)) from error
        return (
            await self._validate_resumed_reply(session, reply, response, request.operation_id)
        ).root

    async def _profiler_resume_line(self, dependency: ProfilerDependency) -> EvaluationResumeLine:
        operation = await self.run.evaluation.profiler_operation(
            dependency.handle, principal_id=dependency.principal_id, scope_id=dependency.scope_id
        )
        return EvaluationResumeLine(
            handle_id=dependency.handle,
            status=operation.state.value,
            candidate_revision=dependency.candidate_revision,
            evaluator_revision=dependency.request_digest,
            dependency_kind="profiler",
            evidence_ids=(),
            artifact_refs=(),
            detail=operation.model_dump_json(),
        )

    async def _validate_resumed_reply[ReplyT: BaseModel](
        self, session: AgentConversation, reply: ReplyT, response: type[ReplyT], operation_id: str
    ) -> ReplyT:
        """Correct a completed reply's typed agent errors without fencing dispatch."""
        try:
            await self.validate_reply(session, reply)
        except (EvaluationAgentAccessError, StructuredResponseError) as error:

            async def validate(corrected: ReplyT) -> None:
                await self.validate_reply(session, corrected)

            validated = await validated_turn(
                session,
                render_evaluation_wait_error(error=str(error)),
                response,
                invocation_id=f"{operation_id}/wait-correction",
                validate_response=validate,
            )
        else:
            validated = reply
        return await self.bind_evidence_reply(validated)

    async def _read_report(
        self, handle: str, *, scope_id: str, generation: int
    ) -> StoredEvaluation:
        try:
            associated_generation = await self.run.evaluation.submitted_generation(
                handle, scope_id=scope_id
            )
            report = StoredEvaluation.model_validate_json(
                await self.run.evaluation.submitted_report(handle, scope_id=scope_id)
            )
        except (RuntimeContractError, ValueError, OSError) as error:
            raise EvaluationSuspensionUnresolvedError(str(error)) from error
        if associated_generation != generation:
            message = "evaluation resume requester generation differs from its dependency"
            raise EvaluationSuspensionInvariantError(message)
        return report

    async def _continue_session(
        self,
        session: AgentConversation,
        request: ResumeAgentTurn,
        message: RenderedPrompt,
        response: type[BaseModel],
    ) -> InvocationOutcome:
        try:
            return await session.resume(message, request.invocation_id, response=response)
        except (
            SessionConfigurationError,
            SessionResumeError,
            InvocationConflictError,
            StructuredResponseError,
            SessionPersistenceError,
            SessionTransportUnavailableError,
        ) as error:
            await self.apply(BlockIntent(operation_id=request.operation_id))
            raise EvaluationSuspensionUnresolvedError(str(error)) from error


def _artifact_refs(
    report: StoredEvaluation,
    evidence_ids: tuple[str, ...],
    dependency: EvaluationDependency,
) -> tuple[str, ...]:
    try:
        evidence = tuple(
            TrustedEvidence.model_validate(stage.result)
            for stage in report.stage_results
            if stage.state is StageState.SUCCEEDED and stage.result is not None
        )
    except ValueError as error:
        raise EvaluationSuspensionUnresolvedError(str(error)) from error
    if any(
        item.evidence_id not in evidence_ids
        or item.evaluation_id != dependency.handle
        or (
            item.fingerprints.candidate.value,
            item.fingerprints.evaluator.value,
            item.fingerprints.workload.value,
            item.fingerprints.environment.value,
        )
        != (
            dependency.candidate_digest,
            dependency.evaluator_digest,
            dependency.workload_digest,
            dependency.environment_digest,
        )
        for item in evidence
    ):
        message = "evaluation resume contains unaccepted semantic evidence"
        raise EvaluationSuspensionInvariantError(message)
    return tuple(artifact.path for item in evidence for artifact in item.artifacts)


def _trusted_report(report: StoredEvaluation) -> StoredEvaluation:
    """Keep failed-stage payloads outside the accepted evidence report."""
    return report.model_copy(
        update={
            "stage_results": tuple(
                stage
                if stage.state is StageState.SUCCEEDED
                else stage.model_copy(update={"result": None})
                for stage in report.stage_results
            )
        }
    )


def _stored_outcome(report: StoredEvaluation) -> EvaluationOutcome | None:
    match report.state:
        case EvaluationState.SUCCEEDED:
            return EvaluationOutcome.SUCCEEDED
        case EvaluationState.FAILED:
            return EvaluationOutcome.FAILED
        case EvaluationState.CANCELED | EvaluationState.SUPERSEDED:
            return EvaluationOutcome.CANCELLED
        case EvaluationState.QUEUED | EvaluationState.STARTING | EvaluationState.RUNNING:
            return None


class _EvaluationDigests(TypedDict):
    candidate_digest: str
    evaluator_digest: str
    workload_digest: str
    environment_digest: str


class _ObservationIdentity(_EvaluationDigests):
    continuation_id: str
    scope_id: str
    generation: int
    handle: str
    at_s: float
    observation_state: Literal["pending", "running", "unknown"]
    stage: Literal["queued", "framework_setup", "accuracy", "benchmark", "profile"] | None
    queued_seconds: float | None
    ran_seconds: float | None
    pending_reason: str | None
    estimated_start_s: float | None


def _fingerprints(observation: EvaluationSettlementObservation) -> _EvaluationDigests:
    fingerprints = observation.fingerprints
    return {
        "candidate_digest": fingerprints.candidate.value,
        "evaluator_digest": fingerprints.evaluator.value,
        "workload_digest": fingerprints.workload.value,
        "environment_digest": fingerprints.environment.value,
    }


def _terminal_outcome(observation: EvaluationSettlementObservation) -> EvaluationOutcome | None:
    match observation.result:
        case EvaluationCompleted():
            return EvaluationOutcome.SUCCEEDED
        case EvaluationFailed():
            return EvaluationOutcome.FAILED
        case EvaluationCanceled():
            return EvaluationOutcome.CANCELLED
    return None


__all__ = [
    "AttemptBoundKind",
    "EvaluationAttemptBoundError",
    "EvaluationAttemptHistoryUnavailableError",
    "EvaluationSuspension",
    "EvaluationSuspensionInvariantError",
    "EvaluationSuspensionUnresolvedError",
    "PlanningObservations",
    "gather_planning_observations",
    "repeated_evaluation_failures",
    "repeated_measurement_failure",
]


def _observation_state(
    observation: EvaluationSettlementObservation,
) -> Literal["pending", "running", "unknown"]:
    if isinstance(observation.result, EvaluationPending):
        return "running" if observation.result.state is EvaluationState.RUNNING else "pending"
    return "unknown"


def _repeat_line(repeated: RepeatedFailure) -> RepeatedFailureLine:
    return RepeatedFailureLine(
        kind=repeated.kind.value,
        stage=repeated.stage.value if repeated.stage else None,
        signature=repeated.signature,
        count=repeated.count,
        instruction=repeated.instruction,
    )


def _attempt_chain(
    state: DynamicState, current: DependencyContinuation
) -> tuple[DependencyContinuation, ...]:
    """Follow durable invocation ancestry; retries have distinct initial TURN identities."""
    predecessors = {
        f"{item.continuation_id}/resume": item for item in state.lifecycle.continuations.values()
    }
    chain = [current]
    while current.yielded_invocation_id in predecessors:
        current = predecessors[current.yielded_invocation_id]
        chain.append(current)
    return tuple(reversed(chain))


def _repeated_snapshots(
    snapshots: tuple[EvaluationOperationSnapshot, ...],
) -> RepeatedFailure | None:
    """A stage's last failure is checked even when another stage most recently passed."""
    initial = detect_repeated_failure(snapshots)
    repeats: list[RepeatedFailure] = []
    if initial is not None:
        repeats.append(initial)
    for kind in EvidenceKind:
        relevant = tuple(
            snapshot.model_copy(
                update={
                    "stage_outcomes": tuple(
                        stage for stage in snapshot.stage_outcomes if stage.kind is kind
                    ),
                    "state": EvaluationState.SUCCEEDED
                    if any(
                        stage.kind is kind and stage.outcome is EvidenceOutcome.PASSED
                        for stage in snapshot.stage_outcomes
                    )
                    else snapshot.state,
                    "failure": snapshot.failure
                    if any(
                        stage.kind is kind and stage.outcome is EvidenceOutcome.FAILED
                        for stage in snapshot.stage_outcomes
                    )
                    or not snapshot.stage_outcomes
                    else None,
                }
            )
            for snapshot in snapshots
            if any(stage.kind is kind for stage in snapshot.stage_outcomes)
        )
        repeated = detect_repeated_failure(relevant)
        if repeated is not None:
            repeats.append(repeated)
    if not repeats:
        return None
    return max(repeats, key=lambda repeated: repeated.count)


def repeated_evaluation_failures(evaluations: Sequence[AgentEvaluation]) -> RepeatedFailure | None:
    """Apply stage-specific repeated-failure guidance to the final reply's evaluation history."""
    snapshots = tuple(_agent_snapshot(index, item) for index, item in enumerate(evaluations))
    return _repeated_snapshots(snapshots)


def _validate_report(
    continuation: DependencyContinuation, dependency: EvaluationDependency, report: StoredEvaluation
) -> None:
    if (
        report.handle_id != dependency.handle
        or _stored_outcome(report) != continuation.settlements[dependency.handle]
    ):
        message = "evaluation resume report differs from its settled dependency"
        raise EvaluationSuspensionInvariantError(message)
    _artifact_refs(report, continuation.evidence_ids.get(dependency.handle, ()), dependency)
    try:
        diagnostics = tuple(
            TrustedEvidence.model_validate(stage.result)
            for stage in report.stage_results
            if stage.state is StageState.FAILED and stage.result is not None
        )
    except ValueError as error:
        raise EvaluationSuspensionUnresolvedError(str(error)) from error
    if any(
        item.evaluation_id != dependency.handle
        or (
            item.fingerprints.candidate.value,
            item.fingerprints.evaluator.value,
            item.fingerprints.workload.value,
            item.fingerprints.environment.value,
        )
        != (
            dependency.candidate_digest,
            dependency.evaluator_digest,
            dependency.workload_digest,
            dependency.environment_digest,
        )
        for item in diagnostics
    ):
        message = "evaluation continuation diagnostic differs from its submitted capture"
        raise EvaluationSuspensionInvariantError(message)


def repeated_measurement_failure(evaluations: Sequence[AgentEvaluation]) -> RepeatedFailure | None:
    """Return the repeated partial-rate failure for final replies retaining legacy traceback signatures."""
    repeated = repeated_evaluation_failures(evaluations)
    return repeated if repeated is not None and repeated.kind is FailureKind.MEASUREMENT else None


def _agent_snapshot(index: int, item: AgentEvaluation) -> EvaluationOperationSnapshot:
    states = {
        AgentEvaluationStatus.PASSED: EvaluationState.SUCCEEDED,
        AgentEvaluationStatus.FAILED: EvaluationState.FAILED,
        AgentEvaluationStatus.PENDING: EvaluationState.RUNNING,
        AgentEvaluationStatus.CANCELED: EvaluationState.CANCELED,
    }
    return EvaluationOperationSnapshot(
        handle_id=str(index),
        state=states[item.status],
        evidence_recorded=False,
        failure=item.failure,
        stage_outcomes=tuple(
            EvaluationStageOutcome(
                kind=EvidenceKind(stage.kind),
                outcome=EvidenceOutcome(stage.outcome.value),
                partial_measurement=stage.partial_measurement,
            )
            for stage in item.stages
        ),
    )


def _invocation_sequence(identity: str) -> int | None:
    _, separator, sequence = identity.rpartition("/invocation-")
    return int(sequence) if separator and sequence.isdigit() else None


def _report_snapshot(report: StoredEvaluation) -> EvaluationOperationSnapshot:
    """Retain failed diagnostic partials without accepting them as passing evidence."""
    capture = {
        stage.name: SemanticEvaluationStage.model_validate(stage.payload)
        for stage in report.request.stages
    }
    outcomes = []
    for stage in report.stage_results:
        submitted = capture.get(stage.name)
        if stage.state is StageState.FAILED and stage.result is None and submitted is not None:
            outcomes.append(
                EvaluationStageOutcome(kind=submitted.kind, outcome=EvidenceOutcome.FAILED)
            )
        if stage.result is None or stage.state not in {StageState.SUCCEEDED, StageState.FAILED}:
            continue
        item = TrustedEvidence.model_validate(stage.result)
        if (
            submitted is None
            or item.stage_name != stage.name
            or item.evaluation_id != report.handle_id
            or item.kind is not submitted.kind
            or item.fingerprints != submitted.fingerprints
            or (stage.state is StageState.FAILED and item.outcome is not EvidenceOutcome.FAILED)
        ):
            message = "attempt evaluation diagnostic differs from its submitted capture"
            raise EvaluationSuspensionInvariantError(message)
        outcomes.append(
            EvaluationStageOutcome(
                kind=item.kind,
                outcome=item.outcome,
                partial_measurement=item.partial_measurement,
                summary_tail=item.semantic_summary[-MAX_STAGE_SUMMARY_TAIL_CHARS:]
                if item.semantic_summary
                else None,
            )
        )
    return EvaluationOperationSnapshot(
        handle_id=report.handle_id,
        state=report.state,
        evidence_recorded=False,
        failure=agent_evaluation(report).failure,
        stage_outcomes=tuple(outcomes),
    )


def _profiler_outcome(operation: ProfilerOperation) -> EvaluationOutcome:
    """Project operation failure separately from advisory profiler conclusions."""
    if operation.state is ProfilerOperationState.COMPLETED:
        return EvaluationOutcome.SUCCEEDED
    if operation.state in {ProfilerOperationState.CANCELED, ProfilerOperationState.INTERRUPTED}:
        return EvaluationOutcome.CANCELLED
    return EvaluationOutcome.FAILED
