"""Persist core suspension requests before calling evaluation and session interfaces.

EvaluationSuspension returns a completed reply and its pending acknowledgement.
The caller commits that acknowledgement with the scientific stage result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict

from pydantic import RootModel

from vibesys.orchestration.dynamic import steers
from vibesys.orchestration.dynamic.lifecycle import (
    BlockIntent,
    CancelEvaluation,
    CompleteIntent,
    DispatchIntent,
    EvaluationContinuation,
    EvaluationDependency,
    EvaluationOutcome,
    InspectEvaluation,
    IntentKind,
    IntentStage,
    ObserveEvaluations,
    RecoveryStarted,
    ResumeAgentTurn,
)
from vibesys.orchestration.dynamic.models import ImplementerReply, JudgeReply, WaitingForEvaluation
from vibesys.orchestration.dynamic.prompts import EvaluationResumeLine, render_evaluation_resume
from vibesys.orchestration.dynamic.transitions import (
    DeadlineReached,
    EnvelopeEvent,
    EvaluationInspected,
    EvaluationSettled,
    WorkerAwaitingEvaluation,
    step,
)
from vs_agent.api import (
    Completed,
    InvocationConflictError,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
    Unknown,
)
from vs_evaluation.api import (
    EvaluationCanceled,
    EvaluationCompleted,
    EvaluationFailed,
    EvaluationPending,
    EvaluationState,
    EvaluationUnknown,
    OwnedEvaluationDependencies,
    StoredEvaluation,
    TrustedEvidence,
)
from vs_runtime.api import RuntimeContractError, SessionTransportUnavailableError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import BaseModel

    from vibesys.orchestration.dynamic.lifecycle import LifecycleRequest
    from vibesys.orchestration.dynamic.models import DynamicState, ImplementerResult, ReviewResult
    from vs_agent.api import InvocationOutcome
    from vs_evaluation.api import EvaluationSettlementObservation
    from vs_prompts.api import RenderedPrompt
    from vs_runtime.api import AgentSession, CandidateWorkspace, Run


class EvaluationSuspensionUnresolvedError(RuntimeContractError):
    """Durable continuation remains owned until acceptance is reconciled."""


@dataclass(slots=True)
class EvaluationSuspension:
    """Execute only requests authorized by the atomically committed envelope."""

    run: Run
    state: DynamicState
    lock: asyncio.Lock
    commit: Callable[[str], Awaitable[None]]

    async def apply(self, event: EnvelopeEvent) -> tuple[LifecycleRequest, ...]:
        """Commit the entire reducer result before returning its requests."""
        async with self.lock:
            updated, requests = step(self.state, event)
            for name in type(self.state).model_fields:
                setattr(self.state, name, getattr(updated, name))
            await self.commit(f"dynamic: evaluation suspension {type(event).__name__}")
            return requests

    async def yield_turn(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentSession,
        reply: WaitingForEvaluation,
    ) -> None:
        """Validate submitted captures, retain WIP, then persist the yielded turn."""
        item = self.state.workstreams[index]
        if workspace.id is None:
            message = "evaluation suspension requires an owned candidate workspace"
            raise EvaluationSuspensionUnresolvedError(message)
        dependencies = OwnedEvaluationDependencies(
            scope_id=workspace.id,
            generation=await self.run.evaluation.submitted_generation(reply.handles[0]),
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
        revision = await workspace.snapshot(f"dynamic: {item.hypothesis_id} suspended WIP")
        await workspace.retain(revision, label=f"dynamic-{item.hypothesis_id}-suspended")
        active = next(
            intent
            for intent in reversed(tuple(self.state.lifecycle.intents.values()))
            if intent.scope_id == item.hypothesis_id
            and intent.generation == item.sequence
            and intent.kind in {IntentKind.TURN, IntentKind.RESUME}
            and intent.stage is IntentStage.DISPATCHED
        )
        try:
            deadline_at_s = min(
                [await self.run.evaluation.submitted_deadline(handle) for handle in reply.handles]
            )
        except RuntimeContractError as error:
            await self.apply(BlockIntent(operation_id=active.operation_id))
            raise EvaluationSuspensionUnresolvedError(str(error)) from error
        role = "implementer" if session.role.id == "dynamic-implementer" else "judge"
        continuation = EvaluationContinuation(
            continuation_id=f"{active.operation_id}/evaluation",
            scope_id=item.hypothesis_id,
            generation=item.sequence,
            role=role,
            session_key=str(session.session_key),
            yielded_invocation_id=active.operation_id,
            retained_revision=revision,
            original_stage="implementing" if role == "implementer" else "implemented",
            evaluation_scope_id=workspace.id,
            evaluation_generation=dependencies.generation,
            dependencies=captured,
            deadline_at_s=deadline_at_s,
        )
        await self.apply(WorkerAwaitingEvaluation(continuation=continuation))

    async def run_wait(
        self,
        index: int,
        workspace: CandidateWorkspace,
        session: AgentSession,
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
            reply = await self._resume(request, session)
            if isinstance(reply, WaitingForEvaluation):
                await self.yield_turn(index, workspace, session, reply)
                continue
            return reply, request.operation_id

    async def _observe(self, request: ObserveEvaluations) -> None:
        if request.stage is IntentStage.PREPARED:
            authorized = await self.apply(DispatchIntent(operation_id=request.operation_id))
            if not any(isinstance(item, ObserveEvaluations) for item in authorized):
                message = "evaluation observation dispatch is fenced"
                raise EvaluationSuspensionUnresolvedError(message)
        continuation = request.continuation
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

    async def _record_observations(
        self,
        continuation: EvaluationContinuation,
        observations: tuple[EvaluationSettlementObservation, ...],
    ) -> None:
        for observation in observations:
            outcome = _terminal_outcome(observation)
            await self.apply(
                EvaluationSettled(
                    continuation_id=continuation.continuation_id,
                    scope_id=observation.scope_id,
                    generation=observation.generation,
                    handle=observation.handle_id,
                    outcome=outcome or EvaluationOutcome.UNKNOWN,
                    at_s=self.run.evaluation.current_time(),
                    observation_state=_observation_state(observation),
                    stage=observation.stage,
                    queued_seconds=observation.queued_seconds,
                    ran_seconds=observation.ran_seconds,
                    pending_reason=observation.pending_reason,
                    estimated_start_s=observation.estimated_start_s,
                    evidence_ids=(
                        await self.run.evaluation.accepted_evidence_ids(observation.handle_id)
                        if outcome is not None
                        else ()
                    ),
                    **_fingerprints(observation),
                )
            )
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
                raise EvaluationSuspensionUnresolvedError(message)
        try:
            if isinstance(request, CancelEvaluation):
                await self.run.evaluation.cancel_submitted(request.handle)
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
        except (RuntimeContractError, OSError, ValueError):
            await self.apply(BlockIntent(operation_id=request.operation_id))

    async def _resume(
        self, request: ResumeAgentTurn, session: AgentSession
    ) -> ImplementerResult | ReviewResult | WaitingForEvaluation:
        continuation = request.continuation
        if str(session.session_key) != continuation.session_key:
            message = "evaluation resume changed its session key"
            raise EvaluationSuspensionUnresolvedError(message)
        response = (
            RootModel[ImplementerReply]
            if continuation.role == "implementer"
            else RootModel[JudgeReply]
        )
        reports = {
            dependency.handle: await self._read_report(dependency.handle)
            for dependency in continuation.dependencies
            if dependency.handle in continuation.settlements
        }
        for dependency in continuation.dependencies:
            if dependency.handle not in reports:
                continue
            report = reports[dependency.handle]
            if (
                report.handle_id != dependency.handle
                or (report.request.owner_scope, report.request.owner_generation)
                != (dependency.scope_id, dependency.generation)
                or _stored_outcome(report) != continuation.settlements[dependency.handle]
            ):
                message = "evaluation resume report differs from its settled dependency"
                raise EvaluationSuspensionUnresolvedError(message)
        artifacts = {
            dependency.handle: _artifact_refs(
                reports[dependency.handle],
                continuation.evidence_ids.get(dependency.handle, ()),
                dependency,
            )
            for dependency in continuation.dependencies
            if dependency.handle in reports
        }
        if request.reconcile_only:
            outcome = session.inspect(request.invocation_id)
        else:
            authorized = await self.apply(DispatchIntent(operation_id=request.operation_id))
            if not any(isinstance(item, ResumeAgentTurn) for item in authorized):
                message = "evaluation resume dispatch is fenced"
                raise EvaluationSuspensionUnresolvedError(message)
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
                            status=continuation.settlements[dependency.handle].value
                            if dependency.handle in continuation.settlements
                            else "timed_out",
                            candidate_revision=dependency.candidate_revision,
                            evaluator_revision=dependency.evaluator_digest,
                            evidence_ids=continuation.evidence_ids.get(dependency.handle, ()),
                            artifact_refs=artifacts.get(dependency.handle, ()),
                            detail=reports[dependency.handle].model_dump_json()
                            if dependency.handle in reports
                            else continuation.timed_out.model_dump_json()
                            if continuation.timed_out is not None
                            else "",
                        )
                        for dependency in continuation.dependencies
                    ),
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
            return response.model_validate_json(outcome.result.text).root
        except ValueError as error:
            await self.apply(BlockIntent(operation_id=request.operation_id))
            raise EvaluationSuspensionUnresolvedError(str(error)) from error

    async def _read_report(self, handle: str) -> StoredEvaluation:
        try:
            return StoredEvaluation.model_validate_json(
                await self.run.evaluation.submitted_report(handle)
            )
        except (RuntimeContractError, ValueError, OSError) as error:
            raise EvaluationSuspensionUnresolvedError(str(error)) from error

    async def _continue_session(
        self,
        session: AgentSession,
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
            if stage.result is not None
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
        raise EvaluationSuspensionUnresolvedError(message)
    return tuple(artifact.path for item in evidence for artifact in item.artifacts)


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


__all__ = ["EvaluationSuspension", "EvaluationSuspensionUnresolvedError"]


def _observation_state(
    observation: EvaluationSettlementObservation,
) -> Literal["pending", "running", "unknown"]:
    if isinstance(observation.result, EvaluationPending):
        return "running" if observation.result.state is EvaluationState.RUNNING else "pending"
    return "unknown"
