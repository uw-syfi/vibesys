"""Durable shell. Core owns policy; this module owns commit-before-I/O and the single writer."""

from __future__ import annotations

import asyncio
import contextlib
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
    TurnObserved,
    orphan_waits,
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
    counts_toward_concurrency,
    settles_through_core,
)
from vs_runtime._evaluation_jobs import awaits_submissions

if TYPE_CHECKING:
    from vs_core.api import Request, Wait
    from vs_project.api import Project
    from vs_runtime._core_requests import ExecutionOutcome


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


class AdmissionBusyError(RuntimeCommitError):
    """An admission arrived while a commit was in flight; the caller may try again.

    The one transient admission refusal. Nothing was queued and the shell is intact,
    unlike every other ``RuntimeCommitError``.
    """

    def __init__(self) -> None:
        super().__init__("admission needs an idle input queue")


class LeaseUnavailableError(RuntimeCommitError):
    """Another host holds the run's lease, or the clock is behind the store's last mutation."""


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


class OwnerEventRejectedError(RuntimeCommitError):
    """Core rejected an owner event an executor committed durably; the shell halted.

    The owner event is already in the outbox and the effect that produced it
    already ran, so dropping it would lose the fact, and replaying it after a
    restart would hit the same rejection forever. It is a defect to surface.
    """

    def __init__(self, event: CoreEvent, rejection: ContractError) -> None:
        self.event_kind = event.kind
        self.path = rejection.path
        self.rejection = rejection.detail
        super().__init__(
            f"core rejected owner event {self.event_kind} at "
            f"{'.'.join(map(str, self.path))}: {self.rejection}"
        )


class OrphanWaitError(RuntimeCommitError):
    """A wait in core state that nothing will end: no request in flight, no event in the outbox.

    The run would sit until its deadline. The shell fails at the commit that created the wait
    (or at start, for a restored one), naming each waiter, instead.
    """

    def __init__(self, orphans: tuple[Wait, ...]) -> None:
        self.orphans = orphans
        named = ", ".join(f"{wait.waiter.value} {wait.subject}" for wait in orphans)
        super().__init__(f"orphan waits, nothing will end them: {named}")


class DispatchCapExceededError(RuntimeCommitError):
    """``run_until_idle`` executed more requests than its cap: a request cycle that never ends.

    ``kinds`` counts the request kinds dispatched, so the cycle is named in the failure.
    """

    def __init__(self, cap: int, kinds: dict[str, int]) -> None:
        self.cap = cap
        self.kinds = dict(kinds)
        super().__init__(f"more than {cap} dispatches without going idle; kinds seen: {kinds}")


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


class CommitObserver(Protocol):
    """Hears each runtime record the store confirmed, for display and projections only.

    It runs after the commit is durable and on the loop's thread, and it must not raise.
    ``previous`` is the record this process held before the commit: the last durable one,
    the fresh record of a run that had none, or None when it held nothing.
    """

    def committed(self, previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]) -> object:
        """One commit became durable."""
        ...


class IgnoreCommits:
    """The observer of a host that shows nothing about commits."""

    def committed(self, previous: RuntimeRecord[Any] | None, current: RuntimeRecord[Any]) -> None:
        """Drop the report."""
        del previous, current


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


def _task_failure(task: asyncio.Task[Any]) -> BaseException | None:
    """What a finished task raised, without raising it."""
    return asyncio.CancelledError() if task.cancelled() else task.exception()


@dataclass(frozen=True)
class _Decide:
    now_at: float


@dataclass(frozen=True)
class _Flight:
    """A request that is durably authorized and executing; only the loop completes it."""

    order: int
    """Authorization index within the shell: the tie-break between same-tick completions."""
    intent: Intent
    now_at: float
    task: asyncio.Task[ExecutionOutcome]


