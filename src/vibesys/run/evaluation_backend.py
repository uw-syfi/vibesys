"""VibeSys semantic adapter over the provider-neutral evaluation lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, ValidationError

from vs_evaluation.api import (
    MAX_AGENT_AWAIT_S,
    MAX_STAGE_SUMMARY_TAIL_CHARS,
    AvailabilitySnapshot,
    AvailabilityState,
    ContentDigest,
    CostClass,
    EvaluationAdmissionStoppedError,
    EvaluationAwaitResult,
    EvaluationCoordinator,
    EvaluationDependencyError,
    EvaluationExecutor,
    EvaluationLifecycleEvent,
    EvaluationOperationSnapshot,
    EvaluationRequest,
    EvaluationSettlements,
    EvaluationStageOutcome,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    ExecutorObservation,
    PartialMeasurement,
    ProfilerAgentCapacityError,
    ProfilerAgentService,
    ProfilerAgentUnavailableError,
    ProfilerOperation,
    ProfilerOperationState,
    ProfilerResultOutcome,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    ResourceRequirements,
    ReuseStatus,
    RevisionConflictError,
    ScopeClosingError,
    ScopeLifecycleStore,
    ScopePhase,
    ScopeRelease,
    ScopeSubmissionTracker,
    SettlementErrorCode,
    StageState,
    StoredEvaluation,
    SubmittedSemanticEvaluation,
    TrustedEvidence,
    failure_signature,
    stable_handle_id,
)
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentEvaluation,
    AgentEvaluationMetric,
    AgentEvaluationStage,
    AgentEvaluationStageOutcome,
    AgentEvaluationStatus,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CandidateProfile,
    CandidateProfileComponent,
    CandidateProfileStatus,
    Evaluation,
    LocalValidationEvaluation,
    MetricDirection,
    ReleasedJobs,
    RuntimeContractError,
    Workspace,
    Workspaces,
    member_workspace_id,
)
from vs_runtime.api.infrastructure import RunStopped, TrustedEvaluationPlan

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from vs_evaluation.api import EvaluationStateNamespace
    from vs_prompts.api import RenderedPrompt
    from vs_runtime.api.infrastructure import AgentToolBindingContext

_STATE_DIRECTORY = "semantic-evaluations"
_RETRYABLE_STATES = frozenset(
    {EvaluationState.FAILED, EvaluationState.CANCELED, EvaluationState.SUPERSEDED}
)
_INDEX_PATH = f"{_STATE_DIRECTORY}/index.json"


class _Clock:
    def monotonic(self) -> float:
        return time.monotonic()


class _EvaluationIndex(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handle_ids: tuple[str, ...] = ()


class _NamespaceEvaluationStore:
    """Durable evaluation lifecycle records inside the run's local namespace."""

    def __init__(self, namespace: EvaluationStateNamespace) -> None:
        self._namespace = namespace
        self._lock = asyncio.Lock()

    async def claim(self, request: EvaluationRequest, *, handle_id: str) -> StoredEvaluation:
        async with self._lock:
            if stable_handle_id(request.key) != handle_id:
                message = "evaluation handle does not match its request key"
                raise ValueError(message)
            index = self._load_index()
            if handle_id in index.handle_ids:
                record = self._namespace.load(self._path(handle_id), StoredEvaluation)
                if record.request != request:
                    message = "evaluation key already identifies different work"
                    raise ValueError(message)
                return record
            record = StoredEvaluation(
                handle_id=handle_id,
                request=request,
                state=EvaluationState.QUEUED,
                revision=0,
                submission_pending=True,
                dispatch_authorized=False,
            )
            self._namespace.save(self._path(handle_id), record)
            self._namespace.save(
                _INDEX_PATH,
                _EvaluationIndex(handle_ids=(*index.handle_ids, handle_id)),
            )
            return record

    async def get(self, handle_id: str) -> StoredEvaluation | None:
        async with self._lock:
            if handle_id not in self._load_index().handle_ids:
                return None
            return self._namespace.load(self._path(handle_id), StoredEvaluation)

    async def get_by_key(self, key: str) -> StoredEvaluation | None:
        return await self.get(stable_handle_id(key))

    async def compare_and_set(
        self, record: StoredEvaluation, *, expected_revision: int
    ) -> StoredEvaluation:
        async with self._lock:
            current = self._namespace.load(self._path(record.handle_id), StoredEvaluation)
            if current.revision != expected_revision:
                raise RevisionConflictError(record.handle_id, current.revision, expected_revision)
            if record.revision != expected_revision + 1:
                message = "replacement evaluation revision must increment by one"
                raise ValueError(message)
            self._namespace.save(self._path(record.handle_id), record)
            return record

    async def records(self) -> tuple[StoredEvaluation, ...]:
        async with self._lock:
            return tuple(
                self._namespace.load(self._path(handle_id), StoredEvaluation)
                for handle_id in self._load_index().handle_ids
            )

    async def nonterminal(self) -> tuple[StoredEvaluation, ...]:
        terminal = {
            EvaluationState.SUCCEEDED,
            EvaluationState.FAILED,
            EvaluationState.CANCELED,
            EvaluationState.SUPERSEDED,
        }
        return tuple(record for record in await self.records() if record.state not in terminal)

    def _load_index(self) -> _EvaluationIndex:
        return self._namespace.load_optional(_INDEX_PATH, _EvaluationIndex) or _EvaluationIndex()

    @staticmethod
    def _path(handle_id: str) -> str:
        return f"{_STATE_DIRECTORY}/{handle_id}.json"


