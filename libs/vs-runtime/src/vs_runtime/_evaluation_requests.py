"""The EVALUATION role: measurement jobs driven through durable receipts.

Every request is idempotent by its canonical identity. A sealed result is
replayed verbatim and never recomputed. A job is registered (its intent) before
any executor contact, and a restarted host inspects the executor before it
considers submitting again, so one submission identity is one executor job.

Observation and inspection are pure: they only poll. Termination is proven by
a poll that shows the job ended, never by a cancel call returning. Closing a
scope fences exactly its admission episode and lists every job it registered as
the child manifest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    CancelOwnedJob,
    CloseAttemptScope,
    CollectEvidence,
    ContractError,
    DecisionId,
    EventId,
    InspectOwnedJob,
    JobObserved,
    MeasurementFailure,
    MeasurementPlan,
    MeasurementSubmissionObserved,
    Observation,
    ObservationStatus,
    ObserveOwnedJob,
    RequestBase,
    RequestId,
    RequestObserved,
    ResourceId,
    Scope,
    SubmitMeasurement,
    TargetObservation,
)
from vs_evaluation.api import (
    ExecutorCancellationUnknownError,
    ExecutorPoll,
    ExecutorRejectedError,
    PollPhase,
)
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._evaluation_jobs import (
    JobView,
    RejectedPlanError,
    handle_for,
    job_view,
    measurement_request,
)

if TYPE_CHECKING:
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_runtime._core_requests import EvaluationRoleRequest, OwnerEvent
    from vs_runtime._receipt_store import ReceiptStore

_JOBS = "evaluation-jobs"
_SCOPES = "evaluation-scopes"
_RESULTS = "evaluation-results"
_SEQUENCES = "evaluation-sequences"
_FENCE = "evaluation-fence"


class JobRecord(BaseModel):
    """A registered job: the intent receipt written before the effect."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    handle_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    scope: Scope
    admission_id: DecisionId | None = None
    plan: MeasurementPlan


class ScopeJobs(BaseModel):
    """The jobs registered under one admission episode of one scope, and whether it closed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    scope: Scope
    admission_id: DecisionId | None = None
    handles: tuple[str, ...] = ()
    closed: bool = False


class Counter(BaseModel):
    """A durable monotone counter: request observations and the highest host epoch seen."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value: int = Field(default=0, ge=0)


