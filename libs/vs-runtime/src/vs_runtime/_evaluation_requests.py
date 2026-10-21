"""The EVALUATION role: measurement jobs driven through durable receipts.

Every request runs through ``ReceiptStore.run_once``, so it is idempotent by its
canonical identity: a begun marker precedes the effect and a sealed result is
replayed verbatim and never recomputed. Only a definitive result is sealed (see
``settle``); Unknown, an unproven acceptance and an unreleased cancellation are
returned unsealed so a retry re-observes them. A job is registered (its intent) before
any executor contact, and a restarted host inspects the executor before it
considers submitting again, so one submission identity is one executor job.

A submission enters the effect fences (``vs_runtime._effect_fence``) of its scope episode
and of its job in the same locked step as it registers the job, and again just before it
contacts the executor; closing the scope closes the episode's fence first, and cancelling a
job closes the job's. So a submission the close or cancel did not see submits nothing, and
neither answers released while a registered submission may still reach the executor.

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

from contextlib import suppress
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
    EvaluationState,
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    ExecutorPoll,
    ExecutorRejectedError,
    PollPhase,
    TrustedEvidence,
)
from vs_runtime._core_identity import core_identity, core_named
from vs_runtime._core_requests import ExecutionContext, ExecutionResult, settle
from vs_runtime._effect_fence import (
    EffectFences,
    Entered,
    FenceKey,
    FenceKind,
    MayHold,
    NothingHeld,
    Unrecorded,
)
from vs_runtime._evaluation_jobs import (
    JobView,
    RejectedPlanError,
    handle_for,
    job_view,
    measurement_request,
)
from vs_runtime._evidence_ledger import ReceiptEvidenceLedger
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
from vs_runtime._receipt_store import Conflict, Declined, Performed, Replayed, owner_key

if TYPE_CHECKING:
    from vs_evaluation.api import PollingEvaluationExecutor
    from vs_runtime._core_identity import CoreRequestId
    from vs_runtime._core_requests import EvaluationRoleRequest, OwnerEvent
    from vs_runtime._receipt_store import ReceiptStore, Settled, Transient
    from vs_runtime._run_control import RunControlChannel

_JOBS = "evaluation-jobs"
_FENCED = "scope episode is closed or the job was cancelled"
_SCOPES = "evaluation-scopes"
_UNREADABLE_RUN = "the run this measurement reuses can no longer be read as ended"


class JobRecord(BaseModel):
    """A registered job: the intent receipt written before the effect."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    handle_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    payload_digest: str = Field(min_length=1)
    scope: Scope
    admission_id: DecisionId | None = None
    plan: MeasurementPlan
    reuses: RequestId | None = None
    """The submission whose ended job this one reads, when it reuses an earlier run."""
    parked: bool = False
    relaunch: int = Field(default=0, ge=0)

    @property
    def executor_handle(self) -> str:
        """The executor job this record's observations read."""
        return (
            self.handle_id if self.relaunch == 0 else f"{self.handle_id}-relaunch-{self.relaunch}"
        )


class ScopeJobs(BaseModel):
    """The jobs registered under one admission episode of one scope.

    Whether the episode closed is its effect fence's to say (``scope_fence``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    scope: Scope
    admission_id: DecisionId | None = None
    handles: tuple[str, ...] = ()


def _identity(request: RequestBase) -> RequestId:
    if request.request_id is None:
        raise ContractError(("request_id",), "canonical identity required")
    return request.request_id


def _scope_key(scope: Scope, admission_id: DecisionId | None) -> str:
    episode = "" if admission_id is None else admission_id.root
    return f"{scope.owner.kind}:{scope.owner.root}:{scope.generation}:{episode}"


def _close_diagnostic(*, released: bool, submitting: bool) -> str:
    if released:
        return ""
    if submitting:
        return "a submission may still reach the executor"
    return "some jobs have not been observed to end"


def scope_fence(scope: Scope, admission_id: DecisionId | None) -> FenceKey:
    """The fence of one scope episode's jobs: submissions enter it, closing the scope closes it."""
    return FenceKey(FenceKind.SCOPE_EPISODE, _scope_key(scope, admission_id))


