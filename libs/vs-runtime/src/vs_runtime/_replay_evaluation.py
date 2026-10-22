"""Typed keyed submission, observation and release through the evaluation owner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from vs_core.api import (
    AttemptId,
    AttemptRef,
    CancelOwnedJob,
    CloseAttemptScope,
    CollectEvidence,
    DecisionId,
    EvidenceKind,
    JobObserved,
    MeasurementPlan,
    MeasurementStage,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
    RevisionRef,
    Scope,
    SubmitMeasurement,
)
from vs_proto.api import core_agent_facts, core_common, core_leaves, core_vocabulary, validate
from vs_proto.api import replay_evaluation as pb
from vs_runtime._evaluation_jobs import handle_for
from vs_runtime._evaluation_requests import MeasurementRequests
from vs_runtime._replay_evidence import encode_evidence
from vs_runtime._replay_operations import ReplayOperationError
from vs_runtime._replay_owner import ReceiptReplayExecutor, ReplayOwnerCodec
from vs_runtime._replay_runtime import ReplayAbsent, ReplayPending, ReplayResult, ReplayUnknown
from vs_runtime.api.core_values import from_proto, to_proto

if TYPE_CHECKING:
    from vs_core.api import ArtifactRef, BaseRef, EvidenceId, MeasurementFailure
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_project.api import StoreFence
    from vs_proto.api import Span
    from vs_runtime._core_requests import EvaluationRoleRequest, ExecutionContext, ExecutionResult
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._replay_operations import ReplayOperationHost
    from vs_runtime._run_control import RunControlChannel


def replay_measurement_plan(plan: MeasurementPlan) -> pb.ReplayMeasurementPlan:
    """Encode a resolved plan, including a mechanically derived DAG validation witness."""
    remaining = {stage.stage_id: stage for stage in plan.stages}
    ranks: dict[str, int] = {}
    while remaining:
        ready = [stage for stage in remaining.values() if set(stage.depends_on) <= ranks.keys()]
        if not ready:
            problem = "measurement plan contains a dependency cycle"
            raise ReplayOperationError(problem)
        for stage in ready:
            ranks[stage.stage_id] = 1 + max((ranks[name] for name in stage.depends_on), default=-1)
            del remaining[stage.stage_id]
    if not isinstance(plan.candidate, RevisionRef):
        problem = "replay measurement requires a resolved retained revision"
        raise ReplayOperationError(problem)
    result = pb.ReplayMeasurementPlan(
        purpose=core_agent_facts.EvidencePurpose[plan.purpose.upper().replace("-", "_")],
        candidate=cast("core_leaves.RevisionRef", to_proto(plan.candidate)),
        evaluator_digest=plan.evaluator_digest,
        workload_digest=plan.workload_digest,
        environment_digest=plan.environment_digest,
        stages=[
            pb.ReplayMeasurementStage(
                id=stage.stage_id,
                depends_on=list(stage.depends_on),
                required_outputs=list(stage.required_outputs),
                execution_seconds=stage.execution_budget,
                dependency_rank=ranks[stage.stage_id],
            )
            for stage in plan.stages
        ],
        order=pb.ReplayMeasurementOrder[plan.policy.upper()],
        recipe=cast("core_common.ArtifactRef", to_proto(plan.recipe)),
        submitted_at=plan.submitted_at,
        queue_allowance=plan.queue_allowance,
        reusable_evidence=[
            cast("core_common.EvidenceId", to_proto(item)) for item in plan.reusable_evidence
        ],
        dedicated_resources=plan.dedicated_measurement_resources,
        measured_input=plan.measured_input,
        accuracy_stage=plan.accuracy_stage or "",
        accuracy_gated=[core_vocabulary.EvidenceKind[item.name] for item in plan.accuracy_gated],
    )
    validate(result)
    return result


def _plan(native: pb.ReplayMeasurementPlan | None) -> MeasurementPlan:
    if native is None or native.candidate is None or native.recipe is None:
        problem = "replay measurement requires its resolved plan"
        raise ReplayOperationError(problem)
    return MeasurementPlan(
        purpose=cast(
            "Literal['baseline', 'local-validation', 'official', 'profile']",
            native.purpose.name.lower().replace("_", "-"),
        ),
        candidate=cast("RevisionRef", from_proto(native.candidate)),
        evaluator_digest=native.evaluator_digest,
        workload_digest=native.workload_digest,
        environment_digest=native.environment_digest,
        stages=tuple(
            MeasurementStage(
                stage_id=stage.id,
                depends_on=tuple(stage.depends_on),
                required_outputs=tuple(stage.required_outputs),
                execution_budget=stage.execution_seconds,
            )
            for stage in native.stages
        ),
        policy=cast("Literal['ordered', 'parallel']", native.order.name.lower()),
        recipe=cast("ArtifactRef", from_proto(native.recipe)),
        submitted_at=native.submitted_at,
        queue_allowance=native.queue_allowance,
        reusable_evidence=tuple(
            cast("EvidenceId", from_proto(item)) for item in native.reusable_evidence
        ),
        dedicated_measurement_resources=native.dedicated_resources,
        measured_input=native.measured_input or None,
        accuracy_stage=native.accuracy_stage or None,
        accuracy_gated=tuple(EvidenceKind(item.name.lower()) for item in native.accuracy_gated),
    )


def _failure(failure: MeasurementFailure | None) -> pb.ReplayMeasurementFailure:
    return (
        pb.ReplayMeasurementFailure.UNSPECIFIED
        if failure is None
        else pb.ReplayMeasurementFailure[failure.name]
    )


class EvaluationReplayExecutor:
    """The existing measurement owner retains submission, evidence and cancellation authority.

    A submission result may name a live job. That typed fact never means the
    measurement completed: subsequent observation, evidence collection and release
    are separate semantic effects. Unknown acceptance remains unfinished.
    """

    def __init__(
        self,
        jobs: PollingEvaluationExecutor,
        receipts: ReceiptStore,
        host: ReplayOperationHost,
        *,
        within_seconds: float,
        control: RunControlChannel | None = None,
    ) -> None:
        if not 0 < within_seconds < float("inf"):
            problem = "evaluation replay within_seconds must be finite and positive"
            raise ReplayOperationError(problem)
        self._within_seconds = within_seconds
        owner = MeasurementRequests(jobs, receipts, control=control)

        async def execute(
            request: EvaluationRoleRequest, context: ExecutionContext
        ) -> ExecutionResult:
            if isinstance(request, ObserveOwnedJob):
                await jobs.wait_for_change(
                    request.resource_id.root, min(1, max(0, context.until.at - host.clock.now()))
                )
            result = await owner.execute(request, context)
            seen = result.observation.observation
            awaiting_acceptance = (
                isinstance(request, SubmitMeasurement) and seen.status is ObservationStatus.UNKNOWN
            )
            awaiting_release = isinstance(request, CancelOwnedJob) and not seen.released
            if awaiting_acceptance or awaiting_release:
                handles = (
                    (handle_for(request.request_id),)
                    if isinstance(request, SubmitMeasurement) and request.request_id is not None
                    else tuple(item.root for item in seen.children)
                )
                if isinstance(request, CancelOwnedJob):
                    handles = (request.resource_id.root,)
                remaining = max(0.0, context.until.at - host.clock.now())
                for handle in handles:
                    await jobs.wait_for_change(handle, remaining / max(1, len(handles)))
                result = await owner.execute(
                    request, context.model_copy(update={"now_at": host.clock.now()})
                )
            return result

        self._executor = ReceiptReplayExecutor(
            execute,
            receipts,
            host,
            ReplayOwnerCodec(self._request, self._encode, self._within, self._dispatch_check),
        )

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult:
        return await self._executor.execute(span, fence)

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        return await self._executor.inspect(span, fence)

    @staticmethod
    def _native(span: Span) -> pb.ReplayEvaluationRequest:
        validate(span)
        operation = span.replay
        if operation is None or operation.policy is None:
            problem = "evaluation replay requires immutable metadata"
            raise ReplayOperationError(problem)
        policy = operation.policy
        if (
            policy.request is None
            or policy.request.type_url != pb.ReplayEvaluationRequest.desc().type_name
        ):
            problem = "evaluation replay request type differs"
            raise ReplayOperationError(problem)
        if policy.reply_type != pb.ReplayEvaluationResult.desc().type_name:
            problem = "evaluation replay reply type differs"
            raise ReplayOperationError(problem)
        result = pb.ReplayEvaluationRequest.from_binary(policy.request.value)
        validate(result)
        if result.to_binary() != result.to_binary(write_unknown_fields=False):
            problem = "evaluation replay request has unknown fields"
            raise ReplayOperationError(problem)
        return result

    @classmethod
    def _within(cls, span: Span) -> float:
        return cls._native(span).within_seconds

    def _dispatch_check(self, span: Span) -> None:
        native = self._native(span)
        if native.within_seconds > self._within_seconds:
            problem = "evaluation request exceeds its declared replay bound"
            raise ReplayOperationError(problem)
        if (
            native.action is not None
            and native.action.field == "submit"
            and _plan(native.action.value.plan).time_bound > native.within_seconds
        ):
            problem = "evaluation stages and queue allowance exceed the declared replay bound"
            raise ReplayOperationError(problem)

    @classmethod
    def _request(cls, span: Span) -> EvaluationRoleRequest:
        native = cls._native(span)
        if native.scope is None:
            problem = "evaluation replay requires a scope"
            raise ReplayOperationError(problem)
        scope = cast("Scope", from_proto(native.scope))
        identity = RequestId(root=span.id)
        admission = DecisionId(root=native.admission) if native.admission else None
        match native.action:
            case action if action is not None and action.field == "submit":
                return SubmitMeasurement(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    plan=_plan(action.value.plan),
                    reuses=None
                    if action.value.reuses is None
                    else cast("RequestId", from_proto(action.value.reuses)),
                    base=None
                    if action.value.base is None
                    else cast("BaseRef", from_proto(action.value.base)),
                )
            case action if action is not None and action.field == "observe":
                return ObserveOwnedJob(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    resource_id=ResourceId(root=action.value.resource),
                )
            case action if action is not None and action.field == "collect":
                return CollectEvidence(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    resource_id=ResourceId(root=action.value.resource),
                )
            case action if action is not None and action.field == "cancel":
                return CancelOwnedJob(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    resource_id=ResourceId(root=action.value.resource),
                    for_park=action.value.for_park,
                    immediate=action.value.immediate,
                )
            case action if action is not None and action.field == "close":
                if not isinstance(scope.owner, AttemptId):
                    problem = "evaluation scope closure requires attempt ownership"
                    raise ReplayOperationError(problem)
                return CloseAttemptScope(
                    request_id=identity,
                    scope=scope,
                    admission_id=admission,
                    attempt=AttemptRef(attempt_id=scope.owner, generation=scope.generation),
                )
            case _:
                problem = "unsupported replay evaluation action"
                raise ReplayOperationError(problem)

    @staticmethod
    async def _encode(
        request: EvaluationRoleRequest, result: ExecutionResult
    ) -> ReplayResult | ReplayUnknown:
        observed = result.observation
        seen = observed.observation
        if seen.status is ObservationStatus.UNKNOWN or (not seen.accepted and not seen.terminal):
            return ReplayUnknown(seen.diagnostic)
        if isinstance(request, CancelOwnedJob) and not seen.released:
            return ReplayUnknown(seen.diagnostic)
        jobs = [event for event in result.owner_events if isinstance(event, JobObserved)]
        if len(jobs) > 1:
            problem = "evaluation owner returned multiple jobs for one request"
            raise ReplayOperationError(problem)
        job = None
        if jobs:
            current = jobs[0]
            job = pb.ReplayEvaluationJob(
                resource=current.resource_id.root,
                status=core_vocabulary.ObservationStatus[current.observation.status.name],
                terminal=current.observation.terminal,
                released=current.observation.released,
                observation_sequence=current.observation.sequence,
                evidence=[encode_evidence(item) for item in current.evidence],
                failure=_failure(current.failure),
                detail=current.observation.diagnostic,
            )
        native = pb.ReplayEvaluationResult(
            status=core_vocabulary.ObservationStatus[seen.status.name],
            terminal=seen.terminal,
            released=seen.released,
            resource=None if seen.resource_id is None else seen.resource_id.root,
            children=[item.root for item in seen.children],
            detail=seen.diagnostic,
            job=job,
            failure=_failure(observed.measurement_failure),
        )
        validate(native)
        return ReplayResult(native)