class SealedResult(BaseModel):
    """The terminal result of one request, replayed verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    request_id: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    result_json: str


def _identity(request: RequestBase) -> RequestId:
    if request.request_id is None:
        raise ContractError(("request_id",), "canonical identity required")
    return request.request_id


def _scope_key(scope: Scope, admission_id: DecisionId | None) -> str:
    episode = "" if admission_id is None else admission_id.root
    return f"{scope.owner.kind}:{scope.owner.root}:{scope.generation}:{episode}"


class MeasurementRequests:
    """Translate the six evaluation requests into polls and submissions of one executor."""

    def __init__(self, jobs: PollingEvaluationExecutor, store: ReceiptStore) -> None:
        """Bind the executor that runs jobs and the receipts that make requests idempotent."""
        self._jobs = jobs
        self._store = store

    async def execute(
        self, request: EvaluationRoleRequest, context: ExecutionContext
    ) -> ExecutionResult:
        """Route one authorized request to its exact handler."""
        sealed = self._sealed(_identity(request), context)
        if sealed is not None:
            return sealed
        match request:
            case SubmitMeasurement():
                return await self._submit(request, context)
            case ObserveOwnedJob() | InspectOwnedJob():
                return await self._inspect(request, context)
            case CollectEvidence():
                return await self._collect(request, context)
            case CancelOwnedJob():
                return await self._cancel(request, context)
            case CloseAttemptScope():
                return await self._close(request, context)
            case _:
                assert_never(request)

    # sealing

    def _sealed(self, request_id: RequestId, context: ExecutionContext) -> ExecutionResult | None:
        receipt = self._store.load(_RESULTS, "result", request_id.root, SealedResult)
        if receipt is None:
            return None
        if receipt.payload_digest != context.payload_digest:
            raise ContractError(("request_id",), "same request identity with another payload")
        return ExecutionResult.model_validate_json(receipt.result_json)

    def _seal(
        self, request: RequestBase, context: ExecutionContext, result: ExecutionResult
    ) -> ExecutionResult:
        """Record the result, then return what a replay will return."""
        receipt = SealedResult(
            request_id=_identity(request).root,
            payload_digest=context.payload_digest,
            result_json=result.model_dump_json(),
        )
        self._store.record_once(_RESULTS, "result", receipt.request_id, receipt)
        return ExecutionResult.model_validate_json(receipt.result_json)

    # ledger

    def _record(self, handle_id: str) -> JobRecord | None:
        return self._store.load(_JOBS, "job", handle_id, JobRecord)

    def _owned(self, request: RequestBase, resource: ResourceId) -> JobRecord | None:
        """The registered job this request may touch, or None for foreign and unknown ones."""
        record = self._record(resource.root)
        return record if record is not None and record.scope == request.scope else None

    def _scope_jobs(self, scope: Scope, admission_id: DecisionId | None) -> ScopeJobs:
        key = _scope_key(scope, admission_id)
        return self._store.load(_SCOPES, "scope", key, ScopeJobs) or ScopeJobs(
            scope=scope, admission_id=admission_id
        )

    def _save_scope(self, index: ScopeJobs) -> None:
        self._store.replace(_SCOPES, "scope", _scope_key(index.scope, index.admission_id), index)

    def _allocate(self, record: JobRecord) -> int:
        """Reserve the next observation sequence of this job, durably.

        The job's observations carry the submission's request id, so they share one
        counter with the submission's own (possibly Unknown) observations.
        """
        return self._bump(record.request_id)

    async def _poll(self, handle_id: str) -> ExecutorPoll:
        try:
            return await self._jobs.poll(handle_id)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-C20006 [BLE001]; an inspection that cannot run proves neither progress nor termination, so it is reported as an unknown poll instead of halting recovery.
            return ExecutorPoll(
                phase=PollPhase.UNKNOWN, detail=f"poll raised {type(error).__name__}: {error}"
            )

    async def _view(self, record: JobRecord, context: ExecutionContext) -> tuple[JobView, bool]:
        """Poll the job as the next numbered observation. True when the job ended."""
        polled = await self._poll(record.handle_id)
        sequence = self._allocate(record)
        view = job_view(
            polled,
            record.plan,
            RequestId(root=record.request_id),
            record.scope,
            record.admission_id,
            record.handle_id,
            sequence,
            context.now_at,
        )
        return view, polled.phase is PollPhase.ENDED

    # observations

    def _next(self, request: RequestBase) -> int:
        """The next observation sequence of this request, durable across retries.

        An observation that is not sealed (Unknown) is followed by a retry, and the
        core only accepts an observation of a request whose sequence increased.
        """
        return self._bump(_identity(request).root)

    def _bump(self, key: str) -> int:
        prior = self._store.load(_SEQUENCES, "sequence", key, Counter) or Counter()
        self._store.replace(_SEQUENCES, "sequence", key, Counter(value=prior.value + 1))
        return prior.value + 1

    def _authority_problem(self, context: ExecutionContext) -> str | None:
        """Why this host may not run an effect now, or None. Checked before every effect.

        The lease must verify at the current time, and the host epoch must not be older
        than the newest epoch that already ran an effect.
        """
        if context.lease is None or not context.lease.verify(now_at=context.now_at):
            return "host lease is not verified"
        seen = self._store.load(_FENCE, "epoch", "host", Counter) or Counter()
        if context.fence.epoch < seen.value:
            return "host epoch is older than a host that already ran effects"
        if context.fence.epoch > seen.value:
            self._store.replace(_FENCE, "epoch", "host", Counter(value=context.fence.epoch))
        return None

    def _own(  # noqa: PLR0913  # lint-waiver: LW-C20007 [PLR0913]; each argument is an independent fact of the request's own observation.
        self,
        request: RequestBase,
        context: ExecutionContext,
        status: ObservationStatus,
        *,
        accepted: bool,
        terminal: bool,
        released: bool,
        children: tuple[ResourceId, ...] = (),
        manifest: bool = False,
        resource: ResourceId | None = None,
        diagnostic: str = "",
    ) -> Observation:
        sequence = self._next(request)
        return Observation(
            event_id=EventId(root=f"{_identity(request).root}:observation:own:{sequence}"),
            request_id=_identity(request),
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=sequence,
            observed_at=context.now_at,
            status=status,
            resource_id=resource,
            accepted=accepted,
            terminal=terminal,
            released=released,
            children=children,
            children_complete=manifest,
            diagnostic=diagnostic,
        )

    def _rejected(
        self,
        request: RequestBase,
        context: ExecutionContext,
        reason: str,
        *,
        failure: MeasurementFailure | None = None,
        seal: bool = False,
    ) -> ExecutionResult:
        """Definite non-acceptance: nothing is owned, so nothing is left to release."""
        observed = RequestObserved(
            observation=self._own(
                request,
                context,
                ObservationStatus.REJECTED,
                accepted=False,
                terminal=True,
                released=True,
                manifest=True,
                diagnostic=reason,
            ),
            measurement_failure=failure,
        )
        result = ExecutionResult(
            observation=observed,
            owner_events=()
            if failure is None
            else (
                MeasurementSubmissionObserved(observation=observed.observation, failure=failure),
            ),
        )
        return self._seal(request, context, result) if seal else result

    def _unknown(
        self, request: RequestBase, context: ExecutionContext, reason: str
    ) -> ExecutionResult:
        """An outcome that cannot be proven yet. It claims nothing and is never sealed."""
        return ExecutionResult(
            observation=RequestObserved(
                observation=self._own(
                    request,
                    context,
                    ObservationStatus.UNKNOWN,
                    accepted=False,
                    terminal=False,
                    released=False,
                    diagnostic=reason,
                )
            )
        )

    @staticmethod
    def _events(view: JobView) -> tuple[OwnerEvent, ...]:
        resource = view.observation.resource_id
        assert resource is not None  # noqa: S101  # every job observation names the job
        failed: tuple[OwnerEvent, ...] = (
            ()
            if view.failure is None
            else (
                MeasurementSubmissionObserved(observation=view.observation, failure=view.failure),
            )
        )
        return (
            *failed,
            JobObserved(
                resource_id=resource,
                observation=view.observation,
                progress=view.progress,
                evidence=view.evidence,
                evaluation_result=view.facts,
            ),
        )

    # submit

    async def _submit(
        self, request: SubmitMeasurement, context: ExecutionContext
    ) -> ExecutionResult:
        request_id = _identity(request)
        handle = handle_for(request_id)
        index = self._scope_jobs(request.scope, request.admission_id)
        if index.closed:
            return self._rejected(request, context, "scope episode is closed", seal=True)
        try:
            eval_request = measurement_request(request.plan, request.scope, handle)
        except RejectedPlanError as error:
            return self._rejected(
                request, context, str(error), failure=MeasurementFailure.WORKLOAD, seal=True
            )
        record = self._record(handle)
        if record is None:
            record = JobRecord(
                handle_id=handle,
                request_id=request_id.root,
                payload_digest=context.payload_digest,
                scope=request.scope,
                admission_id=request.admission_id,
                plan=request.plan,
            )
            if handle not in index.handles:
                self._save_scope(index.model_copy(update={"handles": (*index.handles, handle)}))
            self._store.record_once(_JOBS, "job", handle, record)
        elif record.payload_digest != context.payload_digest:
            raise ContractError(("request_id",), "same request identity with another payload")
        if (await self._poll(handle)).phase is PollPhase.UNSUBMITTED:
            problem = self._authority_problem(context)
            if problem is not None:
                return self._unknown(request, context, problem)
            try:
                await self._jobs.submit(eval_request, handle_id=handle)
            except ExecutorRejectedError as error:
                return self._rejected(request, context, str(error), seal=True)
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-C20008 [BLE001]; a failed submission may or may not have reached the executor, so it is reported as an unproven outcome that the next request inspects.
                return self._unknown(
                    request, context, f"submission raised {type(error).__name__}: {error}"
                )
        view, _ = await self._view(self._record(handle) or record, context)
        observed = RequestObserved(
            observation=view.observation,
            progress=view.progress,
            measurement_failure=view.failure,
            evaluation_result=view.facts,
            evidence=view.evidence,
        )
        result = ExecutionResult(
            observation=observed,
            owner_events=(
                MeasurementSubmissionObserved(observation=view.observation, failure=view.failure),
            ),
        )
        return self._seal(request, context, result)

    # observe, inspect, collect

    async def _inspect(
        self, request: ObserveOwnedJob | InspectOwnedJob, context: ExecutionContext
    ) -> ExecutionResult:
        record = self._owned(request, request.resource_id)
        if record is None:
            return self._rejected(request, context, "job is not owned by this scope")
        view, _ = await self._view(record, context)
        own = self._own(
            request,
            context,
            ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            manifest=True,
        )
        target = (
            TargetObservation(
                observation=view.observation,
                progress=view.progress,
                evidence=view.evidence,
                evaluation_result=view.facts,
                measurement_failure=view.failure,
            )
            if isinstance(request, InspectOwnedJob)
            else None
        )
        result = ExecutionResult(
            observation=RequestObserved(observation=own, target=target),
            owner_events=self._events(view),
        )
        return self._seal(request, context, result)

    async def _collect(
        self, request: CollectEvidence, context: ExecutionContext
    ) -> ExecutionResult:
        record = self._owned(request, request.resource_id)
        if record is None:
            return self._rejected(request, context, "job is not owned by this scope")
        view, ended = await self._view(record, context)
        if not ended:
            return self._unknown(request, context, "job has not ended, so nothing is collectable")
        own = self._own(
            request,
            context,
            ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            manifest=True,
        )
        result = ExecutionResult(
            observation=RequestObserved(observation=own), owner_events=self._events(view)
        )
        return self._seal(request, context, result)

    # cancel

    async def _cancel(self, request: CancelOwnedJob, context: ExecutionContext) -> ExecutionResult:
        record = self._owned(request, request.resource_id)
        if record is None or record.admission_id != request.admission_id:
            return self._rejected(request, context, "job is not owned by this scope episode")
        problem = self._authority_problem(context)
        if problem is not None:
            return self._unknown(request, context, problem)
        try:
            await self._jobs.cancel(record.handle_id)
        except ExecutorCancellationUnknownError as error:
            return self._unknown(request, context, f"cancellation outcome unknown: {error}")
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-C20009 [BLE001]; a failed cancel leaves the job owned and running, so it is reported unproven and retried rather than halting the run.
            return self._unknown(request, context, f"cancel raised {type(error).__name__}: {error}")
        view, ended = await self._view(record, context)
        own = self._own(
            request,
            context,
            ObservationStatus.CANCELLED,
            accepted=True,
            terminal=True,
            released=ended,
            resource=request.resource_id,
            diagnostic="" if ended else "cancellation requested, termination not yet observed",
        )
        result = ExecutionResult(
            observation=RequestObserved(observation=own), owner_events=self._events(view)
        )
        return self._seal(request, context, result)

    # close

    async def _close(
        self, request: CloseAttemptScope, context: ExecutionContext
    ) -> ExecutionResult:
        problem = self._authority_problem(context)
        if problem is not None:
            return self._unknown(request, context, problem)
        index = self._scope_jobs(request.scope, request.admission_id)
        if not index.closed:
            # Fence first: a submission that arrives after this point is refused.
            index = index.model_copy(update={"closed": True})
            self._save_scope(index)
        ended = []
        for handle in index.handles:
            if self._authority_problem(context) is not None:
                ended.append(False)
                continue
            try:
                await self._jobs.cancel(handle)
            except ExecutorCancellationUnknownError:
                pass
            except Exception:  # noqa: BLE001  # lint-waiver: LW-C20010 [BLE001]; one job that cannot be cancelled must not hide the others from the manifest, and it stays unreleased.
                ended.append(False)
                continue
            ended.append((await self._poll(handle)).phase is PollPhase.ENDED)
        released = all(ended)
        own = self._own(
            request,
            context,
            ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=released,
            children=tuple(ResourceId(root=handle) for handle in index.handles),
            manifest=True,
            diagnostic="" if released else "some jobs have not been observed to end",
        )
        result = ExecutionResult(observation=RequestObserved(observation=own))
        return self._seal(request, context, result) if released else result