def job_fence(scope: Scope, admission_id: DecisionId | None, handle: str) -> FenceKey:
    """The fence of one job: its submission enters it, a cancel of it closes it.

    Keyed by the episode too, so a cancel from another scope closes only a fence of its own.
    """
    return FenceKey(FenceKind.MEASUREMENT_JOB, f"{_scope_key(scope, admission_id)}/{handle}")


def _fences_of(request: RequestBase, handle: str) -> tuple[FenceKey, FenceKey]:
    return (
        scope_fence(request.scope, request.admission_id),
        job_fence(request.scope, request.admission_id, handle),
    )


class MeasurementRequests:
    """Translate the six evaluation requests into polls and submissions of one executor."""

    def __init__(
        self,
        jobs: PollingEvaluationExecutor,
        store: ReceiptStore,
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
        self._fences = EffectFences(store)

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
                return ExecutionResult(
                    observation=RequestObserved(
                        observation=self._observations.observe(
                            ObservationSubject.of(request),
                            ObservationFacts.payload_conflict(),
                            observed_at=context.now_at,
                        )
                    )
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
            case CancelOwnedJob(for_park=True):
                return await self._park(request, context)
            case CancelOwnedJob():
                return await self._cancel(request, context)
            case CloseAttemptScope():
                return await self._close(request, context)
            case _:
                assert_never(request)

    # ledger

    def _record(self, handle_id: str) -> JobRecord | None:
        return self._store.load(_JOBS, "job", handle_id, JobRecord)

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

    def _register(self, scope: Scope, admission_id: DecisionId | None, handle: str) -> None:
        """Add *handle* to its scope episode's index; called inside the episode's open fence."""

        def decide(stored: ScopeJobs | None) -> tuple[ScopeJobs | None, None]:
            index = stored or ScopeJobs(scope=scope, admission_id=admission_id)
            if handle in index.handles:
                return None, None
            return index.model_copy(update={"handles": (*index.handles, handle)}), None

        self._store.modify(_SCOPES, "scope", _scope_key(scope, admission_id), ScopeJobs, decide)

    def _forget(self, scope: Scope, admission_id: DecisionId | None, handle: str) -> None:
        """Drop *handle* from its scope's index: its submission was refused, so it never was a job.

        A close lists every indexed handle as a resource core may own, and core would look
        for one it never heard of. An episode that already closed has listed the handle, so
        its index is left as the close read it.
        """

        def decide(stored: ScopeJobs | None) -> tuple[ScopeJobs | None, None]:
            if stored is None or handle not in stored.handles:
                return None, None
            kept = tuple(item for item in stored.handles if item != handle)
            return stored.model_copy(update={"handles": kept}), None

        with self._store.exclusive():
            if self._fences.is_closed(scope_fence(scope, admission_id)):
                return
            self._store.modify(_SCOPES, "scope", _scope_key(scope, admission_id), ScopeJobs, decide)

    def _handles(self, scope: Scope, admission_id: DecisionId | None) -> tuple[str, ...]:
        index = self._store.load(_SCOPES, "scope", _scope_key(scope, admission_id), ScopeJobs)
        return () if index is None else index.handles

    def _enter(
        self, fences: tuple[FenceKey, ...], by: CoreRequestId, context: ExecutionContext
    ) -> bool:
        """Enter every fence of a submission; True when all are open. Call under ``exclusive``."""
        admissions = [self._fences.enter(fence, by=by, host=context.fence) for fence in fences]
        return all(isinstance(admission, Entered) for admission in admissions)

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

    def _never_held(
        self,
        request: RequestBase,
        context: ExecutionContext,
        proof: NothingHeld,
        fence: FenceKey,
        reason: str,
    ) -> ExecutionResult:
        """The job a release names holds nothing, now or later: its fence proves it."""
        observed = self._observations.observe(
            ObservationSubject.of(request),
            ObservationFacts.never_held(proof, fence, reason),
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
        fences = _fences_of(request, handle)
        by = core_identity(request)
        with self._store.exclusive():
            admitted = self._enter(fences, by, context)
            record = self._record(handle)
            if record is None and admitted:
                self._register(request.scope, request.admission_id, handle)
                record = JobRecord(
                    handle_id=handle,
                    request_id=request_id.root,
                    payload_digest=context.payload_digest,
                    scope=request.scope,
                    admission_id=request.admission_id,
                    plan=request.plan,
                    reuses=request.reuses,
                )
                self._store.record_once(_JOBS, "job", handle, record)
        if record is None:
            # The scope closed, or the job was cancelled, before this submission registered.
            return self._rejected(request, context, _FENCED)
        try:
            refused = await self._launch(request, context, eval_request, handle)
        finally:
            for fence in fences:
                self._fences.leave(fence, by=by)
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
            problem = self._authority_problem(request, context)
            if problem is not None:
                return self._unknown(request, context, problem)
            if not self._enter(_fences_of(request, handle), core_identity(request), context):
                # Fenced while the executor held nothing: the job never will exist.
                return self._rejected(request, context, _FENCED)
            if self._control is not None and request.decision_id is None:
                self._control.prepare_evaluation(handle, request.scope)
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
        record = await self._relaunch(record, request, context)
        if isinstance(record, ExecutionResult):
            return record
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
        # Fence first: a submission of this job that has not entered by now submits nothing.
        fence = job_fence(request.scope, request.admission_id, request.resource_id.root)

        def episode_job() -> JobRecord | None:
            record = self._owned(request, request.resource_id)
            return (
                record
                if record is not None and record.admission_id == request.admission_id
                else None
            )

        closure, record = self._fences.close_and_load(
            fence, by=core_identity(request), host=context.fence, load=episode_job
        )
        submitting = isinstance(closure, MayHold) and closure.live
        if isinstance(record, Unrecorded):
            # A submission that entered registers in the same step, so one that left no
            # record was fenced before it registered, or died first.
            if submitting:
                return self._unknown(request, context, "the job's submission is still running")
            proof = (
                closure
                if isinstance(closure, NothingHeld)
                else self._fences.cleared(closure, record)
            )
            return self._never_held(
                request, context, proof, fence, "job is not owned by this scope"
            )
        problem = self._authority_problem(request, context)
        if problem is not None:
            return self._unknown(request, context, problem)
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
        # A job the executor holds no record of is ended by the cancel only once no submission
        # of it can still reach the executor.
        view, ended = await self._view(record, context, cancelled=not submitting)
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

    async def _park(self, request: CancelOwnedJob, context: ExecutionContext) -> ExecutionResult:
        """Release a job without publishing a failed measurement or closing its effect fence."""
        record = self._owned(request, request.resource_id)
        if record is None:
            return self._unknown(request, context, "park names no owned job")
        problem = self._authority_problem(request, context)
        if problem is not None:
            return self._unknown(request, context, problem)
        polled = await self._poll(self._executor_handle(record))
        dependency = self._control is not None and self._control.evaluation_is_dependency(
            record.handle_id
        )
        finishing = polled.phase is PollPhase.RUNNING or dependency
        if not finishing and not record.parked:
            try:
                record = await self._hold_for_relaunch(record, polled)
            except ExecutorCancellationUnknownError as error:
                return self._unknown(request, context, str(error))
        polled = await self._poll(self._executor_handle(record))
        released = record.reuses is not None or polled.phase in (
            PollPhase.ENDED,
            PollPhase.UNSUBMITTED,
        )
        if self._control is not None and (finishing or released):
            self._control.settle_drain_job(request.resource_id.root)
        while not released:
            await self._jobs.wait_for_change(record.executor_handle, 1.0)
            polled = await self._poll(self._executor_handle(record))
            released = polled.phase in (PollPhase.ENDED, PollPhase.UNSUBMITTED)
        if self._control is not None:
            self._control.settle_drain_job(request.resource_id.root)
        own = self._own(
            request,
            context,
            ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=released,
            released=released,
            resource=request.resource_id,
            diagnostic="" if released else "park awaits physical job termination",
        )
        events = ()
        if finishing:
            view, _ = await self._view(record, context)
            events = self._events(view)
        return ExecutionResult(observation=RequestObserved(observation=own), owner_events=events)

    async def _hold_for_relaunch(self, record: JobRecord, polled: ExecutorPoll) -> JobRecord:
        """Cancel queued physical work, retaining its logical submission for relaunch."""
        if record.reuses is None and polled.phase is not PollPhase.ENDED:
            with suppress(ExecutorCancellationUnconfirmedError):
                await self._jobs.cancel(record.executor_handle)
        record = record.model_copy(update={"parked": True})
        self._store.replace(_JOBS, "job", record.handle_id, record)
        return record

    async def _relaunch(
        self,
        record: JobRecord,
        request: ObserveOwnedJob | InspectOwnedJob,
        context: ExecutionContext,
    ) -> JobRecord | ExecutionResult:
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
        fences = _fences_of(request, record.handle_id)
        by = core_identity(request)
        try:
            with self._store.exclusive():
                if not self._enter(fences, by, context):
                    return self._rejected(request, context, _FENCED)
                if record.parked:
                    record = record.model_copy(
                        update={"parked": False, "relaunch": record.relaunch + 1}
                    )
                    self._store.replace(_JOBS, "job", record.handle_id, record)
            if (await self._poll(record.executor_handle)).phase is PollPhase.UNSUBMITTED:
                problem = self._authority_problem(request, context)
                if problem is not None:
                    return self._unknown(request, context, problem)
                eval_request = measurement_request(
                    record.plan,
                    record.scope,
                    record.executor_handle,
                    deadline_at=context.now_at + record.plan.time_bound,
                )
                await self._jobs.submit(eval_request, handle_id=record.executor_handle)
            return record
        finally:
            for fence in fences:
                self._fences.leave(fence, by=by)

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
        problem = self._authority_problem(request, context)
        if problem is not None:
            return self._unknown(request, context, problem)
        # Fence first: a submission that has not entered by now submits nothing.
        with self._store.exclusive():
            closure = self._fences.close(
                scope_fence(request.scope, request.admission_id),
                by=core_identity(request),
                host=context.fence,
            )
            handles = self._handles(request.scope, request.admission_id)
        # A submission still inside the fence may yet reach the executor, so a handle that
        # polls unsubmitted is ended only once no submission can still be submitting.
        submitting = isinstance(closure, MayHold) and closure.live
        ended = []
        for handle in handles:
            if self._authority_problem(request, context) is not None:
                ended.append(False)
                continue
            record = self._record(handle)
            if record is None or record.reuses is None:
                # A reusing job launched nothing; the executor holds no job under its handle.
                await self._cancel_for_close(handle)
            physical = (
                record.executor_handle if record is not None and record.reuses is None else handle
            )
            phase = (await self._poll(physical)).phase
            ended.append(
                phase is PollPhase.ENDED or (phase is PollPhase.UNSUBMITTED and not submitting)
            )
        released = all(ended) and not submitting
        own = self._own(
            request,
            context,
            ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=released,
            children=tuple(ResourceId(root=handle) for handle in handles),
            manifest=True,
            diagnostic=_close_diagnostic(released=released, submitting=submitting),
        )
        return ExecutionResult(observation=RequestObserved(observation=own))
