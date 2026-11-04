"""The EVALUATION role: measurement jobs driven through a durable job registry.

Every request is idempotent by its canonical identity through the effect, not through a receipt
of its result: a job is registered (its intent) before any executor contact, and a repeat of a
request inspects the executor before it considers submitting again, so one submission identity is
one executor job. A request's observation is rebuilt from the executor's state each time, so a
retry re-observes Unknown, an unproven acceptance and an unreleased cancellation.

The step loop orders the requests of a scope: a close or a cancel of a job runs only after the
job's submission has a result or was cancelled, so neither answers released while a submission
may still reach the executor.

A submission that reuses an earlier run (``SubmitMeasurement.reuses``, decided by core) launches
nothing: its job reads the earlier run's ended job, and its records are that run's records,
ledgered under the reusing submission and marked with the run they came from. If the earlier
run's job can no longer be read as ended, the submission is refused as an infrastructure
failure, so core measures afresh in the open. Cancelling or closing a reusing job never
touches the earlier run's job.

Observation and inspection are pure: they only poll. Termination is proven by
a poll that shows the job ended, never by a cancel call returning. Closing a
scope fences exactly its admission episode and lists every job it registered as
the child manifest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from vs_core.api import (
    CancelOwnedJob,
    CloseAttemptScope,
    CollectEvidence,
    ContractError,
    DecisionId,
    InspectOwnedJob,
    JobObserved,
    MeasurementFailure,
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
    EvaluationState,
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorPoll,
    ExecutorRejectedError,
    PollPhase,
)
from vs_runtime._core_identity import core_named
from vs_runtime._core_requests import ExecutionContext, ExecutionResult
from vs_runtime._evaluation_jobs import (
    JobView,
    RejectedPlanError,
    handle_for,
    job_view,
    measurement_request,
)
from vs_runtime._evidence_ledger import ReceiptEvidenceLedger, ledgered_poll
from vs_runtime._job_registry import JobRecord, JobRegistry
from vs_runtime._measurement_progress import (
    IgnoreMeasurement,
    MeasurementDecisions,
    MeasurementObserver,
    MeasurementProgress,
)
from vs_runtime._observation_factory import (
    ObservationFactory,
    ObservationFacts,
    ObservationSubject,
)

if TYPE_CHECKING:
    from vs_core.api.recovery import OwnerEvent
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_runtime._core_requests import EvaluationRoleRequest
    from vs_runtime._keyed_documents import KeyedDocuments
    from vs_runtime._run_control import RunControlChannel

_UNREADABLE_RUN = "the run this measurement reuses can no longer be read as ended"


def _identity(request: RequestBase) -> RequestId:
    if request.request_id is None:
        raise ContractError(("request_id",), "canonical identity required")
    return request.request_id


def _close_diagnostic(*, released: bool) -> str:
    return "" if released else "some jobs have not been observed to end"


class MeasurementRequests:
    """Translate the six evaluation requests into polls and submissions of one executor."""

    def __init__(
        self,
        jobs: PollingEvaluationExecutor,
        store: KeyedDocuments,
        observer: MeasurementObserver | None = None,
        decisions: MeasurementDecisions | None = None,
        *,
        control: RunControlChannel | None = None,
    ) -> None:
        """Bind the executor that runs jobs and the receipts that make requests idempotent.

        *observer* is told when each stage of a job starts and ends; it never changes a result.
        *decisions* learns which decision each submitted handle answers, for stage output.
        """
        self._control = control
        self._decisions = MeasurementDecisions() if decisions is None else decisions
        self._jobs = jobs
        self._store = store
        self._progress = MeasurementProgress(observer or IgnoreMeasurement())
        self._observations = ObservationFactory(store)
        self._registry = JobRegistry(store)

    async def execute(
        self, request: EvaluationRoleRequest, context: ExecutionContext
    ) -> ExecutionResult:
        """Run one request. A repeat (``context.repeat``) finds its job by polling it first."""
        _identity(request)
        return await self._perform(request, context, repeat=context.repeat)

    async def _perform(
        self, request: EvaluationRoleRequest, context: ExecutionContext, *, repeat: bool
    ) -> ExecutionResult:
        """The effect of one request. ``repeat`` is moot: every step first polls the job."""
        del repeat  # the job record and a poll tell what an earlier attempt already did
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
        return self._registry.record(handle_id)

    def _executor_handle(self, record: JobRecord) -> str:
        """Resolve reuse through the source's durable physical identity, including relaunches."""
        seen: set[str] = set()
        while record.reuses is not None:
            source = handle_for(record.reuses)
            if source in seen:
                raise ContractError(("evaluation", "reuses"), "cyclic measurement reuse")
            seen.add(source)
            earlier = self._record(source)
            if earlier is None:
                raise ContractError(("evaluation", "reuses"), "missing measurement reuse source")
            record = earlier
        return record.executor_handle

    def _owned(self, request: RequestBase, resource: ResourceId) -> JobRecord | None:
        """The registered job this request may touch, or None for foreign and unknown ones."""
        record = self._record(resource.root)
        return record if record is not None and record.scope == request.scope else None

    def _forget(self, scope: Scope, admission_id: DecisionId | None, handle: str) -> None:
        """Drop *handle* from its scope's index: its submission was refused, so it never was a job.

        A close lists every indexed handle as a resource core may own, and core would look
        for one it never heard of. The loop closes a scope only after every other step of it
        has a result, so the close reads the index after this.
        """
        with self._store.exclusive():
            self._registry.unindex(scope, admission_id, handle)

    def _handles(self, scope: Scope, admission_id: DecisionId | None) -> tuple[str, ...]:
        return self._registry.handles(scope, admission_id)

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
        """Record each stage reading once; a later poll that reads differently gets the stored one."""
        return ledgered_poll(
            ReceiptEvidenceLedger(self._store),
            polled,
            source_request=subject.request_id,
            plan=record.plan,
        )

    async def _view(
        self, record: JobRecord, context: ExecutionContext, *, cancelled: bool = False
    ) -> tuple[JobView, bool]:
        """Poll the job as the next numbered observation. True when the job ended.

        *cancelled*: the executor has just accepted this job's cancellation. A job it then
        holds no record of did not begin (a submission is answered only once it is made), so
        the cancellation ended it; leaving it unknown would hold its owner open for good.
        """
        subject = ObservationSubject(
            core_named(RequestId(root=record.request_id)), record.scope, record.admission_id
        )
        polled = await self._poll(self._executor_handle(record))
        if cancelled and polled.phase is PollPhase.UNSUBMITTED:
            polled = ExecutorPoll(
                phase=PollPhase.ENDED,
                terminal=ExecutorObservation(state=EvaluationState.CANCELED),
            )
        polled = self._with_ledgered_evidence(polled, record, subject)
        self._progress.report(
            record.handle_id, record.plan.purpose, polled, reused=record.reuses is not None
        )
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
        if record.reuses is not None:
            view = view.reused_from(record.reuses)
        return view, polled.phase is PollPhase.ENDED

    # observations

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
        """A refusal decided before the request created anything: it holds nothing."""
        observed = RequestObserved(
            observation=self._observations.observe(
                ObservationSubject.of(request),
                ObservationFacts.refused(reason),
                observed_at=context.now_at,
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

    def _nothing_held(
        self, request: RequestBase, context: ExecutionContext, reason: str
    ) -> ExecutionResult:
        """The job a release names holds nothing: it was never registered under this scope."""
        observed = self._observations.observe(
            ObservationSubject.of(request),
            ObservationFacts.nothing_held(reason),
            observed_at=context.now_at,
        )
        return ExecutionResult(observation=RequestObserved(observation=observed))

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
                failure=view.failure,
                operator_diagnostic=view.operator_diagnostic,
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
                failure=view.failure,
                operator_diagnostic=view.operator_diagnostic,
            ),
        )

    # submit

    async def _submit(
        self, request: SubmitMeasurement, context: ExecutionContext
    ) -> ExecutionResult:
        request_id = _identity(request)
        handle = handle_for(request_id)
        if request.decision_id is not None:
            self._decisions.record(handle, request.decision_id.root)
        try:
            eval_request = measurement_request(
                request.plan, request.scope, handle, deadline_at=context.until.at
            )
        except RejectedPlanError as error:
            return self._rejected(request, context, str(error), failure=MeasurementFailure.WORKLOAD)
        with self._store.exclusive():
            record = self._record(handle)
            if record is None:
                record = JobRecord.of(request, request_id, handle, context.payload_digest)
                self._registry.register(record)
        refused = await self._launch(request, context, eval_request, handle)
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
    ) -> ExecutionResult | None:
        """Submit the job if the executor holds none. A result means: stop, nothing was launched.

        A reusing submission launches nothing (``_attach``).
        """
        if request.reuses is not None:
            return await self._attach(request, request.reuses, context, handle)
        if (await self._poll(handle)).phase is PollPhase.UNSUBMITTED:
            if self._control is not None and request.decision_id is None:
                self._control.prepare_evaluation(handle, request.scope, permit=context.permit)
            try:
                await self._jobs.submit(eval_request, handle_id=handle)
            except ExecutorRejectedError as error:
                self._forget(request.scope, request.admission_id, handle)
                return self._rejected(request, context, str(error))
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-940008 [BLE001]; a failed submission may or may not have reached the executor, so it is reported as an unproven outcome that the next request inspects.
                return self._unknown(
                    request, context, f"submission raised {type(error).__name__}: {error}"
                )
        return None

    async def _attach(
        self,
        request: SubmitMeasurement,
        source: RequestId,
        context: ExecutionContext,
        handle: str,
    ) -> ExecutionResult | None:
        """Read the ended job of the run ``request`` reuses; refuse it if that job is not ended.

        The refusal is an infrastructure failure, so core's next submission measures afresh.
        """
        earlier = self._record(handle_for(source))
        if (
            earlier is not None
            and (await self._poll(self._executor_handle(earlier))).phase is PollPhase.ENDED
        ):
            return None
        self._forget(request.scope, request.admission_id, handle)
        return self._rejected(
            request, context, _UNREADABLE_RUN, failure=MeasurementFailure.INFRASTRUCTURE
        )

    # observe, inspect, collect

    async def _inspect(
        self, request: ObserveOwnedJob | InspectOwnedJob, context: ExecutionContext
    ) -> ExecutionResult:
        record = self._owned(request, request.resource_id)
        if record is None:
            return self._rejected(request, context, "job is not owned by this scope")
        record = await self._relaunch(record, context)
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
        # Core asks for a collection whenever an ended job holds no evidence. A job that
        # produced none has nothing to add to its end, which core already has; repeating
        # that end would only make core ask again, forever.
        return ExecutionResult(
            observation=RequestObserved(observation=own),
            owner_events=self._events(view) if view.evidence else (),
        )

    # cancel

    async def _cancel(self, request: CancelOwnedJob, context: ExecutionContext) -> ExecutionResult:
        record = self._owned(request, request.resource_id)
        if record is None or record.admission_id != request.admission_id:
            # Nothing registered the job under this scope and episode: it holds nothing.
            return self._nothing_held(request, context, "job is not owned by this scope")
        try:
            if record.reuses is None:
                # A reusing job runs nothing of its own, and the run it reads is not its to end.
                await self._jobs.cancel(record.executor_handle)
        except ExecutorCancellationUnconfirmedError:
            # The job is known and cancellation was requested; the view below
            # reports it unreleased until its end is observed.
            pass
        except ExecutorCancellationUnknownError as error:
            return self._unknown(request, context, f"cancellation outcome unknown: {error}")
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-940009 [BLE001]; a failed cancel leaves the job owned and running, so it is reported unproven and retried rather than halting the run.
            return self._unknown(request, context, f"cancel raised {type(error).__name__}: {error}")
        # A job the executor holds no record of is ended by the cancel: the loop runs a cancel
        # only after the job's submission has a result or was cancelled.
        view, ended = await self._view(record, context, cancelled=True)
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

    async def _relaunch(self, record: JobRecord, context: ExecutionContext) -> JobRecord:
        """Rerun a parked physical job under a new handle, retaining its logical resource id.

        The next poll after resume owns this step. Persist the new handle before submit,
        so a crash resumes the same idempotent submission instead of starting another.
        """
        if record.reuses is not None or not (record.parked or record.relaunch):
            return record
        if (
            not record.parked
            and (await self._poll(record.executor_handle)).phase is not PollPhase.UNSUBMITTED
        ):
            return record
        with self._store.exclusive():
            if record.parked:
                record = record.model_copy(
                    update={"parked": False, "relaunch": record.relaunch + 1}
                )
                self._registry.replace(record)
        if (await self._poll(record.executor_handle)).phase is PollPhase.UNSUBMITTED:
            eval_request = measurement_request(
                record.plan,
                record.scope,
                record.executor_handle,
                deadline_at=context.now_at + record.plan.time_bound,
            )
            await self._jobs.submit(eval_request, handle_id=record.executor_handle)
        return record

    async def _cancel_for_close(self, handle: str) -> None:
        """Ask the executor to cancel *handle*; nothing the cancel does decides the handle's fate."""
        try:
            record = self._record(handle)
            await self._jobs.cancel(handle if record is None else record.executor_handle)
        except Exception:  # noqa: BLE001  # lint-waiver: LW-940010 [BLE001]; one job that cannot be cancelled, for any reason, must not hide the others from the manifest. Whether it ended is not the failed cancel's to say: the close polls the executor's state next, so a handle that never launched is released and a live one stays unreleased.
            return

    # close

    async def _close(
        self, request: CloseAttemptScope, context: ExecutionContext
    ) -> ExecutionResult:
        # The loop holds a close until every other step of the scope has a result, so no
        # submission of the scope is still reaching the executor.
        with self._store.exclusive():
            handles = self._handles(request.scope, request.admission_id)
        ended = []
        for handle in handles:
            record = self._record(handle)
            if record is None or record.reuses is None:
                # A reusing job launched nothing; the executor holds no job under its handle.
                await self._cancel_for_close(handle)
            physical = (
                record.executor_handle if record is not None and record.reuses is None else handle
            )
            phase = (await self._poll(physical)).phase
            ended.append(phase in (PollPhase.ENDED, PollPhase.UNSUBMITTED))
        released = all(ended)
        own = self._own(
            request,
            context,
            ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=released,
            children=tuple(ResourceId(root=handle) for handle in handles),
            manifest=True,
            diagnostic=_close_diagnostic(released=released),
        )
        return ExecutionResult(observation=RequestObserved(observation=own))