@dataclass(frozen=True)
class CoreRuntimeBindings:
    """Closed composition choices, separate from durable shell state."""

    registry: OperationRegistry = field(default_factory=OperationRegistry)
    executors: RequestExecutors = field(default_factory=RequestExecutors)
    transitions: CoreTransitions = field(default_factory=ProductionCoreTransitions)
    commits: CommitObserver = field(default_factory=IgnoreCommits)
    check_liveness: bool = False
    """Check ``orphan_waits`` after every commit, not only at start. Linear in state size."""


class CoreRuntime[S: StrategyState]:
    """One input queue, one writer and one fenced durable state authority.

    Call start before admission. submit queues controls, durable occurrences,
    supplied clock/deadline events or observations. advance commits one input;
    dispatch_one authorizes and executes at most one prepared request, and
    run_until_idle may keep up to ``max_concurrent`` authorized requests executing.
    Executors never touch the record: only the calling task authorizes, commits and
    queues observations, so commits never interleave. Crashes between these public
    boundaries recover through core's new-epoch barrier.
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
        self._commits = selected.commits
        self._check_liveness = selected.check_liveness
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
        self._strategy_wake_at: float | None = None
        self._busy = False
        # Authorized requests whose executors are still running. They outlive the calls
        # that start them, so the run loop can decide, tick the clock and read controls
        # while they run; only `settle` completes them.
        self._flights: list[_Flight] = []
        self._dispatched = 0
        self._authorizations = 0
        self._last_kind: str | None = None
        self._kinds: dict[str, int] = {}
        # A delivery failure waits here, with publishing paused, until nothing is running.
        self._publication_failure: OSError | ContractError | None = None
        # Latest time a lease renewal or check supplied; commits never use an earlier time.
        self._time_floor = 0.0
        self._queue: deque[_Input[S] | _Decide] = deque()
        # Durable turn ends whose wait names a job its submission has not made core's yet.
        self._held: list[_Input[S]] = []
        # State after the admitted inputs still queued, valid while core stays at one revision
        # (a publication or lease commit changes the storage revision, not core).
        self._tail: tuple[int, CoreState] | None = None
        # The (run, registry) values `_check_identity` last accepted. Both are frozen, so
        # the same objects always give the same verdict.
        self._identity_verified: tuple[object, object] | None = None

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
    def dispatched(self) -> int:
        """Requests this shell has executed so far (not durable; counts from process start)."""
        return self._dispatched

    @property
    def storage_revision(self) -> int | None:
        """CAS revision, independent of record.envelope.core.revision."""
        return self._storage_revision

    def _decode(self, stored: StoredEnvelope) -> RuntimeRecord[S]:
        record = self._record_model.decode(stored, self._registry)
        self._check_identity(record.envelope)
        return record.model_copy(update={"envelope": record.envelope})

    def _check_identity(self, envelope: RunEnvelope[S]) -> None:
        """The envelope belongs to this run, strategy and registry (no decoding)."""
        verified = self._identity_verified
        if (
            verified is not None
            and verified[0] is envelope.core.run
            and verified[1] is envelope.core.registry
        ):
            return
        if envelope.core.run.declaration != self._strategy.declaration:
            raise ContractError(
                ("declaration",), "offered strategy differs from durable declaration"
            )
        if envelope.core.run.run_id != self._initial.run.run_id:
            raise ContractError(("run_id",), "selected run differs from durable envelope")
        selected = validate_startup(self._strategy.declaration, envelope.core.run.capabilities)
        if selected.operations != envelope.core.registry:
            raise ContractError(("registry",), "durable registry differs from selected declaration")
        self._identity_verified = (envelope.core.run, envelope.core.registry)

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
            raise LeaseUnavailableError(message)
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
            self._require_no_orphan_waits()
        except BaseException:
            self._halted = True
            if self._storage_revision == previous_revision:
                self._record = previous_record
            raise
        # Owner events committed with an observation before a crash resume here.
        self._held.clear()
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
        # Like every commit and check, renewal never stamps earlier than the last commit:
        # the store rejects a stamp below its watermark, which the commit raised.
        self._time_floor = max(self._time_floor, now_at)
        try:
            renewed = self._store.renew(self._fence, now=self._time_floor, duration=lease_duration)
        except OSError:
            self._halted = True
            raise
        if renewed is None:
            self._halted = True
            message = "runtime lease renewal rejected"
            raise RuntimeCommitError(message)
        self._fence = renewed
        return renewed

    def release_lease(self, *, now_at: float) -> None:
        """Give the lease back so a restart need not wait for it to expire.

        Call when the loop ends, cleanly or not. A shell that never started or already
        lost its lease does nothing; the store releases only a lease this fence holds.
        """
        if self._fence is None:
            return
        self._time_floor = max(self._time_floor, now_at)
        self._halted = True
        # Any failure to release, not only an OSError (a corrupt lease document fails
        # validation), only makes the next host wait for the lease to expire. This runs
        # in a ``finally``, so letting it raise would replace the run's own exception.
        with contextlib.suppress(Exception):
            self._store.release(self._fence, now=self._time_floor)

    def holds_lease(self, *, now_at: float) -> bool:
        """True while this shell is active and the store still honors its fence."""
        if self._halted or self._fence is None:
            return False
        self._time_floor = max(self._time_floor, now_at)
        return self._store.verify(self._fence, now=max(now_at, self._time_floor))

    def submit(self, event: CoreEvent, *, now_at: float) -> None:
        """Queue validated input. Redeliver durable occurrences after precommit crashes."""
        self._require_active(queue_only=True)
        self._queue.append(_Input[S](event=event, now_at=now_at))

    def admit(self, event: CoreEvent, *, now_at: float) -> Transition:
        """Queue an input and return the transition core will commit for it.

        An agent tool call arrives while a turn holds the shell, so its answer cannot
        wait for the next commit. Core's step is pure: this runs it against the state
        after every admitted input still queued and returns the result, which the
        later commit reproduces because nothing else commits during a turn. A rejected
        event raises ``ContractError`` and queues nothing. Admission needs an idle
        queue, or only admitted inputs in it, and says so loudly otherwise.
        """
        self._require_active(queue_only=True)
        tail = self._tail
        if tail is not None and tail[0] == self.record.envelope.core.revision:
            base = tail[1]
        elif self._queue:
            raise AdmissionBusyError
        else:
            base = self.record.envelope.core
        transition = self._transitions.step(base, event)
        self._queue.append(_Input[S](event=event, now_at=now_at))
        self._tail = (self.record.envelope.core.revision, transition.state)
        return transition

    @property
    def admitted_core(self) -> CoreState:
        """Core's state after the admitted inputs still queued, or the committed state.

        An agent's tool call is admitted while the loop keeps committing, so what the call
        asked for (a submission, which owns a job) may not be committed yet. A caller that
        asks core whether a later call is valid asks this state.
        """
        core = self.record.envelope.core
        tail = self._tail
        return tail[1] if tail is not None and tail[0] == core.revision else core

    @property
    def strategy_wake_at(self) -> float | None:
        """The time the strategy's latest proposal waits for, or None when it waits for none."""
        return self._strategy_wake_at

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
        The exceptions are an executor's observation and the owner events committed
        with it: the effect already ran and nobody redelivers them, so a rejection
        halts the shell with ``ObservationRejectedError`` or
        ``OwnerEventRejectedError`` instead of losing the result.
        """
        self._require_active()
        if not self._queue:
            return False
        item = self._queue.popleft()
        if isinstance(item, _Input) and self._awaits_submissions(item):
            self._held.append(item)
            return True
        if isinstance(item, _Decide):
            if self.record.envelope.core.intents.recovery.phase != RecoveryPhase.READY:
                raise ContractError(("recovery",), "strategy proposals require ready recovery")
            proposal = self._strategy.bind(self.record.envelope.strategy).decide(
                project(self.record.envelope.core)
            )
            self._strategy_wake_at = proposal.wake_at
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
            if item.executed is not None:
                self._halted = True
                raise ObservationRejectedError(item.executed, error) from error
            if item.durable:
                if self._end_turn_without_wait(item):
                    return True
                self._halted = True
                raise OwnerEventRejectedError(item.event, error) from error
            raise
        return True

    def _awaits_submissions(self, item: _Input[S]) -> bool:
        """Whether a turn end carries a wait on jobs whose submissions are still in flight.

        An agent submits a measurement and waits on it in one turn, so the turn is observed
        before the submission's request has run. The turn end stays in the outbox, and
        is committed once the jobs are owned, when core's wait gate can judge it; or, once
        a submission ended without a job, when that gate refuses it and the turn ends plainly.
        """
        event = item.event
        return (
            item.durable
            and isinstance(event, TurnObserved)
            and event.suspension is not None
            and awaits_submissions(self.record.envelope.core, event.suspension.jobs)
        )

    def _release_held(self) -> None:
        """Queue the held turn ends whose submissions are no longer in flight, in hold order."""
        ready = [item for item in self._held if not self._awaits_submissions(item)]
        if ready:
            self._held = [item for item in self._held if item not in ready]
            self._queue.extendleft(reversed(ready))

    def _end_turn_without_wait(self, item: _Input[S]) -> bool:
        """Replace a rejected turn event that carries a wait with the same turn ending plainly.

        The wait is an agent tool call, and a tool call must never halt the run. Turns in
        one scope can run at once, so a wait the tool bridge accepted from committed state
        can be refused at commit by a peer's. The agent's turn already ran; it ends without
        suspending. Returns False when the event carries no wait, or when ending it plainly
        is rejected too (a committed-state inconsistency, which does halt).
        """
        event = item.event
        if not isinstance(event, TurnObserved) or event.suspension is None:
            return False
        ended = event.model_copy(update={"suspension": None})
        pending = tuple(ended if p == event else p for p in self.record.pending_inputs)
        self._commit(self.record.model_copy(update={"pending_inputs": pending}), item.now_at)
        self._queue.appendleft(_Input[S](event=ended, now_at=item.now_at, durable=True))
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
        self._release_held()

    def _commit(self, candidate: RuntimeRecord[S], now_at: float) -> None:
        # One serialization per commit. What the candidate must satisfy is checked on the
        # value (no encode-then-decode round trip); that its bytes decode back to the same
        # record is a property of the codec, tested over generated records and enforced
        # on every load by `_decode`. `model_copy` skips validators, so the record's own
        # invariants are rechecked here.
        self._registry.validate_envelope(candidate.envelope)
        # A strategy state built with `model_copy(update=...)` never ran its field validators;
        # validating it on the value keeps an invalid state from becoming a durable record
        # that no later process can load.
        strategy = candidate.envelope.strategy
        type(strategy).model_validate(strategy.model_dump(warnings=False))
        candidate.check_publications()
        self._check_identity(candidate.envelope)
        stored = StoredEnvelope(
            revision=0 if self._storage_revision is None else self._storage_revision + 1,
            schema_version=1,
            payload=candidate.model_dump_json().encode(),
        )
        if self._fence is None:
            message = "runtime has no lease"
            raise RuntimeCommitError(message)
        # The store's time watermark never moves back, so a commit made after a later one
        # (an input stamped before an earlier commit, such as a tool call that arrived
        # during a turn) is stamped no earlier than the last commit.
        stamp = max(now_at, self._time_floor)
        try:
            result = self._store.commit(self._storage_revision, stored, self._fence, now=stamp)
        except OSError:
            self._halted = True
            raise
        if isinstance(result, Committed):
            if result.record != stored:
                self._halted = True
                message = "store acknowledged a different runtime record"
                raise RuntimeCommitError(message)
            previous = self._record
            self._record = candidate
            self._storage_revision = stored.revision
            self._time_floor = stamp
            self._commits.committed(previous, candidate)
            if self._check_liveness:
                self._halt_on_orphan_waits()
            return
        self._halted = True
        reloaded = self._load()

        if isinstance(result, Unknown):
            raise RuntimeCommitUncertainError(candidate_visible=reloaded == stored)
        message = f"runtime commit conflict: {result.reason}"
        raise RuntimeCommitError(message)

    def _require_no_orphan_waits(self) -> None:
        record = self.record
        orphans = orphan_waits(record.envelope.core, record.pending_inputs)
        if orphans:
            raise OrphanWaitError(orphans)

    def _halt_on_orphan_waits(self) -> None:
        try:
            self._require_no_orphan_waits()
        except OrphanWaitError:
            self._halted = True
            raise

    def _require_active(self, *, queue_only: bool = False) -> None:
        if self._halted or self._fence is None or (self._busy and not queue_only):
            message = "runtime inactive, busy or commit unconfirmed"
            raise RuntimeCommitError(message)

    def _authorized(self, *, exempt_only: bool = False) -> tuple[Intent, Transition] | None:
        """Ask core for dispatch authority; blocked prerequisites remain pending.

        ``exempt_only`` considers only requests that never wait for a free slot.
        """
        core = self.record.envelope.core
        for intent in core.intents.intents:
            if intent.phase != IntentPhase.PREPARED:
                continue
            if exempt_only and counts_toward_concurrency(intent.request):
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
        started = self._start(now_at=now_at)
        if started is None:
            return DispatchProgress.IDLE
        if isinstance(started, ExecutorRefusal):
            return started
        try:
            await started.task
        except BaseException:
            self._halted = True
            raise
        try:
            self._finish(started)
        except BaseException:
            self._halted = True
            raise
        return DispatchProgress.DISPATCHED

    def _start(
        self, *, now_at: float, exempt_only: bool = False
    ) -> ExecutorRefusal | _Flight | None:
        """Authorize the next prepared request durably and start its executor.

        Synchronous: the commit and the task start cannot interleave with another commit.
        Returns None when no request is eligible and a refusal when its role is unbound.
        """
        self._require_active()
        if self._queue:
            message = "consume queued inputs before dispatch"
            raise RuntimeError(message)
        authorized = self._authorized(exempt_only=exempt_only)
        if authorized is None:
            return None
        intent, transition = authorized
        unbound = self._executors.refusal(intent.request)
        if unbound is not None:
            return unbound
        self._consume(
            _Input[S](event=DispatchAuthorized(request_id=intent.request_id), now_at=now_at),
            transition,
        )
        if self._fence is None or not self._store.verify(
            self._fence, now=max(now_at, self._time_floor)
        ):
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
        self._authorizations += 1
        return _Flight(
            order=self._authorizations,
            intent=intent,
            now_at=now_at,
            task=asyncio.create_task(
                self._executors.dispatch(intent.request, context),
                name=f"dispatch:{intent.request_id.root}",
            ),
        )

    def _finish(self, flight: _Flight) -> str:
        """Validate a finished executor's result and queue its observation; returns the kind.

        Raises what the executor raised. The caller halts the shell on any failure.
        """
        return self._accept(flight, flight.task.result())

    def _accept(self, flight: _Flight, outcome: ExecutionOutcome) -> str:
        intent = flight.intent
        if isinstance(outcome, ExecutorRefusal):
            raise RuntimeExecutionError(
                intent.request_id, f"executor refused after authorization: {outcome.detail}"
            )
        self._validate_observation(intent.request, outcome.observation.observation)
        observed = self._registry.validate_event(outcome.observation)
        for event in outcome.owner_events:
            self._validate_owner_event(intent.request, event)
        self._dispatched += 1
        self._last_kind = intent.request.kind
        self._kinds[intent.request.kind] = self._kinds.get(intent.request.kind, 0) + 1
        self._queue.append(
            _Input[S](
                event=observed,
                now_at=flight.now_at,
                owner_events=outcome.owner_events,
                executed=intent.request_id,
            )
        )
        return intent.request.kind

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
        if self._fence is None or not self._store.verify(
            self._fence, now=max(now_at, self._time_floor)
        ):
            self._halted = True
            message = "runtime fence lost before publication"
            raise RuntimeCommitError(message)
        publication = self.record.pending_publications[0]
        self._busy = True
        try:
            await delivery.publish(
                publication,
                PublicationContext(
                    fence=self._fence, now_at=max(now_at, self._time_floor), lease=_ShellLease(self)
                ),
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

    def _check_cap(self, cap: int | None) -> None:
        if cap is not None and self._dispatched + len(self._flights) > cap:
            raise DispatchCapExceededError(cap, self._kinds)

    @property
    def in_flight(self) -> tuple[Request, ...]:
        """The requests whose executors are running, in authorization order."""
        return tuple(flight.intent.request for flight in self._flights)

    @property
    def stop_ends_in_core(self) -> bool:
        """Whether core will end every running request itself once a stop is committed.

        True when each is a cancellation, or a turn core has asked to cancel. A request
        with no cancellation in core (a measurement submission, a job poll) ends on its
        own or not at all, so a stop that finds one cannot finish through core.
        """
        intents = self.record.envelope.core.intents.intents
        return all(settles_through_core(f.intent.request, intents) for f in self._flights)

    def _finish_done(self) -> bool:
        """Queue the observations of finished flights, ties in authorization order.

        A flight that failed does not drop the observations of the flights that finished
        with it: those are committed first. The failure then halts the shell, and the
        peers still running are cancelled by the caller, because a halted shell can no
        longer commit what they would return. Their intents stay DISPATCHED, like the
        failed one, for the next epoch to inspect.
        """
        done = sorted((f for f in self._flights if f.task.done()), key=lambda f: f.order)
        failure: BaseException | None = None
        queued = len(self._queue)
        for flight in done:
            self._flights.remove(flight)
            error = _task_failure(flight.task)
            if error is None:
                try:
                    self._finish(flight)
                except (ContractError, RuntimeExecutionError) as rejected:
                    error = rejected
            failure = failure or error
        if failure is not None:
            self._halt_with(failure, commit=len(self._queue) > queued)
        return bool(done)

    def _halt_with(self, failure: BaseException, *, commit: bool) -> None:
        """Commit what finished flights queued beside the failure, then halt and raise it.

        With no such observation nothing commits: a failing request is not a reason to
        write anything else.
        """
        try:
            while commit and not self._halted and self._queue:
                self.advance()
        finally:
            self._halted = True
        raise failure

    def _start_available(
        self,
        now_at: float,
        max_concurrent: int,
        cap: int | None,
    ) -> ExecutorRefusal | None:
        """Start eligible requests while a slot is free; returns a refusal if one hit.

        Cancellations start whether or not a slot is free.
        """
        if max_concurrent < 1:
            message = "max_concurrent must be at least 1"
            raise ValueError(message)
        while True:
            self._check_cap(cap)
            occupied = sum(1 for f in self._flights if counts_toward_concurrency(f.intent.request))
            began = self._start(now_at=now_at, exempt_only=occupied >= max_concurrent)
            if isinstance(began, ExecutorRefusal):
                return began
            if began is None:
                return None
            self._flights.append(began)

    @staticmethod
    def _idle_result(
        refusal: ExecutorRefusal | None, publication_error: OSError | ContractError | None
    ) -> ExecutorRefusal | None:
        """The end of a drain: a refusal wins, then a delivery failure is re-raised."""
        if refusal is not None:
            return refusal
        if publication_error is not None:
            raise publication_error
        return None

    async def _try_publish(
        self, delivery: PublicationDelivery, now_at: float
    ) -> tuple[bool, OSError | ContractError | None]:
        """Publish one pending event; a delivery failure is returned, not raised."""
        try:
            return await self.publish_one(delivery, now_at=now_at), None
        except (OSError, ContractError) as error:
            return False, error

    async def abandon(self) -> None:
        """Halt and cancel the running requests the loop will not complete.

        For a failure, a stop that core cannot carry out, or the cancellation of the loop.
        Their intents stay DISPATCHED, so the next epoch inspects them.
        """
        self._halted = True
        flights = list(self._flights)
        self._flights.clear()
        for flight in flights:
            flight.task.cancel()
        await asyncio.gather(*(f.task for f in flights), return_exceptions=True)

    async def wait_for_flight(self) -> None:
        """Return once a running request has finished (at once when one already has).

        Does not complete anything: ``settle`` commits the finished request's observation.
        Returns immediately when nothing is running.
        """
        if self._flights:
            await asyncio.wait([f.task for f in self._flights], return_when=asyncio.FIRST_COMPLETED)

    async def settle(
        self,
        delivery: PublicationDelivery,
        *,
        now_at: float,
        max_dispatches: int | None = None,
        max_concurrent: int = 1,
        refusal: ExecutorRefusal | None = None,
    ) -> ExecutorRefusal | None:
        """Do everything that does not wait for a running request, then return.

        Commits the input queue and the finished requests' observations, publishes the
        outbox, and starts prepared requests while a slot is free. Requests that are
        still running stay running and are completed by a later ``settle``, so the caller
        can decide, tick the clock and read controls while they run. A refusal (an unbound
        role) is returned; passing a refusal already hit starts nothing more.

        Publication is diagnostic, so a failing delivery never blocks dispatch
        (cancellations must still go out). Publishing stops after the first failure and
        that failure is re-raised once nothing is running.

        Only this method authorizes, commits and queues observations, so the record has
        one writer; observations commit in completion order, same-tick completions in
        authorization order. A failure cancels the requests still running (see
        ``_finish_done``). ``max_dispatches`` bounds the requests executed from now on:
        a cycle that keeps issuing requests raises ``DispatchCapExceededError`` naming the
        kinds seen, so a spinning run fails loudly instead of hanging.
        """
        entered = self._dispatched
        try:
            while True:
                # A failed request surfaces before anything else commits.
                if self._finish_done():
                    continue
                if self.advance():
                    continue
                if self._publication_failure is None:
                    published, self._publication_failure = await self._try_publish(delivery, now_at)
                    if published or self._publication_failure is not None:
                        # The await let tools admit inputs and heartbeats commit: consume
                        # them before the next request starts.
                        continue
                if refusal is None:
                    budget = None if max_dispatches is None else max_dispatches + entered
                    refusal = self._start_available(now_at, max_concurrent, budget)
                if self._flights:
                    return refusal
                failure, self._publication_failure = self._publication_failure, None
                return self._idle_result(refusal, failure)
        except BaseException:
            await self.abandon()
            raise

    async def run_until_idle(
        self,
        delivery: PublicationDelivery,
        *,
        now_at: float,
        max_dispatches: int | None = None,
        max_concurrent: int = 1,
    ) -> ExecutorRefusal | None:
        """``settle`` repeatedly, waiting for running requests, until none is running.

        A refusal stops new starts, lets the requests already running finish and commit,
        then is returned. ``max_dispatches`` bounds the requests executed by this call.
        """
        refusal: ExecutorRefusal | None = None
        first = self._dispatched
        while True:
            refusal = await self.settle(
                delivery,
                now_at=now_at,
                max_dispatches=None
                if max_dispatches is None
                else max_dispatches - (self._dispatched - first),
                max_concurrent=max_concurrent,
                refusal=refusal,
            )
            if not self._flights:
                return refusal
            await self.wait_for_flight()

    async def finish_in_flight(
        self, delivery: PublicationDelivery, *, now_at: float, refusal: ExecutorRefusal
    ) -> None:
        """Let the running requests finish and commit after ``refusal``, starting nothing."""
        while True:
            await self.settle(delivery, now_at=now_at, refusal=refusal)
            if not self._flights:
                return
            await self.wait_for_flight()
