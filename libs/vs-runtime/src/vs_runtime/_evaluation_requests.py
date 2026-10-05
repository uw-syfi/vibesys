"""The EVALUATION role: measurement jobs driven through durable receipts.

Every request runs through ``ReceiptStore.run_once``, so it is idempotent by its
canonical identity: a begun marker precedes the effect and a sealed result is
replayed verbatim and never recomputed. Only a definitive result is sealed (see
``settle``); Unknown, an unproven acceptance and an unreleased cancellation are
returned unsealed so a retry re-observes them. A job is registered (its intent) before
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
    EvidenceId,
    EvidenceKey,
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
    EvaluationRequest,
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    ExecutorPoll,
    ExecutorRejectedError,
    PollPhase,
    TrustedEvidence,
)
from vs_runtime._core_requests import ExecutionContext, ExecutionResult, settle
from vs_runtime._evaluation_jobs import (
    JobView,
    RejectedPlanError,
    handle_for,
    job_view,
    measurement_request,
)
from vs_runtime._evidence_ledger import ReceiptEvidenceLedger
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationSubject,
)
from vs_runtime._receipt_store import Conflict, Declined, Performed, Replayed, owner_key

if TYPE_CHECKING:
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_runtime._core_requests import EvaluationRoleRequest, OwnerEvent
    from vs_runtime._receipt_store import ReceiptStore, Settled, Transient

_JOBS = "evaluation-jobs"
_SCOPES = "evaluation-scopes"


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
        self._observations = ObservationFactory(store)

    async def execute(
        self, request: EvaluationRoleRequest, context: ExecutionContext
    ) -> ExecutionResult:
        """Run one authorized request at most once under its canonical identity."""
        request_id = _identity(request)

        async def perform(
            *, resumed: bool
        ) -> Settled[ExecutionResult] | Transient[ExecutionResult]:
            return settle(await self._perform(request, context, resumed=resumed))

        execution = await self._store.run_once(
            request_id.root,
            owner=owner_key(request),
            context=context,
            result_type=ExecutionResult,
            perform=perform,
        )
        match execution:
            case Replayed(result) | Performed(result):
                return result
            case Conflict():
                return self._rejected(
                    request, context, "same request identity with another payload"
                )
            case Declined(reason):
                return self._unknown(request, context, reason)
            case _:
                assert_never(execution)

    async def _perform(
        self, request: EvaluationRoleRequest, context: ExecutionContext, *, resumed: bool
    ) -> ExecutionResult:
        """The effect of one request. ``resumed`` is moot: every step first polls the job."""
        del resumed  # the job record and a poll tell what an earlier host already did
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

    # ledger

    def _record(self, handle_id: str) -> JobRecord | None:
        return self._store.load(_JOBS, "job", handle_id, JobRecord)

    def _owned(self, request: RequestBase, resource: ResourceId) -> JobRecord | None:
        """The registered job this request may touch, or None for foreign and unknown ones."""
        record = self._record(resource.root)
        return record if record is not None and record.scope == request.scope else None

    def _register(self, scope: Scope, admission_id: DecisionId | None, handle: str) -> bool:
        """Add *handle* to its scope's index, atomically with the closed check. True when closed."""

        def decide(stored: ScopeJobs | None) -> tuple[ScopeJobs | None, bool]:
            index = stored or ScopeJobs(scope=scope, admission_id=admission_id)
            if index.closed:
                return None, True
            if handle in index.handles:
                return None, False
            return index.model_copy(update={"handles": (*index.handles, handle)}), False

        return self._store.modify(
            _SCOPES, "scope", _scope_key(scope, admission_id), ScopeJobs, decide
        )

    def _fence(self, scope: Scope, admission_id: DecisionId | None) -> ScopeJobs:
        """Close the scope episode to new registrations and return the jobs it holds."""

        def decide(stored: ScopeJobs | None) -> tuple[ScopeJobs | None, ScopeJobs]:
            index = stored or ScopeJobs(scope=scope, admission_id=admission_id)
            if stored is not None and stored.closed:
                return None, stored
            closed = index.model_copy(update={"closed": True})
            return closed, closed

        return self._store.modify(
            _SCOPES, "scope", _scope_key(scope, admission_id), ScopeJobs, decide
        )

    async def _poll(self, handle_id: str) -> ExecutorPoll:
        try:
            return await self._jobs.poll(handle_id)
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-940006 [BLE001]; an inspection that cannot run proves neither progress nor termination, so it is reported as an unknown poll instead of halting recovery.
            return ExecutorPoll(
                phase=PollPhase.UNKNOWN, detail=f"poll raised {type(error).__name__}: {error}"
            )

    def _with_ledgered_evidence(
        self, polled: ExecutorPoll, record: JobRecord, subject: ObservationSubject
    ) -> ExecutorPoll:
        """Record each stage reading once; a later poll that reads differently gets the stored one.

        Evidence never changes under a reader, so a replay returns the stored entry
        instead of failing inside the pure translation.
        """
        terminal = polled.terminal
        if terminal is None:
            return polled
        ledger = ReceiptEvidenceLedger(self._store)
        known = {stage.stage_id for stage in record.plan.stages}
        steps = []
        for step in terminal.stage_results:
            if step.result is None or step.name not in known:
                steps.append(step)
                continue
            item = TrustedEvidence.model_validate(step.result)
            key = EvidenceKey(
                source_request=subject.request_id, evidence_id=EvidenceId(root=item.evidence_id)
            )
            stored = ledger.lookup(key)
            if stored is None:
                ledger.record(subject.request_id, item, record.plan.purpose)
                steps.append(step)
            else:
                steps.append(
                    step.model_copy(update={"result": stored.evidence.model_dump(mode="json")})
                )
        return polled.model_copy(
            update={"terminal": terminal.model_copy(update={"stage_results": tuple(steps)})}
        )

    async def _view(self, record: JobRecord, context: ExecutionContext) -> tuple[JobView, bool]:
        """Poll the job as the next numbered observation. True when the job ended."""
        subject = ObservationSubject(
            RequestId(root=record.request_id), record.scope, record.admission_id
        )
        polled = self._with_ledgered_evidence(await self._poll(record.handle_id), record, subject)
        # The job's observations carry the submission's request id, so they share one
        # sequence with the submission's own (possibly Unknown) observations.
        view = job_view(
            polled,
            record.plan,
            subject,
            record.handle_id,
            context.now_at,
            lambda facts: self._observations.observe(
                subject, facts, observed_at=context.now_at, fresh=True
            ),
        )
        return view, polled.phase is PollPhase.ENDED

    # observations

    def _authority_problem(self, request: RequestBase, context: ExecutionContext) -> str | None:
        """Why this host may not run an effect now, or None. Checked before every effect."""
        return self._store.authorize(owner_key(request), context)

    def _own(  # noqa: PLR0913  # lint-waiver: LW-940007 [PLR0913]; each argument is an independent fact of the request's own observation.
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
        return self._observations.observe(
            ObservationSubject.of(request),
            ObservationFacts(
                status=status,
                terminal=terminal,
                accepted=accepted,
                released=released,
                children=children,
                children_complete=manifest,
                resource_id=resource,
                diagnostic=diagnostic,
            ),
            observed_at=context.now_at,
        )

    def _rejected(
        self,
        request: RequestBase,
        context: ExecutionContext,
        reason: str,
        *,
        failure: MeasurementFailure | None = None,
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
        return ExecutionResult(
            observation=observed,
            owner_events=()
            if failure is None
            else (
                MeasurementSubmissionObserved(observation=observed.observation, failure=failure),
            ),
        )

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
    def _first_job_event(view: JobView) -> tuple[OwnerEvent, ...]:
        """The job's first observation, once the executor owns a job to observe."""
        observed = view.observation
        if not observed.accepted or observed.resource_id is None:
            return ()
        return (
            JobObserved(
                resource_id=observed.resource_id,
                observation=observed,
                progress=view.progress,
                evidence=view.evidence,
                evaluation_result=view.facts,
            ),
        )

    @staticmethod
    def _events(view: JobView) -> tuple[OwnerEvent, ...]:
        resource = view.observation.resource_id
        if resource is None:
            raise ContractError(("resource_id",), "every job observation names the job")
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
        try:
            eval_request = measurement_request(request.plan, request.scope, handle)
        except RejectedPlanError as error:
            return self._rejected(request, context, str(error), failure=MeasurementFailure.WORKLOAD)
        closed = self._register(request.scope, request.admission_id, handle)
        record = self._record(handle)
        if record is None:
            if closed:
                return self._rejected(request, context, "scope episode is closed")
            record = JobRecord(
                handle_id=handle,
                request_id=request_id.root,
                payload_digest=context.payload_digest,
                scope=request.scope,
                admission_id=request.admission_id,
                plan=request.plan,
            )
            self._store.record_once(_JOBS, "job", handle, record)
        refused = await self._launch(request, context, eval_request, handle, closed=closed)
        if refused is not None:
            return refused
        view, _ = await self._view(record, context)
        observed = RequestObserved(
            observation=view.observation,
            progress=view.progress,
            measurement_failure=view.failure,
            evaluation_result=view.facts,
            evidence=view.evidence,
        )
        return ExecutionResult(
            observation=observed,
            # The submission's own view is the job's first observation. Core polls a
            # live job again after each one, so delivering it starts the observe cycle,
            # and a job that already ended is not lost.
            owner_events=(
                MeasurementSubmissionObserved(observation=view.observation, failure=view.failure),
                *self._first_job_event(view),
            ),
        )

    async def _launch(
        self,
        request: SubmitMeasurement,
        context: ExecutionContext,
        eval_request: EvaluationRequest,
        handle: str,
        *,
        closed: bool,
    ) -> ExecutionResult | None:
        """Submit the job if the executor holds none. A result means: stop, nothing was launched."""
        if (await self._poll(handle)).phase is PollPhase.UNSUBMITTED:
            if closed:
                # The scope is fenced and the executor holds nothing, so the job never will exist.
                return self._rejected(request, context, "scope episode is closed")
            problem = self._authority_problem(request, context)
            if problem is not None:
                return self._unknown(request, context, problem)
            try:
                await self._jobs.submit(eval_request, handle_id=handle)
            except ExecutorRejectedError as error:
                return self._rejected(request, context, str(error))
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-940008 [BLE001]; a failed submission may or may not have reached the executor, so it is reported as an unproven outcome that the next request inspects.
                return self._unknown(
                    request, context, f"submission raised {type(error).__name__}: {error}"
                )
        return None

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
        return ExecutionResult(
            observation=RequestObserved(observation=own, target=target),
            owner_events=self._events(view),
        )

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
        return ExecutionResult(
            observation=RequestObserved(observation=own), owner_events=self._events(view)
        )

    # cancel

    async def _cancel(self, request: CancelOwnedJob, context: ExecutionContext) -> ExecutionResult:
        record = self._owned(request, request.resource_id)
        if record is None or record.admission_id != request.admission_id:
            return self._rejected(request, context, "job is not owned by this scope episode")
        problem = self._authority_problem(request, context)
        if problem is not None:
            return self._unknown(request, context, problem)
        try:
            await self._jobs.cancel(record.handle_id)
        except ExecutorCancellationUnconfirmedError:
            # The job is known and cancellation was requested; the view below
            # reports it unreleased until its end is observed.
            pass
        except ExecutorCancellationUnknownError as error:
            return self._unknown(request, context, f"cancellation outcome unknown: {error}")
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-940009 [BLE001]; a failed cancel leaves the job owned and running, so it is reported unproven and retried rather than halting the run.
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
        return ExecutionResult(
            observation=RequestObserved(observation=own), owner_events=self._events(view)
        )

    # close

    async def _close(
        self, request: CloseAttemptScope, context: ExecutionContext
    ) -> ExecutionResult:
        problem = self._authority_problem(request, context)
        if problem is not None:
            return self._unknown(request, context, problem)
        # Fence first: a submission that arrives after this point is refused.
        index = self._fence(request.scope, request.admission_id)
        ended = []
        for handle in index.handles:
            if self._authority_problem(request, context) is not None:
                ended.append(False)
                continue
            try:
                await self._jobs.cancel(handle)
            except (ExecutorCancellationUnknownError, ExecutorCancellationUnconfirmedError):
                pass
            except Exception:  # noqa: BLE001  # lint-waiver: LW-940010 [BLE001]; one job that cannot be cancelled must not hide the others from the manifest, and it stays unreleased.
                ended.append(False)
                continue
            # The scope is fenced closed above, so a handle whose submission never
            # reached the executor can never be submitted: it is as good as ended.
            ended.append(
                (await self._poll(handle)).phase in (PollPhase.ENDED, PollPhase.UNSUBMITTED)
            )
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
        return ExecutionResult(observation=RequestObserved(observation=own))