class SemanticEvaluationStage(BaseModel):
    """Trusted semantic identity carried through an executor-specific codec."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: str
    kind: EvidenceKind
    fingerprints: EvidenceFingerprints
    submitted_at_s: FiniteFloat | None = Field(default=None, ge=0)
    deadline_at_s: FiniteFloat | None = Field(default=None, ge=0)
    deadline_unavailable: str | None = Field(default=None, min_length=1)


class _LocalSemanticExecutor:
    """Execute immutable candidate snapshots through the trusted runtime API."""

    def __init__(self, evaluation: Evaluation, workspaces: Workspaces) -> None:
        self._evaluation = evaluation
        self._workspaces = workspaces
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._observations: dict[str, ExecutorObservation] = {}
        self._changes: dict[str, asyncio.Event] = {}

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        del requirements
        active = sum(not task.done() for task in self._tasks.values())
        return AvailabilitySnapshot(
            state=AvailabilityState.IMMEDIATE if active == 0 else AvailabilityState.BUSY,
            capacity=1,
            in_flight=active,
            queue_depth=max(0, active - 1),
            reuse_status=ReuseStatus.UNKNOWN,
            cost_class=CostClass.UNKNOWN,
            observed_at=time.monotonic(),
            fresh_for_s=1.0,
            supported_evidence_kinds=(EvidenceKind.ACCURACY.value, EvidenceKind.BENCHMARK.value),
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        if handle_id in self._tasks or handle_id in self._observations:
            return
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.QUEUED))
        self._tasks[handle_id] = asyncio.create_task(self._run(handle_id, request))

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        """Read process-local evidence without starting recovery tasks."""
        return self._observations.get(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        return self._observations.get(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        event = self._changes.setdefault(handle_id, asyncio.Event())
        if event.is_set():
            event.clear()
            return
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except TimeoutError:
            return
        event.clear()

    async def cancel(self, handle_id: str) -> None:
        task = self._tasks.get(handle_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))

    async def close(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, handle_id: str, request: EvaluationRequest) -> None:
        first = SemanticEvaluationStage.model_validate(request.stages[0].payload)
        workspace = await self._workspaces.create_candidate(first.snapshot)
        results: list[EvaluationStepResult] = []
        self._publish(
            handle_id,
            ExecutorObservation(
                state=EvaluationState.RUNNING, current_stage=request.stages[0].name
            ),
        )
        failure: str | None = None
        try:
            for step in request.stages:
                stage = SemanticEvaluationStage.model_validate(step.payload)
                evidence = await self._evaluate(workspace, stage, handle_id)
                results.append(
                    EvaluationStepResult(
                        name=step.name,
                        state=StageState.SUCCEEDED,
                        result=evidence.model_dump(mode="json"),
                    )
                )
                if (
                    evidence.kind is EvidenceKind.ACCURACY
                    and evidence.outcome is EvidenceOutcome.FAILED
                ):
                    skipped = request.stages[len(results) :]
                    results.extend(
                        EvaluationStepResult(name=remaining.name, state=StageState.SKIPPED)
                        for remaining in skipped
                    )
                    if skipped:
                        # A successful evaluation must complete every planned stage, so
                        # skipping the rest makes this a failed evaluation. The message
                        # carries the accuracy diagnostics back to the submitting agent.
                        failure = evidence.semantic_summary or "Accuracy check failed."
                    break
                if len(results) < len(request.stages):
                    # A waiting agent sees each finished stage and the one now running.
                    self._publish(
                        handle_id,
                        ExecutorObservation(
                            state=EvaluationState.RUNNING,
                            current_stage=request.stages[len(results)].name,
                            stage_results=tuple(results),
                        ),
                    )
            self._publish(
                handle_id,
                ExecutorObservation(
                    state=EvaluationState.SUCCEEDED if failure is None else EvaluationState.FAILED,
                    stage_results=tuple(results),
                    failure=failure,
                ),
            )
        except asyncio.CancelledError:
            self._publish(handle_id, ExecutorObservation(state=EvaluationState.CANCELED))
            raise
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-930049 [BLE001]; this lifecycle boundary converts arbitrary extension failures into durable diagnostics; narrower catches would let unknown providers bypass the contract.
            self._publish(
                handle_id,
                ExecutorObservation(state=EvaluationState.FAILED, failure=str(error)),
            )
        finally:
            await workspace.discard()

    async def _evaluate(
        self, workspace: Workspace, stage: SemanticEvaluationStage, handle_id: str
    ) -> TrustedEvidence:
        if stage.kind is EvidenceKind.ACCURACY:
            result = await self._evaluation.accuracy(workspace)
            outcome = EvidenceOutcome.PASSED if result.passed else EvidenceOutcome.FAILED
            summary = result.feedback
            metrics: tuple[EvidenceMetric, ...] = ()
            partial = None
        elif stage.kind is EvidenceKind.BENCHMARK:
            result = await self._evaluation.benchmark(workspace)
            outcome = EvidenceOutcome.PASSED if result.passed else EvidenceOutcome.FAILED
            summary = result.feedback
            partial = result.partial_measurement
            metrics = tuple(
                EvidenceMetric(
                    name=name,
                    value=value,
                    direction=(
                        result.metric_direction.value
                        if name == result.metric_name and result.metric_direction is not None
                        else None
                    ),
                    unit=result.metric_unit if name == result.metric_name else None,
                )
                for name, value in sorted((result.row or {}).items())
            )
        else:
            message = "direct profile evaluation is not supported by the trusted runtime"
            raise ValueError(message)
        evidence_id = evidence_identity(
            stage,
            EvidenceResultIdentity(
                evaluation_id=handle_id,
                outcome=outcome,
                summary=summary,
                metrics=metrics,
                partial=partial,
            ),
        )
        return TrustedEvidence(
            evidence_id=evidence_id,
            evaluation_id=handle_id,
            stage_name=stage.kind.value,
            kind=stage.kind,
            fingerprints=stage.fingerprints,
            trusted_inputs=stage.fingerprints.candidate,
            outcome=outcome,
            semantic_summary=summary,
            metrics=metrics,
            partial_measurement=partial,
            accepted_round=0,
        )

    def _publish(self, handle_id: str, observation: ExecutorObservation) -> None:
        self._observations[handle_id] = observation
        self._changes.setdefault(handle_id, asyncio.Event()).set()


class SemanticEvaluationExecutor(EvaluationExecutor, Protocol):
    """Owned executor accepted by the semantic evaluation service."""

    async def close(self) -> None:
        """Release in-flight provider work and local resources."""
        ...


@dataclass(frozen=True, slots=True)
class SemanticEvaluationIdentity:
    """Stable non-candidate identities supplied by product composition."""

    evaluator: ContentDigest
    workload: ContentDigest
    environment: ContentDigest


class SemanticEvaluationBackend:
    """Role service backend with durable lifecycle and exact evidence reuse."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-930050 [PLR0913]; these arguments are independent injected ports or policy facts; grouping them in a DTO would add a shallow mutable carrier and obscure ownership.
        self,
        evaluation: Evaluation,
        workspaces: Workspaces,
        namespace: EvaluationStateNamespace,
        identity: SemanticEvaluationIdentity,
        *,
        executor: SemanticEvaluationExecutor | None = None,
        events: Callable[[EvaluationLifecycleEvent], None] | None = None,
        plan: TrustedEvaluationPlan | None = None,
        queue_allowance_seconds: int | None = None,
        submitted_time: Callable[[], float] = time.time,
    ) -> None:
        """Bind official evaluation, isolated workspaces, and durable state."""
        if plan is not None and queue_allowance_seconds is None:
            message = "a declared evaluation plan requires queue_allowance_seconds"
            raise RuntimeContractError(message)
        self._workspaces_by_scope: dict[str | None, Workspace] = {}
        self._workspaces = workspaces
        self._identity = identity
        self._plan = plan
        self._queue_allowance_seconds = queue_allowance_seconds
        self._submitted_time = submitted_time
        self._executor = executor or _LocalSemanticExecutor(evaluation, workspaces)
        self._store = _NamespaceEvaluationStore(namespace)
        self._scope_ledger = ScopeLifecycleStore(namespace)
        self._submissions = ScopeSubmissionTracker()
        # Serializes "pick a key, then claim it" so two submissions of identical
        # content cannot both pick the same fresh key with different snapshots.
        self._claim_lock = asyncio.Lock()
        self._coordinator = EvaluationCoordinator(
            self._executor,
            self._store,
            _Clock(),
            events=events or (lambda _event: None),
        )

    def bind(self, context: AgentToolBindingContext) -> None:
        """Register the live workspace addressed by a role-scoped grant."""
        self._workspaces_by_scope[context.workspace.id] = context.workspace

    async def start(self) -> None:
        """Reconcile durable nonterminal work after process restart."""
        # Released ownership is reconciled before submission recovery: an
        # interrupted trusted capture must not be restarted as ordinary work.
        legacy_closing = any(
            scope.reconcile_legacy_captures and scope.phase is ScopePhase.CLOSING
            for scope in self._scope_ledger.snapshot().scopes
        )
        for record in await self._coordinator.history():
            scope_id = record.request.owner_scope
            legacy_capture = (
                legacy_closing
                and scope_id is None
                and any(stage.name == EvidenceKind.PROFILE.value for stage in record.request.stages)
            )
            if legacy_capture or (scope_id is not None and self._scope_ledger.released(scope_id)):
                await self._coordinator.cancel(record.handle_id)
        await self._coordinator.reconcile()

    async def close(self) -> None:
        """Cancel and drain locally owned evaluation tasks."""
        await self._executor.close()

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        """Report current capacity for semantic evaluation work."""
        return await self._coordinator.availability(requirements)

    async def submit_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
        *,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]],
    ) -> SubmittedSemanticEvaluation:
        """Snapshot a candidate and submit exact semantic evidence work."""
        workspace = self._require_workspace(scope_id)
        snapshot = await workspace.snapshot("agent-evaluation")
        return await self.submit_revision_evidence(snapshot, kinds, scope_id=scope_id, own=own)

    async def submit_revision_evidence(
        self,
        snapshot: str,
        kinds: tuple[EvidenceKind, ...],
        *,
        scope_id: str | None = None,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]] | None = None,
    ) -> SubmittedSemanticEvaluation:
        """Submit exact semantic evidence work for one recorded revision.

        Work for the same content and kinds is joined, so an agent that later
        submits the same candidate reads this evaluation instead of a new one.
        """
        async with self._submissions.track(scope_id):
            if scope_id is not None and self._scope_ledger.released(scope_id):
                raise ScopeClosingError(scope_id)
            return await self._submit_revision_evidence(snapshot, kinds, scope_id=scope_id, own=own)

    async def drain_submissions(self, scope_id: str | None) -> None:
        """Join admitted handlers after the scope's Closing intent fences new work."""
        await self._submissions.drain(scope_id)

    async def _submit_revision_evidence(
        self,
        snapshot: str,
        kinds: tuple[EvidenceKind, ...],
        *,
        scope_id: str | None,
        own: Callable[[SubmittedSemanticEvaluation], Awaitable[None]] | None,
    ) -> SubmittedSemanticEvaluation:
        fingerprints = await self._fingerprints(snapshot)
        # Only choosing and claiming the key is serialized. Staging and submitting to
        # the executor can take tens of seconds, so they run outside the lock.
        async with self._claim_lock:
            if scope_id is not None and self._scope_ledger.released(scope_id):
                raise ScopeClosingError(scope_id)
            generation = next(
                (
                    scope.generation
                    for scope in self._scope_ledger.snapshot().scopes
                    if scope.scope_id == scope_id
                ),
                0,
            )
            key, existing = await self._claimable_key(fingerprints, kinds, scope_id, generation)
            submitted_at_s = self._submitted_time()
            deadline_at_s = None
            deadline_unavailable = None
            if (
                existing is None
                and self._plan is not None
                and self._queue_allowance_seconds is not None
            ):
                stages = cast(
                    "tuple[Literal['accuracy', 'benchmark', 'profile', 'framework_setup'], ...]",
                    ("framework_setup", *(kind.value for kind in kinds)),
                )
                # Validate clock and queue configuration even when an ordinary
                # evaluation has no declared execution bound for suspension.
                self._plan.suspension_deadline_s(
                    submitted_at_s, ("framework_setup",), self._queue_allowance_seconds
                )
                try:
                    self._plan.execution_budget_seconds(stages)
                except ValueError as error:
                    # These validated, unique stage names leave missing declared
                    # timeouts as the only unavailable execution-budget case.
                    deadline_unavailable = str(error)
                else:
                    deadline_at_s = self._plan.suspension_deadline_s(
                        submitted_at_s, stages, self._queue_allowance_seconds
                    )
            request = (
                existing.request
                if existing is not None
                else EvaluationRequest(
                    key=key,
                    owner_scope=scope_id,
                    owner_generation=generation,
                    stages=tuple(
                        EvaluationStep(
                            name=kind.value,
                            payload=SemanticEvaluationStage(
                                snapshot=snapshot,
                                kind=kind,
                                fingerprints=fingerprints,
                                submitted_at_s=submitted_at_s,
                                deadline_at_s=deadline_at_s,
                                deadline_unavailable=deadline_unavailable,
                            ).model_dump(mode="json"),
                        )
                        for kind in kinds
                    ),
                )
            )
            if existing is None:
                # Claiming makes the key visible to the next submission's pick. The
                # coordinator's own claim below returns this same record.
                await self._store.claim(request, handle_id=stable_handle_id(key))
            if own is not None:
                await own(
                    SubmittedSemanticEvaluation(
                        handle_id=stable_handle_id(key), fingerprints=fingerprints
                    )
                )
        if scope_id is not None and self._scope_ledger.released(scope_id):
            await self._coordinator.cancel(stable_handle_id(key))
            raise ScopeClosingError(scope_id)
        try:
            self._submissions.check_admission()
        except EvaluationAdmissionStoppedError:
            await self._coordinator.cancel(stable_handle_id(key))
            raise
        # An existing record means the same content was already submitted from another
        # snapshot; any snapshot with these fingerprints is the same work, so join it.
        handle = await self._coordinator.submit(request)
        return SubmittedSemanticEvaluation(handle_id=handle.id, fingerprints=fingerprints)

    async def _claimable_key(
        self,
        fingerprints: EvidenceFingerprints,
        kinds: tuple[EvidenceKind, ...],
        scope_id: str | None,
        generation: int,
    ) -> tuple[str, StoredEvaluation | None]:
        """Return the key of live or completed work for this content, else a fresh key.

        Identity is the content fingerprints and evidence kinds, never the snapshot
        commit. An attempt that ended without a result (failed, canceled, or
        superseded) does not block a new attempt at the same content.
        """
        document: dict[str, JsonValue] = {
            "fingerprints": fingerprints.model_dump(mode="json"),
            "kinds": [kind.value for kind in kinds],
            "scope_id": scope_id,
            "scope_generation": generation,
        }
        attempt = 0
        while True:
            if attempt:
                document["attempt"] = attempt
            key = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            existing = await self._store.get_by_key(key)
            if existing is None or existing.state not in _RETRYABLE_STATES:
                return key, existing
            attempt += 1

    async def accepted_evidence(
        self,
        scope_id: str | None,
        kinds: tuple[EvidenceKind, ...],
    ) -> tuple[TrustedEvidence, ...]:
        """Return successful evidence matching the live revision exactly."""
        workspace = self._require_workspace(scope_id)
        revision = workspace.revision
        if revision is None:
            return ()
        fingerprints = await self._fingerprints(revision)
        return await self._matching_evidence(fingerprints, kinds)

    async def snapshot(self, scope_id: str | None, *, label: str) -> str:
        """Record and return the exact live candidate revision."""
        return await self._require_workspace(scope_id).snapshot(label)

    async def evidence_for(
        self,
        workspace: Workspace,
        kinds: tuple[EvidenceKind, ...],
    ) -> tuple[TrustedEvidence, ...]:
        """Return accepted evidence for the workspace's exact candidate content."""
        revision = workspace.revision
        if revision is None:
            return ()
        fingerprints = await self._fingerprints(revision)
        return await self._matching_evidence(fingerprints, kinds)

    async def resolve_profile_evidence(
        self,
        principal_id: str,
        scope_id: str | None,
        candidate_snapshot_id: str,
        evidence_ids: tuple[str, ...],
    ) -> tuple[TrustedEvidence, ...]:
        """Resolve profiler references only for the exact candidate snapshot."""
        del principal_id, scope_id
        requested = set(evidence_ids)
        resolved: dict[str, TrustedEvidence] = {}
        candidate = await self._candidate_fingerprint(candidate_snapshot_id)
        for record in await self._coordinator.history():
            for result in record.stage_results:
                if result.state is not StageState.SUCCEEDED or result.result is None:
                    continue
                evidence = TrustedEvidence.model_validate(result.result)
                if (
                    evidence.evidence_id in requested
                    and evidence.kind is EvidenceKind.PROFILE
                    and evidence.fingerprints.candidate == candidate
                ):
                    existing = resolved.get(evidence.evidence_id)
                    if existing is not None and existing != evidence:
                        message = "trusted profile evidence ID identifies conflicting content"
                        raise ValueError(message)
                    resolved[evidence.evidence_id] = evidence
        if set(resolved) != requested:
            message = "unknown trusted profile evidence for the exact candidate snapshot"
            raise ValueError(message)
        return tuple(resolved.values())

    async def owned_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Project resource ownership from durable claims, including unsubmitted ones.

        ``None`` requests all run-owned resources for stop cleanup.
        """
        legacy_scope = any(
            scope.scope_id == scope_id
            and scope.reconcile_legacy_captures
            and scope.phase is ScopePhase.CLOSING
            for scope in self._scope_ledger.snapshot().scopes
        )
        return tuple(
            record.handle_id
            for record in await self._coordinator.history()
            if scope_id is None
            or record.request.owner_scope == scope_id
            or (
                legacy_scope
                and record.request.owner_scope is None
                and any(stage.name == EvidenceKind.PROFILE.value for stage in record.request.stages)
            )
        )

    async def recorded_status(self, handle_id: str) -> EvaluationState:
        """Read committed state without dispatching work."""
        return await self._coordinator.recorded_status(handle_id)

    async def inspect_snapshot(self, handle_id: str) -> StoredEvaluation | None:
        """Inspect once without starting or cancelling external work."""
        return await self._coordinator.inspect_snapshot(handle_id)

    async def recorded_snapshot(self, handle_id: str) -> StoredEvaluation:
        """Read durable ownership and immutable submission identity without external I/O.

        Settlement observation reads this record before registering a host wait,
        so a terminal result persisted by an earlier host requires no new wait.
        """
        return await self._coordinator.recorded_snapshot(handle_id)

    async def recorded_submission(self, handle_id: str) -> SubmittedSemanticEvaluation:
        """Recover canonical identity from immutable captured stages without external I/O.

        Access records grant ownership; they cannot redefine submitted content.
        All stages in one semantic submission must share the same exact capture.
        """
        record = await self.recorded_snapshot(handle_id)
        captures: list[SemanticEvaluationStage] = []
        for stage in record.request.stages:
            try:
                capture = SemanticEvaluationStage.model_validate(stage.payload)
            except ValidationError as error:
                raise EvaluationDependencyError(
                    SettlementErrorCode.IDENTITY_CONFLICT, handle_id
                ) from error
            if stage.name != capture.kind.value or (
                captures
                and (capture.snapshot, capture.fingerprints)
                != (captures[0].snapshot, captures[0].fingerprints)
            ):
                raise EvaluationDependencyError(SettlementErrorCode.IDENTITY_CONFLICT, handle_id)
            captures.append(capture)
        if not captures:
            raise EvaluationDependencyError(SettlementErrorCode.IDENTITY_CONFLICT, handle_id)
        return SubmittedSemanticEvaluation(
            handle_id=handle_id, fingerprints=captures[0].fingerprints
        )

    async def status(self, handle_id: str) -> EvaluationState:
        """Return the durable lifecycle state for one handle."""
        return await self._coordinator.status(handle_id)

    async def operation_snapshot(self, handle_id: str) -> EvaluationOperationSnapshot:
        """Return lifecycle state and trust-boundary accepted result identity."""
        record = await self._coordinator.snapshot(handle_id)
        evidence = _stage_evidence(record)
        return EvaluationOperationSnapshot(
            handle_id=handle_id,
            state=record.state,
            current_stage=record.current_stage,
            evidence_recorded=(
                record.state is EvaluationState.SUCCEEDED
                and len(evidence) == len(record.request.stages)
            ),
            stage_outcomes=tuple(
                EvaluationStageOutcome(
                    kind=item.kind,
                    outcome=item.outcome,
                    metrics=item.metrics,
                    partial_measurement=item.partial_measurement,
                    summary_tail=(
                        item.semantic_summary[-MAX_STAGE_SUMMARY_TAIL_CHARS:]
                        if item.semantic_summary
                        else None
                    ),
                )
                for item in evidence
            ),
            evidence_ids=tuple(item.evidence_id for item in evidence),
            failure=agent_evaluation(record).failure,
        )

    async def await_result(self, handle_id: str, timeout_s: float) -> EvaluationAwaitResult:
        """Await one handle for at most the caller's bounded timeout."""
        return await self._coordinator.await_result(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> StoredEvaluation:
        """Request cancellation and return the durable operation record."""
        return await self._coordinator.cancel(handle_id)

    async def agent_evaluations(self, handle_ids: tuple[str, ...]) -> tuple[AgentEvaluation, ...]:
        """Describe each handle's current outcome, in the given order."""
        return tuple(
            [
                agent_evaluation(await self._coordinator.snapshot(handle_id))
                for handle_id in handle_ids
            ]
        )

    def _require_workspace(self, scope_id: str | None) -> Workspace:
        workspace = self._workspaces_by_scope.get(scope_id)
        if workspace is None:
            message = f"evaluation workspace scope {scope_id!r} is not bound"
            raise ValueError(message)
        return workspace

    async def _matching_evidence(
        self,
        fingerprints: EvidenceFingerprints,
        kinds: tuple[EvidenceKind, ...],
    ) -> tuple[TrustedEvidence, ...]:
        accepted: dict[str, TrustedEvidence] = {}
        for record in await self._coordinator.history():
            if record.state is not EvaluationState.SUCCEEDED:
                continue
            for result in record.stage_results:
                if result.state is not StageState.SUCCEEDED or result.result is None:
                    continue
                evidence = TrustedEvidence.model_validate(result.result)
                if evidence.fingerprints == fingerprints and evidence.kind in kinds:
                    existing = accepted.get(evidence.evidence_id)
                    if existing is not None and existing != evidence:
                        message = "trusted evidence ID identifies conflicting content"
                        raise ValueError(message)
                    accepted[evidence.evidence_id] = evidence
        return tuple(accepted.values())

    async def _candidate_fingerprint(self, snapshot: str) -> ContentDigest:
        patch = await self._workspaces.export_patch(snapshot)
        return ContentDigest.sha256(patch.encode())

    async def _fingerprints(self, snapshot: str) -> EvidenceFingerprints:
        return EvidenceFingerprints(
            candidate=await self._candidate_fingerprint(snapshot),
            evaluator=self._identity.evaluator,
            workload=self._identity.workload,
            environment=self._identity.environment,
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceResultIdentity:
    """Semantic result content, including its immutable operation attribution."""

    evaluation_id: str
    outcome: EvidenceOutcome
    summary: str | None
    metrics: tuple[EvidenceMetric, ...]
    partial: PartialMeasurement | None


def evidence_identity(stage: SemanticEvaluationStage, result: EvidenceResultIdentity) -> str:
    """Return the content address of one stage's attributed semantic result.

    Operation attribution is part of the content: identical measurements from
    distinct evaluations must not share an ID with conflicting evaluation_id.
    Existing recorded IDs are read without recomputation.
    """
    identity: dict[str, JsonValue] = {
        "evaluation_id": result.evaluation_id,
        "kind": stage.kind.value,
        "fingerprints": stage.fingerprints.model_dump(mode="json"),
        "outcome": result.outcome.value,
        "summary": result.summary,
        "metrics": [item.model_dump(mode="json") for item in result.metrics],
    }
    if result.partial is not None:
        identity["partial_measurement"] = result.partial.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# Evaluation failure text is read by the agent that submitted the evaluation.
_RENDERER = TemplateRenderer(Path(__file__).with_name("prompts"))


def render_rejected_evidence(
    rejected: Sequence[tuple[str | None, EvidenceKind]],
) -> RenderedPrompt:
    """One line per rejected ``(semantic summary, kind)``: the summary, else ``<kind> failed``."""
    return _RENDERER.render_template("rejected_evidence.j2", rejected=rejected)


def render_evaluation_failure(
    record_failure: str | None, stage_failure: str | None
) -> RenderedPrompt:
    """A failed evaluation's own message, else its first stage failure, else a generic one."""
    return _RENDERER.render_template(
        "evaluation_failure.j2", record_failure=record_failure, stage_failure=stage_failure
    )


def render_stage_failure(
    rejected: Sequence[tuple[str | None, EvidenceKind]], observed_failure: str | None
) -> RenderedPrompt:
    """The failure of an evaluation whose failed stage skipped the rest.

    One line per failed check (its summary, else ``<kind> check failed``), else
    the executor's own failure, else a generic stage failure.
    """
    return _RENDERER.render_template(
        "stage_failure.j2", rejected=rejected, observed_failure=observed_failure
    )


def agent_evaluation(record: StoredEvaluation) -> AgentEvaluation:
    """Reduce one durable record to the outcome its submitting agent saw."""
    stage = SemanticEvaluationStage.model_validate(record.request.stages[0].payload)
    kinds = tuple(step.name for step in record.request.stages)
    evidence = _stage_evidence(record)
    stages = tuple(_agent_stage(item) for item in evidence)
    if record.state is EvaluationState.SUCCEEDED:
        rejected = [item for item in evidence if item.outcome is EvidenceOutcome.FAILED]
        if not rejected:
            return AgentEvaluation(
                revision=stage.snapshot,
                content_digest=stage.fingerprints.candidate.value,
                kinds=kinds,
                status=AgentEvaluationStatus.PASSED,
                stages=stages,
            )
        failure = render_rejected_evidence(
            [(item.semantic_summary, item.kind) for item in rejected]
        )
        return AgentEvaluation(
            revision=stage.snapshot,
            content_digest=stage.fingerprints.candidate.value,
            kinds=kinds,
            status=AgentEvaluationStatus.FAILED,
            stages=stages,
            failure=failure,
            signature=failure_signature(failure),
        )
    if record.state is EvaluationState.FAILED:
        stage_failure = next(
            (result.failure for result in record.stage_results if result.failure), None
        )
        failure = render_evaluation_failure(record.failure, stage_failure)
        return AgentEvaluation(
            revision=stage.snapshot,
            content_digest=stage.fingerprints.candidate.value,
            kinds=kinds,
            status=AgentEvaluationStatus.FAILED,
            stages=stages,
            failure=failure,
            signature=failure_signature(failure),
        )
    status = (
        AgentEvaluationStatus.CANCELED
        if record.state in {EvaluationState.CANCELED, EvaluationState.SUPERSEDED}
        else AgentEvaluationStatus.PENDING
    )
    return AgentEvaluation(
        revision=stage.snapshot,
        content_digest=stage.fingerprints.candidate.value,
        kinds=kinds,
        status=status,
        stages=stages,
    )


def _stage_evidence(record: StoredEvaluation) -> tuple[TrustedEvidence, ...]:
    """Return the trusted evidence of each stage that finished with a result, in stage order."""
    return tuple(
        TrustedEvidence.model_validate(result.result)
        for result in record.stage_results
        if result.state is StageState.SUCCEEDED and result.result is not None
    )


def _agent_stage(evidence: TrustedEvidence) -> AgentEvaluationStage:
    return AgentEvaluationStage(
        kind=evidence.kind.value,
        outcome=AgentEvaluationStageOutcome(evidence.outcome.value),
        metrics=tuple(
            AgentEvaluationMetric(
                name=metric.name,
                value=metric.value,
                unit=metric.unit,
                direction=(
                    MetricDirection(metric.direction) if metric.direction is not None else None
                ),
            )
            for metric in evidence.metrics
        ),
        partial_measurement=evidence.partial_measurement,
    )


_MAX_PROFILE_FOCUS_CHARS = 512


def _profile_focus(request: str) -> str:
    """Return the request's first line as the operation's exact-match work focus."""
    first = next((line.strip() for line in request.splitlines() if line.strip()), "profile")
    return first[:_MAX_PROFILE_FOCUS_CHARS].strip()


def _candidate_profile(revision: str, operation: ProfilerOperation) -> CandidateProfile:
    """Project one terminal profiler operation as the policy-facing profile outcome."""
    result = operation.result
    if operation.state is not ProfilerOperationState.COMPLETED or result is None:
        return CandidateProfile(
            revision=revision,
            status=CandidateProfileStatus.FAILED,
            operation_id=operation.operation_id,
            failure=operation.error or f"the profiler operation ended {operation.state.value}",
        )
    report = result.report
    if report.outcome is ProfilerResultOutcome.UNSUPPORTED:
        return CandidateProfile(
            revision=revision,
            status=CandidateProfileStatus.UNSUPPORTED,
            operation_id=operation.operation_id,
            diagnosis=report.unsupported_reason,
            evidence_ids=report.evidence_ids,
        )
    return CandidateProfile(
        revision=revision,
        status=CandidateProfileStatus.OBSERVED,
        operation_id=operation.operation_id,
        diagnosis=report.narrative,
        components=tuple(
            CandidateProfileComponent(name=item.name, share=item.share)
            for item in report.attribution
        ),
        evidence_ids=report.evidence_ids,
    )


_TERMINAL_EVALUATION_STATES = frozenset(
    {
        EvaluationState.SUCCEEDED,
        EvaluationState.FAILED,
        EvaluationState.CANCELED,
        EvaluationState.SUPERSEDED,
    }
)


@dataclass(frozen=True, slots=True)
class _CapturedProfile:
    """The recorded outcome of one host-run trusted profile capture."""

    evidence_id: str
    outcome: EvidenceOutcome
    summary_tail: str | None


class AgentScopes(Protocol):
    """The agent service's record and release of jobs per workspace scope."""

    def settlements(self) -> EvaluationSettlements:
        """Return observations validated against durable scope ownership."""
        ...

    async def scope_handles(self, scope_id: str | None) -> tuple[str, ...]:
        """Return the handles agents last submitted from ``scope_id``, oldest first."""
        ...

    async def cancel_scope(self, scope_id: str) -> ScopeRelease:
        """Cancel the scope's jobs and refuse its new ones; idempotent."""
        ...

    async def reopen_scope(self, scope_id: str) -> None:
        """Reconcile cleanup and open a fresh scope generation."""
        ...

    async def scope_released(self, scope_id: str | None) -> bool:
        """Return whether ``scope_id``'s jobs are released."""
        ...


_RELEASED_PROFILE_FAILURE = "the member's jobs were released, so no profile started"


class EvidenceReusingEvaluation:
    """Reuse exact accepted evidence before invoking official evaluation effects."""

    def __init__(
        self,
        delegate: Evaluation,
        backend: SemanticEvaluationBackend,
        *,
        run_id: str,
        scopes: AgentScopes,
        profiler: ProfilerAgentService | None = None,
    ) -> None:
        """Bind the official evaluator to accepted evidence from one backend.

        ``scopes`` is the agent service, which owns the record of the handles
        agents submitted from each workspace scope and their release.
        ``profiler`` is the run's profiler-agent service, when one is
        provisioned.
        """
        self._delegate = delegate
        self._backend = backend
        self._run_id = run_id
        self._scopes = scopes
        self._profiler = profiler

    async def reopen_jobs(self, member_id: str) -> None:
        """Reconcile a completed release and open a fresh generation for resumed work."""
        await self._scopes.reopen_scope(member_workspace_id(member_id))

    async def jobs_released(self, member_id: str) -> bool:
        """Project whether the member's durable scope refuses ordinary admission.

        Closing and completed releases both fence new work. Recovery can
        reconcile cleanup before opening a fresh scope generation.
        """
        return await self._scopes.scope_released(member_workspace_id(member_id))

    async def release_jobs(self, member_id: str) -> ReleasedJobs:
        """Cancel the member's jobs and refuse its new ones through the agent service.

        The agent service releases the member's workspace scope: its agents'
        evaluations and profiler operations and the profiler operations
        :meth:`profile` dispatched for it. The trusted profile capture a
        running :meth:`profile` waits on is durably owned by that same scope.
        """
        release = await self._scopes.cancel_scope(member_workspace_id(member_id))
        return ReleasedJobs(
            member_id=member_id,
            evaluations=release.evaluations,
            profiler_operations=release.profiler_operations,
            first_release=release.first_release,
        )

    async def can_profile(self) -> bool:
        """Return whether a profiler is provisioned and the executor produces profile evidence.

        The executor's availability snapshot is the one source of its supported
        evidence kinds; the agent service rejects a submission from the same set.
        """
        if self._profiler is None:
            return False
        snapshot = await self._backend.availability(ResourceRequirements())
        return EvidenceKind.PROFILE.value in snapshot.supported_evidence_kinds

    async def profile(self, revision: str, request: str, *, member_id: str) -> CandidateProfile:
        """Run one profiler operation on ``revision`` and return its typed outcome.

        The operation goes through the same profiler service as an agent's
        ``dispatch_profiler``, so it is a durable, run-observable record. An
        operation the host interrupted raises :class:`RunStopped` instead of
        returning an outcome. A profile for a member whose jobs are released
        fails typed without starting.
        """
        scope_id = member_workspace_id(member_id)
        if await self._scopes.scope_released(scope_id):
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.FAILED,
                failure=_RELEASED_PROFILE_FAILURE,
            )
        if self._profiler is None:
            return await self._delegate.profile(revision, request, member_id=member_id)
        captured = await self._trusted_capture(revision, member_id)
        if await self._scopes.scope_released(scope_id):
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.FAILED,
                failure=_RELEASED_PROFILE_FAILURE,
            )
        if captured is not None and captured.outcome is EvidenceOutcome.FAILED:
            # The trusted capture's workload did not run (for example the
            # engine fails the workload's own preflight), so no profiler turn
            # can answer the question from it. Report that without a turn.
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.UNSUPPORTED,
                diagnosis=(
                    "the trusted profile capture failed, so no profiler turn ran: "
                    f"{captured.summary_tail or 'no output'}"
                ),
                evidence_ids=(captured.evidence_id,),
            )
        try:
            dispatched = await self._profiler.dispatch(
                principal_id=member_id,
                # The member's workspace scope, so releasing the member's jobs
                # cancels this operation with its agents' ones.
                scope_id=scope_id,
                request=request,
                work=ProfilerWorkKey(
                    purpose=ProfilerWorkPurpose.PLANNING_GUIDANCE,
                    focus=_profile_focus(request),
                ),
                session_id=None,
                candidate_snapshot_id=revision,
            )
        except (ProfilerAgentUnavailableError, ProfilerAgentCapacityError, ValueError) as error:
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.FAILED,
                failure=f"the profile could not start: {error}",
            )
        if await self._scopes.scope_released(scope_id):
            # Released while the dispatch was in flight: the release may have
            # missed this operation, so cancel it; it then ends canceled.
            await self._profiler.cancel(dispatched.operation_id, member_id, scope_id)
        while True:
            reply = await self._profiler.await_result(
                dispatched.operation_id, member_id, None, MAX_AGENT_AWAIT_S
            )
            if not reply.timed_out:
                if reply.operation.state is ProfilerOperationState.INTERRUPTED:
                    # A stop or the host closing ended the operation, not the
                    # profile: it has no outcome, so a resume runs it again.
                    raise RunStopped
                return _candidate_profile(revision, reply.operation)

    async def _trusted_capture(self, revision: str, member_id: str) -> _CapturedProfile | None:
        """Run the trusted profile capture of ``revision`` before any profiler turn.

        The host waits for the capture, which costs no agent tokens; the
        profiler's own submission of the same content then joins the completed
        evaluation instead of polling a running one. Returns ``None`` when the
        executor cannot capture or the capture recorded no evidence.
        """
        if not await self.can_profile():
            return None
        submitted = await self._backend.submit_revision_evidence(
            revision, (EvidenceKind.PROFILE,), scope_id=member_workspace_id(member_id)
        )
        if await self._scopes.scope_released(member_workspace_id(member_id)):
            await self._backend.cancel(submitted.handle_id)
        while True:
            snapshot = await self._backend.operation_snapshot(submitted.handle_id)
            if snapshot.state in _TERMINAL_EVALUATION_STATES:
                break
            await self._backend.await_result(submitted.handle_id, MAX_AGENT_AWAIT_S)
        if not snapshot.stage_outcomes or not snapshot.evidence_ids:
            return None
        (outcome,) = snapshot.stage_outcomes
        return _CapturedProfile(
            evidence_id=snapshot.evidence_ids[0],
            outcome=outcome.outcome,
            summary_tail=outcome.summary_tail,
        )

    def settlements(self) -> EvaluationSettlements:
        """Expose the service's owned settlement interface to orchestration."""
        return self._scopes.settlements()

    def current_time(self) -> float:
        """Project the owning UTC clock without defining another timebase."""
        return self._delegate.current_time()

    async def wait_until(self, deadline_at_s: float) -> None:
        """Delegate the interruptible deadline suspension to the run adapter."""
        await self._delegate.wait_until(deadline_at_s)

    async def submitted_generation(self, handle_id: str) -> int:
        """Read ownership from the authoritative immutable submission."""
        return (await self._backend.recorded_snapshot(handle_id)).request.owner_generation

    async def submitted_deadline(self, handle_id: str) -> float:
        """Read the deadline captured with the immutable execution plan."""
        await self._backend.recorded_submission(handle_id)
        record = await self._backend.recorded_snapshot(handle_id)
        captures = tuple(
            SemanticEvaluationStage.model_validate(stage.payload) for stage in record.request.stages
        )
        deadline = captures[0].deadline_at_s
        unavailable = captures[0].deadline_unavailable
        if unavailable is not None:
            message = f"evaluation {handle_id!r} has no declared suspension deadline: {unavailable}"
            raise RuntimeContractError(message)
        if deadline is None or any(capture.deadline_at_s != deadline for capture in captures):
            message = f"evaluation {handle_id!r} has no consistent submitted deadline"
            raise RuntimeContractError(message)
        return deadline

    async def cancel_submitted(self, handle_id: str) -> None:
        """Cancel an identified immutable submission without dispatching work."""
        await self._backend.recorded_submission(handle_id)
        await self._backend.cancel(handle_id)

    async def accepted_evidence_ids(self, handle_id: str) -> tuple[str, ...]:
        """Project backend-accepted evidence without attributing later WIP to it."""
        return (await self._backend.operation_snapshot(handle_id)).evidence_ids

    async def submitted_report(self, handle_id: str) -> str:
        """Read validated historical identity and its canonical terminal report."""
        await self._backend.recorded_submission(handle_id)
        return (await self._backend.recorded_snapshot(handle_id)).model_dump_json()

    async def submitted_revision(self, handle_id: str) -> str:
        """Read the exact immutable submission, never the later retained WIP."""
        # The backend validates the complete stage identity before projecting
        # its shared capture revision.
        await self._backend.recorded_submission(handle_id)
        record = await self._backend.recorded_snapshot(handle_id)
        return SemanticEvaluationStage.model_validate(record.request.stages[0].payload).snapshot

    async def agent_evaluations(self, workspace: Workspace) -> tuple[AgentEvaluation, ...]:
        """Return the outcomes of evaluations agents submitted from ``workspace``."""
        return await self._backend.agent_evaluations(await self._scopes.scope_handles(workspace.id))

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Return exact accepted accuracy evidence, or execute through the delegate."""
        if reuse is not None:
            return await self._delegate.accuracy(workspace, reuse=reuse)
        evidence = await self._backend.evidence_for(workspace, (EvidenceKind.ACCURACY,))
        if not evidence:
            return await self._delegate.accuracy(workspace)
        accepted = evidence[-1]
        revision = workspace.revision
        if revision is None:
            return await self._delegate.accuracy(workspace)
        feedback = (
            None
            if accepted.outcome is EvidenceOutcome.PASSED
            else accepted.semantic_summary or "Framework accuracy gate failed."
        )
        return AccuracyEvaluation(
            executed=False,
            feedback=feedback,
            receipt=(
                AccuracyReceipt(
                    run_id=self._run_id,
                    workspace_id=workspace.id,
                    revision=revision,
                )
                if feedback is None
                else None
            ),
        )

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        """Return exact accepted benchmark evidence, or execute through the delegate."""
        evidence = await self._backend.evidence_for(workspace, (EvidenceKind.BENCHMARK,))
        if not evidence:
            return await self._delegate.benchmark(workspace, objectives=objectives)
        accepted = evidence[-1]
        row = {metric.name: metric.value for metric in accepted.metrics}
        if objectives and objectives[0].name not in row:
            return await self._delegate.benchmark(workspace, objectives=objectives)
        headline = objectives[0].name if objectives else next(iter(row), None)
        metric = next((item for item in accepted.metrics if item.name == headline), None)
        feedback = (
            None
            if accepted.outcome is EvidenceOutcome.PASSED
            else accepted.semantic_summary or "Framework benchmark failed."
        )
        direction = (
            objectives[0].direction
            if objectives
            else MetricDirection(metric.direction)
            if metric is not None and metric.direction is not None
            else None
        )
        return BenchmarkEvaluation(
            executed=False,
            feedback=feedback,
            metric_name=headline,
            metric_value=metric.value if metric is not None else None,
            metric_direction=direction,
            metric_unit=metric.unit if metric is not None else None,
            row=row or None,
            partial_measurement=accepted.partial_measurement if feedback is not None else None,
        )

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Delegate candidate-authored local validation unchanged."""
        return await self._delegate.validate_local(
            workspace,
            recipe_artifact=recipe_artifact,
            report_location=report_location,
        )


__all__ = [
    "AgentScopes",
    "EvidenceResultIdentity",
    "EvidenceReusingEvaluation",
    "SemanticEvaluationBackend",
    "SemanticEvaluationExecutor",
    "SemanticEvaluationIdentity",
    "SemanticEvaluationStage",
    "agent_evaluation",
    "evidence_identity",
]
