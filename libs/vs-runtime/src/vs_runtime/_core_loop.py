"""Serial durable shell. Core owns policy; this module owns commit-before-I/O."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ContractError,
    CoreEvent,
    CoreState,
    DispatchAuthorized,
    EventCursor,
    HostFence,
    HostId,
    Intent,
    IntentPhase,
    KernelNotImplementedError,
    Observation,
    OperationRegistry,
    ProposalSubmitted,
    RecoveryPhase,
    RecoveryStarted,
    RequestId,
    RunEnvelope,
    Strategy,
    StrategyState,
    Transition,
    project,
    step,
    validate_startup,
)
from vs_project.api import Committed, StateStore, StoredEnvelope, StoreFence, Unknown
from vs_runtime._core_preflight import resolve_core_resume
from vs_runtime._core_record import (
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    RuntimeRecord,
)
from vs_runtime._core_requests import (
    ExecutionContext,
    ExecutorRefusal,
    OwnerEvent,
    RequestExecutors,
)

if TYPE_CHECKING:
    from vs_core.api import Request
    from vs_project.api import Project


class CoreTransitions(Protocol):
    """Pure transition seam; production always uses vs_core.api.step.

    Public core trace fixtures may supply transitions while sibling leaves land.
    No execution, storage, strategy callbacks or mutable lifecycle state here.
    """

    def step(self, state: CoreState, event: CoreEvent) -> Transition: ...


class ProductionCoreTransitions:
    """Default binding, with no lifecycle fallback when a leaf is missing."""

    def step(self, state: CoreState, event: CoreEvent) -> Transition:
        """Delegate every input to the canonical pure state machine."""
        try:
            return step(state, event)
        except KernelNotImplementedError as error:
            raise CoreContractGapError(error) from error


class RuntimeCommitError(RuntimeError):
    """Commit failed or lost authority; this shell cannot dispatch again."""


class CoreContractGapError(RuntimeCommitError):
    """An explicitly missing core leaf. No transition or I/O is authorized."""

    def __init__(self, error: KernelNotImplementedError) -> None:
        self.area = error.area
        self.event_kind = error.event_kind
        self.subarea = error.subarea
        super().__init__(f"core contract gap: {error}; owning core lane must supply this leaf")


class RuntimeCommitUncertainError(RuntimeCommitError):
    """Unknown commit was reloaded and compared, but grants no I/O authority."""

    def __init__(self, *, candidate_visible: bool) -> None:
        self.candidate_visible = candidate_visible
        super().__init__(
            f"unknown runtime commit; candidate_visible={candidate_visible}; restart required"
        )


class RuntimeExecutionError(RuntimeCommitError):
    """An authorized request ended without a usable observation; the shell halted.

    The intent stays DISPATCHED. Core never receives an invented outcome: the
    executor may have performed the effect, so only a new epoch's inspection
    may classify it.
    """

    def __init__(self, request_id: RequestId, detail: str) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id.root} halted the runtime: {detail}")


class ObservationRejectedError(RuntimeExecutionError):
    """Core rejected an executor's observation after the effect ran; the shell halted.

    Dropping the observation would leave the intent DISPATCHED with the result
    lost, so a retry could never converge. Executors build observations through
    ``ObservationFactory``, so a rejection here is a defect to surface, not a
    condition to absorb.
    """

    def __init__(self, request_id: RequestId, rejection: ContractError) -> None:
        self.path = rejection.path
        self.rejection = rejection.detail
        super().__init__(
            request_id,
            f"core rejected the observation at {'.'.join(map(str, self.path))}: {self.rejection}",
        )


class DispatchProgress(StrEnum):
    """Distinguish no eligible request from a completed executor call."""

    IDLE = "idle"
    DISPATCHED = "dispatched"


class PublicationDelivery(Protocol):
    """Durable publish deduplicates stable IDs and rejects payload conflicts.

    A typed acknowledgement proves durable publication. An exception
    leaves the outbox pending for reconciliation with the same identity.
    """

    async def publish(
        self, publication: Publication, context: PublicationContext
    ) -> PublicationAcknowledgement: ...


class _Input[S: StrategyState](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    event: CoreEvent
    now_at: float = Field(ge=0, allow_inf_nan=False)
    proposed_state: S | None = None
    # Executor owner events to commit with this observation, then apply in order.
    owner_events: tuple[OwnerEvent, ...] = ()
    # True for an event that is already in the durable pending_inputs outbox.
    durable: bool = False
    # The request whose executor produced this input; its rejection halts the shell.
    executed: RequestId | None = None


@dataclass(frozen=True)
class _ShellLease:
    """Process-local lease handle given to executors and publication delivery."""

    shell: CoreRuntime[Any]

    def renew(self, *, now_at: float, lease_duration: float) -> None:
        self.shell.renew(now_at=now_at, lease_duration=lease_duration)

    def verify(self, *, now_at: float) -> bool:
        return self.shell.holds_lease(now_at=now_at)


@dataclass(frozen=True)
class _Decide:
    now_at: float


@dataclass(frozen=True)
class CoreRuntimeBindings:
    """Closed composition choices, separate from durable shell state."""

    registry: OperationRegistry = field(default_factory=OperationRegistry)
    executors: RequestExecutors = field(default_factory=RequestExecutors)
    transitions: CoreTransitions = field(default_factory=ProductionCoreTransitions)


class CoreRuntime[S: StrategyState]:
    """One serial input queue and one fenced durable state authority.

    Call start before admission. submit queues controls, durable occurrences,
    supplied clock/deadline events or observations. advance commits one input;
    dispatch_one authorizes and executes at most one prepared request. Crashes
    between these public boundaries recover through core's new-epoch barrier.
    This shell promises logical deduplication through canonical identities,
    never exactly-once external execution without executor deduplication.
    """

    def __init__(
        self,
        store: StateStore,
        strategy: Strategy[S],
        initial: CoreState,
        *,
        bindings: CoreRuntimeBindings | None = None,
    ) -> None:
        self._store = store
        self._strategy = strategy
        self._initial = initial
        selected = bindings or CoreRuntimeBindings()
        self._registry = selected.registry
        self._executors = selected.executors
        self._transitions = selected.transitions
        self._record_model = cast(
            "type[RuntimeRecord[S]]", RuntimeRecord.__class_getitem__(type(strategy.state))
        )
        self._envelope_model = cast(
            "type[RunEnvelope[S]]", RunEnvelope.__class_getitem__(type(strategy.state))
        )
        self._record: RuntimeRecord[S] | None = None
        self._storage_revision: int | None = None
        self._fence: StoreFence | None = None
        self._halted = False
        self._busy = False
        # Latest time a lease renewal or check supplied; commits never use an earlier time.
        self._time_floor = 0.0
        self._queue: deque[_Input[S] | _Decide] = deque()

    @classmethod
    def resume(
        cls,
        project: Project,
        strategy: Strategy[S],
        *,
        run_id: str | None = None,
        bindings: CoreRuntimeBindings | None = None,
    ) -> CoreRuntime[S]:
        """Read-only preflight before starting the new-envelope recovery shell.

        Executor implementations must defer setup to committed requests. This
        factory resolves the selected Project run before invoking any executor.
        It never converts legacy identities or starts a fresh replacement run.
        """
        selected = bindings or CoreRuntimeBindings()
        resolved = resolve_core_resume(project, strategy, run_id=run_id, registry=selected.registry)
        return cls(resolved.store, strategy, resolved.record.envelope.core, bindings=selected)

    @property
    def record(self) -> RuntimeRecord[S]:
        """Last confirmed or reconciled whole record, never a staged transition."""
        if self._record is None:
            message = "runtime not started"
            raise RuntimeError(message)
        return self._record

    @property
    def storage_revision(self) -> int | None:
        """CAS revision, independent of record.envelope.core.revision."""
        return self._storage_revision

    def _decode(self, stored: StoredEnvelope) -> RuntimeRecord[S]:
        record = self._record_model.decode(stored, self._registry)
        envelope = record.envelope
        if envelope.core.run.declaration != self._strategy.declaration:
            raise ContractError(
                ("declaration",), "offered strategy differs from durable declaration"
            )
        if envelope.core.run.run_id != self._initial.run.run_id:
            raise ContractError(("run_id",), "selected run differs from durable envelope")
        selected = validate_startup(self._strategy.declaration, envelope.core.run.capabilities)
        if selected.operations != envelope.core.registry:
            raise ContractError(("registry",), "durable registry differs from selected declaration")
        return record.model_copy(update={"envelope": envelope})

    def _load(self) -> StoredEnvelope | None:
        stored = self._store.load()
        if stored is None:
            self._record = None
            self._storage_revision = None
            return None
        if not isinstance(stored, StoredEnvelope):
            raise ContractError(("runtime",), "quarantined record cannot run")
        self._record = self._decode(stored)
        self._storage_revision = stored.revision
        return stored

    def start(self, host_id: str, *, now_at: float, lease_duration: float) -> None:
        """Validate before lease acquisition, then commit new-epoch recovery.

        No backend setup or execution occurs here. Unfinished identities produce
        inspection requests; core gates ordinary admission until reconciled.
        """
        if self._fence is not None:
            message = "runtime already started"
            raise RuntimeError(message)
        self._load()
        core = self._initial if self._record is None else self.record.envelope.core
        if core.run.declaration != self._strategy.declaration:
            raise ContractError(("declaration",), "initial strategy declaration mismatch")
        selected = validate_startup(self._strategy.declaration, core.run.capabilities)
        if selected.operations != core.registry:
            raise ContractError(("registry",), "selected operations differ from durable registry")
        self._registry.validate_core(core)
        strategy_state = (
            self._strategy.state if self._record is None else self.record.envelope.strategy
        )
        provisional = RunEnvelope[S](
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root=host_id), epoch=0),
            strategy_id=self._strategy.declaration.strategy_id,
            state_schema=self._strategy.declaration.state_schema,
            core=core,
            strategy=strategy_state,
            event_cursor=EventCursor(sequence=0)
            if self._record is None
            else self.record.envelope.event_cursor,
        )
        self._registry.decode_envelope(
            self._envelope_model, self._registry.encode_envelope(provisional)
        )
        fence = self._store.acquire(host_id, now=now_at, duration=lease_duration)
        if fence is None:
            message = "runtime lease unavailable"
            raise RuntimeCommitError(message)
        self._fence = fence
        provisional = provisional.model_copy(
            update={"fence": HostFence(host_id=HostId(root=fence.host_id), epoch=fence.epoch)}
        )
        previous_record = self._record
        previous_revision = self._storage_revision
        self._record = (
            RuntimeRecord[S].fresh(provisional)
            if self._record is None
            else self.record.model_copy(update={"envelope": provisional})
        )
        try:
            self._consume(
                _Input[S](event=RecoveryStarted(epoch=fence.epoch, now_at=now_at), now_at=now_at)
            )
        except BaseException:
            self._halted = True
            if self._storage_revision == previous_revision:
                self._record = previous_record
            raise
        # Owner events committed with an observation before a crash resume here.
        self._queue.extend(
            _Input[S](event=event, now_at=now_at, durable=True)
            for event in self.record.pending_inputs
        )

    def renew(self, *, now_at: float, lease_duration: float) -> StoreFence:
        """Renew host authority without changing core or storage CAS revision.

        Ambiguous renewal halts this shell. A new host must reconcile at startup;
        the old token never grants authority merely because renewal returned.
        """
        # Renewal changes only lease authority, so it stays legal while an executor
        # or publication is in flight; a long request must not lose the lease.
        if self._halted or self._fence is None:
            message = "runtime inactive, busy or commit unconfirmed"
            raise RuntimeCommitError(message)
        self._time_floor = max(self._time_floor, now_at)
        try:
            renewed = self._store.renew(self._fence, now=now_at, duration=lease_duration)
        except OSError:
            self._halted = True
            raise
        if renewed is None:
            self._halted = True
            message = "runtime lease renewal rejected"
            raise RuntimeCommitError(message)
        self._fence = renewed
        return renewed

    def holds_lease(self, *, now_at: float) -> bool:
        """True while this shell is active and the store still honors its fence."""
        if self._halted or self._fence is None:
            return False
        self._time_floor = max(self._time_floor, now_at)
        return self._store.verify(self._fence, now=now_at)

    def submit(self, event: CoreEvent, *, now_at: float) -> None:
        """Queue validated input. Redeliver durable occurrences after precommit crashes."""
        self._require_active(queue_only=True)
        self._queue.append(_Input[S](event=event, now_at=now_at))

    def decide(self, *, now_at: float) -> None:
        """Queue a strategy call against the revision observed when it is consumed."""
        self._require_active(queue_only=True)
        # Reuse the supplied-time boundary before queueing the command.
        _Input[S](event=ProposalSubmitted(decisions=(), expected_revision=0), now_at=now_at)
        self._queue.append(_Decide(now_at))

    def advance(self) -> bool:
        """Commit the next queued input and callback state, with no I/O dispatch.

        The input leaves the queue before it is stepped. A rejected input
        (ContractError) is dropped without halting: nothing was committed, and
        submitters redeliver durable occurrences after a rejection or crash.
        The exception is an executor's observation: its effect already ran and
        nobody redelivers it, so a rejection halts the shell with
        ``ObservationRejectedError`` instead of losing the result.
        """
        self._require_active()
        if not self._queue:
            return False
        item = self._queue.popleft()
        if isinstance(item, _Decide):
            if self.record.envelope.core.intents.recovery.phase != RecoveryPhase.READY:
                raise ContractError(("recovery",), "strategy proposals require ready recovery")
            proposal = self._strategy.bind(self.record.envelope.strategy).decide(
                project(self.record.envelope.core)
            )
            item = _Input[S](
                event=ProposalSubmitted(
                    decisions=tuple(
                        self._registry.validate_decision(decision)
                        for decision in proposal.decisions
                    ),
                    expected_revision=self.record.envelope.revision,
                ),
                proposed_state=proposal.state,
                now_at=item.now_at,
            )
        try:
            self._consume(item)
        except ContractError as error:
            if item.executed is None:
                raise
            self._halted = True
            raise ObservationRejectedError(item.executed, error) from error
        return True

    def _consume(self, item: _Input[S], transition: Transition | None = None) -> None:
        envelope = self.record.envelope
        transition = transition or self._transitions.step(envelope.core, item.event)
        state = envelope.strategy if item.proposed_state is None else item.proposed_state
        view = project(transition.state)
        for event in transition.events:
            state = self._strategy.bind(state).on_event(view, event)
        pending = self.record.pending_inputs
        if item.durable:
            index = pending.index(item.event)  # type: ignore[arg-type]
            pending = (*pending[:index], *pending[index + 1 :])
        publications = tuple(
            Publication(
                publication_id=f"{transition.state.run.run_id.root}:{sequence}",
                sequence=sequence,
                event=event,
            )
            for sequence, event in enumerate(
                transition.events, start=self.record.next_publication_sequence
            )
        )
        candidate = self.record.model_copy(
            update={
                "envelope": envelope.model_copy(
                    update={
                        "core": transition.state,
                        "strategy": state,
                        "event_cursor": EventCursor(
                            sequence=envelope.event_cursor.sequence + len(transition.events)
                        ),
                    }
                ),
                "pending_publications": (*self.record.pending_publications, *publications),
                "next_publication_sequence": self.record.next_publication_sequence
                + len(publications),
                "pending_inputs": (*pending, *item.owner_events),
            }
        )
        self._commit(candidate, item.now_at)
        self._queue.extend(
            _Input[S](event=event, now_at=item.now_at, durable=True) for event in item.owner_events
        )

    def _commit(self, candidate: RuntimeRecord[S], now_at: float) -> None:
        self._registry.encode_envelope(candidate.envelope)
        stored = StoredEnvelope(
            revision=0 if self._storage_revision is None else self._storage_revision + 1,
            schema_version=1,
            payload=candidate.model_dump_json().encode(),
        )
        # Decode our wire at the boundary too; model_copy deliberately skips validation.
        validated = self._decode(stored)
        if self._fence is None:
            message = "runtime has no lease"
            raise RuntimeCommitError(message)
        try:
            result = self._store.commit(
                self._storage_revision, stored, self._fence, now=max(now_at, self._time_floor)
            )
        except OSError:
            self._halted = True
            raise
        if isinstance(result, Committed):
            if result.record != stored:
                self._halted = True
                message = "store acknowledged a different runtime record"
                raise RuntimeCommitError(message)
            self._record = validated
            self._storage_revision = stored.revision
            return
        self._halted = True
        reloaded = self._load()

        if isinstance(result, Unknown):
            raise RuntimeCommitUncertainError(candidate_visible=reloaded == stored)
        message = f"runtime commit conflict: {result.reason}"
        raise RuntimeCommitError(message)

    def _require_active(self, *, queue_only: bool = False) -> None:
        if self._halted or self._fence is None or (self._busy and not queue_only):
            message = "runtime inactive, busy or commit unconfirmed"
            raise RuntimeCommitError(message)

    def _authorized(self) -> tuple[Intent, Transition] | None:
        """Ask core for dispatch authority; blocked prerequisites remain pending."""
        core = self.record.envelope.core
        for intent in core.intents.intents:
            if intent.phase != IntentPhase.PREPARED:
                continue
            try:
                transition = self._transitions.step(
                    core, DispatchAuthorized(request_id=intent.request_id)
                )
            except ContractError as error:
                if error.path not in (("dependency",), ("recovery",)):
                    raise
                continue
            return intent, transition
        return None

    async def dispatch_one(self, *, now_at: float) -> ExecutorRefusal | DispatchProgress:
        """Persist authorization before I/O. Already dispatched work needs recovery.

        A role without a bound executor is refused before anything is committed,
        so the intent stays PREPARED. Once authorization is durable, an executor
        exception, refusal or unusable result halts the shell (the intent stays
        DISPATCHED for the next epoch's inspection) and is never reported as idle.
        """
        self._require_active()
        if self._queue:
            message = "consume queued inputs before dispatch"
            raise RuntimeError(message)
        authorized = self._authorized()
        if authorized is None:
            return DispatchProgress.IDLE
        intent, transition = authorized
        unbound = self._executors.refusal(intent.request)
        if unbound is not None:
            return unbound
        self._consume(
            _Input[S](event=DispatchAuthorized(request_id=intent.request_id), now_at=now_at),
            transition,
        )
        if self._fence is None or not self._store.verify(self._fence, now=now_at):
            self._halted = True
            message = "runtime fence lost before execution"
            raise RuntimeCommitError(message)
        authorized_intent = next(
            (
                row
                for row in self.record.envelope.core.intents.intents
                if row.request_id == intent.request_id
            ),
            None,
        )
        if (
            authorized_intent is None
            or authorized_intent.phase != IntentPhase.DISPATCHED
            or authorized_intent.request != intent.request
            or authorized_intent.payload_digest != intent.payload_digest
        ):
            raise ContractError(
                ("dispatch_authorized",), "exact canonical request must be durably authorized"
            )
        context = ExecutionContext(
            fence=self.record.envelope.fence,
            now_at=now_at,
            payload_digest=intent.payload_digest,
            lease=_ShellLease(self),
        )
        self._busy = True
        try:
            outcome = await self._executors.dispatch(intent.request, context)
        except BaseException:
            self._halted = True
            raise
        finally:
            self._busy = False
        if isinstance(outcome, ExecutorRefusal):
            self._halted = True
            raise RuntimeExecutionError(
                intent.request_id, f"executor refused after authorization: {outcome.detail}"
            )
        try:
            self._validate_observation(intent.request, outcome.observation.observation)
            observed = self._registry.validate_event(outcome.observation)
            for event in outcome.owner_events:
                self._validate_owner_event(intent.request, event)
        except ContractError:
            self._halted = True
            raise
        self._queue.append(
            _Input[S](
                event=observed,
                now_at=now_at,
                owner_events=outcome.owner_events,
                executed=intent.request_id,
            )
        )
        return DispatchProgress.DISPATCHED

    @staticmethod
    def _validate_observation(request: Request, observation: Observation) -> None:
        if (
            observation.request_id != request.request_id
            or observation.scope != request.scope
            or observation.admission_id != request.admission_id
        ):
            raise ContractError(
                ("observation",), "request, scope or admission differs from execution"
            )

    @staticmethod
    def _validate_owner_event(request: Request, event: OwnerEvent) -> None:
        """Owner events may only speak for the executed request's scope and admission."""
        scope = getattr(event, "scope", None)
        admission_id = getattr(event, "admission_id", request.admission_id)
        observation = getattr(event, "observation", None)
        if (
            (scope is not None and scope != request.scope)
            or admission_id != request.admission_id
            or (
                isinstance(observation, Observation)
                and (
                    observation.scope != request.scope
                    or observation.admission_id != request.admission_id
                )
            )
        ):
            raise ContractError(
                ("owner event", event.kind),
                "owner event scope or admission differs from the executed request",
            )

    async def publish_one(self, delivery: PublicationDelivery, *, now_at: float) -> bool:
        """Append with stable ID, then acknowledge using only a storage revision."""
        self._require_active()
        if not self.record.pending_publications:
            return False
        if self._fence is None or not self._store.verify(self._fence, now=now_at):
            self._halted = True
            message = "runtime fence lost before publication"
            raise RuntimeCommitError(message)
        publication = self.record.pending_publications[0]
        self._busy = True
        try:
            await delivery.publish(
                publication,
                PublicationContext(fence=self._fence, now_at=now_at, lease=_ShellLease(self)),
            )
            self._commit(
                self.record.model_copy(
                    update={
                        "pending_publications": self.record.pending_publications[1:],
                        "delivery_cursor": publication.sequence,
                    }
                ),
                now_at,
            )
        finally:
            self._busy = False
        return True

    async def run_until_idle(
        self, delivery: PublicationDelivery, *, now_at: float
    ) -> ExecutorRefusal | None:
        """Drive the serialized queue, prepared intents and committed publication outbox.

        Publication is diagnostic, so a failing delivery never blocks dispatch
        (cancellations must still go out). Publishing stops after the first
        failure, dispatch continues to idle, and that failure is then re-raised.
        """
        publication_error: OSError | ContractError | None = None
        while True:
            if self.advance():
                continue
            if publication_error is None:
                try:
                    if await self.publish_one(delivery, now_at=now_at):
                        continue
                except (OSError, ContractError) as error:
                    publication_error = error
            outcome = await self.dispatch_one(now_at=now_at)
            if isinstance(outcome, ExecutorRefusal):
                return outcome
            if outcome == DispatchProgress.IDLE:
                if publication_error is not None:
                    raise publication_error
                return None
