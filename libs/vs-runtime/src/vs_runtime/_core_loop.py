"""Durable shell. Core owns policy; this module owns commit-before-I/O and the single writer."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    ClockAdvanced,
    ContractError,
    ControlRefusedError,
    CoreEvent,
    CoreState,
    Decision,
    Discarded,
    DispatchAuthorized,
    EventCursor,
    HostFence,
    HostId,
    HostRenewed,
    Inbox,
    KernelNotImplementedError,
    Observation,
    OperationRegistry,
    RecoveryStarted,
    RequestId,
    RequestPhase,
    RequestRecord,
    RunControlEvent,
    RunEnvelope,
    SearchDriver,
    SearchHost,
    StrategyState,
    TimeReached,
    Transition,
    TurnObserved,
    advance,
    check_decisions,
    deferred,
    encode_json,
    orphan_waits,
    pause_withdraws,
    run_view,
    share_unchanged,
    step,
    translate,
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
)
from vs_runtime._evaluation_jobs import awaits_submissions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from vs_core.api import OpenWait, Request
    from vs_project.api import Project
    from vs_runtime._core_requests import ExecutionOutcome
    from vs_runtime._mid_turn_steering import TurnSteering


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

    The record stays DISPATCHED. Core never receives an invented outcome: the
    executor may have performed the effect, so only a new epoch's inspection
    may classify it.
    """

    def __init__(self, request_id: RequestId, detail: str) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id.root} halted the runtime: {detail}")


class ObservationRejectedError(RuntimeExecutionError):
    """Core rejected an executor's observation after the effect ran; the shell halted.

    Dropping the observation would leave the record DISPATCHED with the result
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

    def __init__(self, orphans: tuple[OpenWait, ...]) -> None:
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

    def write_if_held(self, mutation: Callable[[], None]) -> bool:
        return self.shell.write_if_held(mutation)


def _task_failure(task: asyncio.Task[Any]) -> BaseException | None:
    """What a finished task raised, without raising it."""
    return asyncio.CancelledError() if task.cancelled() else task.exception()


@dataclass(frozen=True)
class _Flush:
    """Deliver the wake-ups that are due and submit the decisions that were held."""

    now_at: float


@dataclass(frozen=True)
class _Flight:
    """A request that is durably authorized and executing; only the loop completes it."""

    order: int
    """Authorization index within the shell: the tie-break between same-tick completions."""
    record: RequestRecord
    now_at: float
    task: asyncio.Task[ExecutionOutcome]


@dataclass(frozen=True)
class CoreRuntimeBindings:
    """Closed composition choices, separate from durable shell state."""

    registry: OperationRegistry = field(default_factory=OperationRegistry)
    executors: RequestExecutors = field(default_factory=RequestExecutors)
    transitions: CoreTransitions = field(default_factory=ProductionCoreTransitions)
    commits: CommitObserver = field(default_factory=IgnoreCommits)
    epoch: Callable[[], int | None] = lambda: None
    """Native writer identity derived from run.log; absent for an unjournaled host."""
    check_liveness: bool = False
    """Check ``orphan_waits`` after every commit, not only at start. Linear in state size."""
    steering: TurnSteering | None = None
    """Reaches the turns this host runs while they run; None offers no input mid-turn."""


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
        search: SearchDriver[S, Any],
        initial: CoreState,
        *,
        config: object,
        bindings: CoreRuntimeBindings | None = None,
    ) -> None:
        self._store = store
        self._search = search
        self._config = config
        self._initial = initial
        selected = bindings or CoreRuntimeBindings()
        self._epoch = selected.epoch
        self._registry = selected.registry
        self._executors = selected.executors
        self._transitions = selected.transitions
        self._commits = selected.commits
        self._check_liveness = selected.check_liveness
        self.steering = selected.steering
        """What reaches a running turn with an interrupt-mode input, if the host has one."""
        self._record_model = cast(
            "type[RuntimeRecord[S]]", RuntimeRecord.__class_getitem__(search.state_model)
        )
        self._envelope_model = cast(
            "type[RunEnvelope[S]]", RunEnvelope.__class_getitem__(search.state_model)
        )
        self._host = SearchHost(step=self._transitions.step, registry=self._registry)
        self._record: RuntimeRecord[S] | None = None
        self._discarded: list[Discarded] = []
        self._storage_revision: int | None = None
        self._fence: StoreFence | None = None
        self._halted = False
        self._busy = False
        # Authorized requests whose executors are still running. They outlive the calls
        # that start them, so the run loop can decide, tick the clock and read controls
        # while they run; only `settle` completes them.
        self._flights: list[_Flight] = []
        self._dispatched = 0
        self._authorizations = 0
        self._kinds: dict[str, int] = {}
        # A delivery failure waits here, with publishing paused, until nothing is running.
        self._publication_failure: OSError | ContractError | None = None
        # Latest time a lease renewal or check supplied; commits never use an earlier time.
        self._time_floor = 0.0
        # When the store last granted this host its lease (acquired or renewed), and the
        # latest renewal queued for core.
        self._renewed_at: float | None = None
        self._renewal_queued_at = 0.0
        self._queue: deque[_Input[S] | _Flush] = deque()
        # Durable turn ends whose wait names a job its submission has not made core's yet.
        self._held: list[_Input[S]] = []
        # Operator controls core refused since the run loop last took them (``take_refused``).
        self._refused: list[ControlRefusedError] = []
        # State after the admitted inputs still queued, valid while core stays at one revision
        # (a publication or lease commit changes the storage revision, not core).
        self._tail: tuple[int, CoreState] | None = None
        # The (run, registry) values `_check_identity` last accepted. Both are frozen, so
        # the same objects always give the same verdict.
        self._identity_verified: tuple[object, object] | None = None
        # The record and host a start last validated, so a start retried while a lease is
        # held does not validate the same record again.
        self._start_checked: tuple[RuntimeRecord[S], str] | None = None

    @classmethod
    def resume(
        cls,
        project: Project,
        search: SearchDriver[S, Any],
        *,
        config: object,
        run_id: str | None = None,
        bindings: CoreRuntimeBindings | None = None,
    ) -> CoreRuntime[S]:
        """Read-only preflight before starting the new-envelope recovery shell.

        Executor implementations must defer setup to committed requests. This
        factory resolves the selected Project run before invoking any executor.
        It never converts legacy identities or starts a fresh replacement run.
        """
        selected = bindings or CoreRuntimeBindings()
        resolved = resolve_core_resume(project, search, run_id=run_id, registry=selected.registry)
        return cls(
            resolved.store, search, resolved.record.envelope.core, config=config, bindings=selected
        )

    @property
    def discarded(self) -> tuple[Discarded, ...]:
        """What core kept from the search since start (internal failures, late results).

        The shell logs these; they are typed and never reach the search.
        """
        return tuple(self._discarded)

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
        if not envelope.core.run.declaration.resumable_as(self._search.declaration):
            raise ContractError(
                ("declaration",), "offered strategy differs from durable declaration"
            )
        if envelope.core.run.run_id != self._initial.run.run_id:
            raise ContractError(("run_id",), "selected run differs from durable envelope")
        selected = validate_startup(self._search.declaration, envelope.core.run.capabilities)
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
        # A store revision names one write, so the record decoded from it (or committed as
        # it) still stands; a start waiting out a lease reloads every poll without decoding.
        if self._record is None or stored.revision != self._storage_revision:
            self._record = self._decode(stored)
            self._storage_revision = stored.revision
        return stored

    def _start_validated(self, host_id: str) -> bool:
        """Whether a start already validated this very record for this host."""
        checked = self._start_checked
        return (
            self._record is not None
            and checked is not None
            and checked[0] is self._record
            and checked[1] == host_id
        )

    def _validate_provisional(self, provisional: RunEnvelope[S], host_id: str) -> None:
        """The envelope a start would commit round-trips through the registry."""
        self._registry.reload_envelope(self._envelope_model, provisional)
        if self._record is not None:
            self._start_checked = (self._record, host_id)

    def _provisional(
        self, host_id: str, *, now_at: float, resume: bool = False
    ) -> tuple[RunEnvelope[S], tuple[Decision, ...]]:
        """The envelope a start by ``host_id`` would commit, validated, and a new run's decisions.

        ``now_at`` dates a new run; a resumed one keeps its own times.
        """
        core = (
            _started_at(self._initial, now_at)
            if self._record is None
            else self.record.envelope.core
        )
        duration = core.run.duration_seconds or self._initial.run.duration_seconds
        if duration is not None and (self._record is None or resume):
            core = core.model_copy(
                update={
                    "run": core.run.model_copy(
                        update={
                            "duration_seconds": duration,
                            "deadline_at": max(core.run.now_at, now_at) + duration,
                        }
                    )
                }
            )
        if not core.run.declaration.resumable_as(self._search.declaration):
            raise ContractError(("declaration",), "initial strategy declaration mismatch")
        selected = validate_startup(self._search.declaration, core.run.capabilities)
        if selected.operations != core.registry:
            raise ContractError(("registry",), "selected operations differ from durable registry")
        recheck = not self._start_validated(host_id)
        if recheck:
            self._registry.validate_core(core)
        decisions: tuple[Decision, ...] = ()
        if self._record is None:
            with _search_generators():
                strategy_state, first = self._search.init(
                    self._config, core.run.facts, run_view(core)
                )
            decisions = check_decisions(first)
        else:
            strategy_state = self.record.envelope.strategy
        provisional = RunEnvelope[S](
            schema_version=ENVELOPE_SCHEMA_VERSION,
            fence=HostFence(host_id=HostId(root=host_id), epoch=0),
            strategy_id=self._search.declaration.strategy_id,
            state_schema=self._search.declaration.state_schema,
            core=core,
            strategy=strategy_state,
            event_cursor=EventCursor(sequence=0)
            if self._record is None
            else self.record.envelope.event_cursor,
        )
        if recheck:
            self._validate_provisional(provisional, host_id)
        return provisional, decisions

    def prepare_start(self, host_id: str) -> None:
        """Decode and validate the stored record a `start` by ``host_id`` would resume.

        The shell remembers both for that very store revision and host, so a `start` that
        follows only steps and commits. Taking in a long run's record costs seconds on the
        caller's thread; preparing first lets the caller yield between the two halves and read
        the clock for the lease after them, so the lease term is not spent on them. A run with
        nothing stored has nothing to prepare.
        """
        if self._fence is not None:
            message = "runtime already started"
            raise RuntimeError(message)
        if self._load() is not None:
            # A resumed run's start never reads its time before taking the lease.
            self._provisional(host_id, now_at=self.record.envelope.core.run.now_at)

    def start(
        self, host_id: str, *, now_at: float, lease_duration: float, resume: bool = False
    ) -> None:
        """Validate before lease acquisition, then commit new-epoch recovery.

        No backend setup or execution occurs here. Unfinished identities produce
        inspection requests; core gates ordinary admission until reconciled.
        """
        if self._fence is not None:
            message = "runtime already started"
            raise RuntimeError(message)
        self._load()
        provisional, decisions = self._provisional(host_id, now_at=now_at, resume=resume)
        published_epoch = self._epoch()
        fence = self._store.acquire(host_id, now=now_at, duration=lease_duration)
        if fence is None:
            message = "runtime lease unavailable"
            raise LeaseUnavailableError(message)
        epoch = fence.epoch if published_epoch is None else published_epoch
        if published_epoch is not None and epoch <= provisional.core.records.recovery.epoch:
            self._store.release(fence, now=now_at)
            raise ContractError(("epoch",), "run.log must publish a successor before recovery")
        self._fence = fence
        self._renewed_at = now_at
        provisional = provisional.model_copy(
            update={"fence": HostFence(host_id=HostId(root=fence.host_id), epoch=epoch)}
        )
        previous_record = self._record
        previous_revision = self._storage_revision
        self._record = (
            RuntimeRecord[S].fresh(provisional).model_copy(update={"pending_decisions": decisions})
            if self._record is None
            else self.record.model_copy(update={"envelope": provisional})
        )
        try:
            self._consume(
                _Input[S](
                    event=RecoveryStarted(epoch=epoch, now_at=now_at),
                    now_at=now_at,
                )
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
        self._renewed_at = self._time_floor
        return renewed

    @property
    def lease_renewed_at(self) -> float:
        """When the store last granted this host its lease; it lapses a lease duration later.

        The time of the acquisition at ``start`` or of the latest ``renew``, in the store's
        time, so a caller that schedules renewals from it never trusts a lease older than it
        thinks (``start`` may commit for a long time after acquiring).
        """
        if self._renewed_at is None:
            message = "runtime has no lease"
            raise RuntimeCommitError(message)
        return self._renewed_at

    def record_renewal(self) -> None:
        """Queue the latest lease renewal for core, which records that this host was alive then.

        Core credits open limits with the time no host was alive, measured from the dead
        host's last recorded renewal or commit, so the loop records each renewal before it
        settles. A renewal core already holds, or one already queued, is not queued again.
        """
        self._require_active(queue_only=True)
        renewed_at = self._renewed_at
        hosts = self.record.envelope.core.records.hosts
        if (
            renewed_at is None
            or self._fence is None
            or not hosts
            or renewed_at <= max(hosts[-1].alive_at, self._renewal_queued_at)
        ):
            return
        self._renewal_queued_at = renewed_at
        self.submit(
            HostRenewed(epoch=self.record.envelope.fence.epoch, now_at=renewed_at),
            now_at=renewed_at,
        )

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

    def write_if_held(self, mutation: Callable[[], None]) -> bool:
        """Run *mutation* under the store's fence check, at the latest time this shell has seen.

        A renewal while a publication awaits stamps the store with a later time than the
        publication started at; judging the write at that start would refuse a host that
        still owns the run.
        """
        if self._halted or self._fence is None:
            return False
        return self._store.mutate_if_fenced(self._fence, self._time_floor, mutation)

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
    def next_wake_at(self) -> float | None:
        """The earliest time the search asked to be woken at, or None when it asked for none."""
        return min(self.record.wakes, default=None)

    def flush(self, *, now_at: float) -> None:
        """Queue delivery of due wake-ups and submission of held decisions.

        Runs against the state when it is consumed, so the loop calls it each iteration.
        """
        self._require_active(queue_only=True)
        _Input[S](event=ClockAdvanced(now_at=now_at), now_at=now_at)  # the supplied-time check
        self._queue.append(_Flush(now_at))

    def advance(self) -> bool:
        """Commit the next queued input and callback state, with no I/O dispatch.

        The input leaves the queue before it is stepped. A rejected input
        (ContractError) is dropped without halting: nothing was committed, and
        submitters redeliver durable occurrences after a rejection or crash.
        The exceptions are an executor's observation and the owner events committed
        with it: the effect already ran and nobody redelivers them, so a rejection
        halts the shell with ``ObservationRejectedError`` or
        ``OwnerEventRejectedError`` instead of losing the result. An operator's control
        (a ``RunControlEvent`` nobody executed) that core's ``control_refusal`` refuses is
        set aside for ``take_refused`` and the loop goes on: the run's state may have moved
        since the control bridge judged it (an input committed first closed the run), and
        the bridge answers the operator with core's reason. Any other rejected input raises,
        a malformed control included (an identity conflict is a defect in its builder).
        """
        self._require_active()
        if not self._queue:
            return False
        item = self._queue.popleft()
        if isinstance(item, _Input) and self._awaits_submissions(item):
            self._held.append(item)
            return True
        if isinstance(item, _Flush):
            self._flush(item)
            return True
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
            if isinstance(item.event, RunControlEvent) and isinstance(error, ControlRefusedError):
                self._refused.append(error)
                return True
            raise
        return True

    def take_refused(self) -> tuple[ControlRefusedError, ...]:
        """The controls core refused since the last call, in commit order, each once."""
        refused, self._refused = tuple(self._refused), []
        return refused

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
        pending = self.record.pending_inputs
        if item.durable:
            index = pending.index(item.event)  # type: ignore[arg-type]
            pending = (*pending[:index], *pending[index + 1 :])
        self._apply(
            transition,
            item.now_at,
            pending_inputs=(*pending, *item.owner_events),
            due=(),
        )
        self._queue.extend(
            _Input[S](event=event, now_at=item.now_at, durable=True) for event in item.owner_events
        )
        self._release_held()

    def _flush(self, item: _Flush) -> None:
        """Deliver due wake-ups and submit held decisions; commit only if there was any.

        A held decision core still cannot take (``deferred``: recovery not ready, or the run
        paused) is not resubmitted, so a paused run writes nothing until it is resumed.
        """
        record = self.record
        due = tuple(wake for wake in record.wakes if wake <= item.now_at)
        core = record.envelope.core
        takeable = any(not deferred(core, held) for held in record.pending_decisions)
        if not due and not takeable:
            return
        self._apply(
            Transition(state=record.envelope.core),
            item.now_at,
            pending_inputs=record.pending_inputs,
            due=due,
        )

    def _apply(
        self,
        transition: Transition,
        now_at: float,
        *,
        pending_inputs: tuple[OwnerEvent, ...],
        due: tuple[float, ...],
    ) -> None:
        """Commit one core transition together with everything the search did about it.

        The search sees the events one at a time, in order; the decisions it returns go to
        core in the same pass, and the events those cause go back to the search, until it
        has nothing more to say. The search state, core and published events then commit as
        one record.
        """
        record = self.record
        envelope = record.envelope
        translated = translate(transition.state, transition.events)
        arrived = (*translated.events, *(TimeReached(time=wake) for wake in due))
        with _search_generators():
            advanced = advance(
                self._search,
                envelope.strategy,
                transition.state,
                self._host,
                Inbox(arrived, record.pending_decisions, translated.discarded),
            )
        self._discarded.extend(advanced.discarded)
        events = (*transition.events, *advanced.published)
        publications = tuple(
            Publication(
                publication_id=f"{advanced.core.run.run_id.root}:{sequence}",
                sequence=sequence,
                event=event,
            )
            for sequence, event in enumerate(events, start=record.next_publication_sequence)
        )
        waiting = tuple(wake for wake in record.wakes if wake not in due)
        candidate = record.model_copy(
            update={
                "envelope": envelope.model_copy(
                    update={
                        "core": advanced.core,
                        "strategy": advanced.state,
                        "event_cursor": EventCursor(
                            sequence=envelope.event_cursor.sequence + len(events)
                        ),
                    }
                ),
                "pending_publications": (*record.pending_publications, *publications),
                "next_publication_sequence": record.next_publication_sequence + len(publications),
                "pending_inputs": pending_inputs,
                "pending_decisions": advanced.held,
                "wakes": tuple(sorted({*waiting, *advanced.wakes})),
            }
        )
        self._commit(candidate, now_at)

    def _commit(self, candidate: RuntimeRecord[S], now_at: float) -> None:
        # Whatever the candidate leaves equal to the committed record is that record's object,
        # whichever path built it (a strategy's fold returns an equal state of its own making).
        # The checks and the encoding below remember what they learn per object, so only values
        # that actually changed cost anything.
        if self._record is not None:
            candidate = share_unchanged(self._record, candidate)
        # One serialization per commit. What the candidate must satisfy is checked on the
        # value (no encode-then-decode round trip); that its bytes decode back to the same
        # record is a property of the codec, tested over generated records and enforced
        # on every load by `_decode`. `model_copy` skips validators, so the record's own
        # invariants are rechecked here.
        self._registry.validate_envelope(candidate.envelope)
        # A strategy state built with `model_copy(update=...)` never ran its field validators;
        # validating it on the value keeps an invalid state from becoming a durable record
        # that no later process can load. The committed record's state was validated when it
        # was committed (or decoded), so a candidate that carries the very same object needs
        # nothing again.
        strategy = candidate.envelope.strategy
        if self._record is None or self._record.envelope.strategy is not strategy:
            type(strategy).model_validate(strategy.model_dump(warnings=False))
        candidate.check_publications()
        self._check_identity(candidate.envelope)
        stored = StoredEnvelope(
            revision=0 if self._storage_revision is None else self._storage_revision + 1,
            schema_version=1,
            payload=encode_json(candidate),
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

    def _authorized(
        self, *, now_at: float, exempt_only: bool = False
    ) -> tuple[RequestRecord, Transition] | None:
        """Ask core for dispatch authority; blocked prerequisites remain pending.

        ``exempt_only`` considers only requests that never wait for a free slot.
        """
        core = self.record.envelope.core
        for record in core.records.records:
            if record.phase != RequestPhase.PREPARED:
                continue
            if exempt_only and counts_toward_concurrency(record.request):
                continue
            try:
                transition = self._transitions.step(
                    core, DispatchAuthorized(request_id=record.request_id, now_at=now_at)
                )
            except ContractError as error:
                if error.path not in (("dependency",), ("recovery",)):
                    raise
                continue
            return record, transition
        return None

    async def dispatch_one(self, *, now_at: float) -> ExecutorRefusal | DispatchProgress:
        """Persist authorization before I/O. Already dispatched work needs recovery.

        A role without a bound executor is refused before anything is committed,
        so the record stays PREPARED. Once authorization is durable, an executor
        exception, refusal or unusable result halts the shell (the record stays
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
        authorized = self._authorized(now_at=now_at, exempt_only=exempt_only)
        if authorized is None:
            return None
        record, transition = authorized
        unbound = self._executors.refusal(record.request)
        if unbound is not None:
            return unbound
        self._consume(
            _Input[S](
                event=DispatchAuthorized(request_id=record.request_id, now_at=now_at), now_at=now_at
            ),
            transition,
        )
        if self._fence is None or not self._store.verify(
            self._fence, now=max(now_at, self._time_floor)
        ):
            self._halted = True
            message = "runtime fence lost before execution"
            raise RuntimeCommitError(message)
        authorized_record = self.record.envelope.core.records.record(record.request_id)
        if (
            authorized_record is None
            or authorized_record.phase != RequestPhase.DISPATCHED
            or authorized_record.request != record.request
            or authorized_record.payload_digest != record.payload_digest
        ):
            raise ContractError(
                ("dispatch_authorized",), "exact canonical request must be durably authorized"
            )
        context = ExecutionContext(
            fence=self.record.envelope.fence,
            now_at=now_at,
            payload_digest=record.payload_digest,
            until=authorized_record.until,
            lease=_ShellLease(self),
        )
        self._authorizations += 1
        return _Flight(
            order=self._authorizations,
            record=authorized_record,
            now_at=now_at,
            task=asyncio.create_task(
                self._executors.dispatch(record.request, context),
                name=f"dispatch:{record.request_id.root}",
            ),
        )

    def _finish(self, flight: _Flight) -> str:
        """Validate a finished executor's result and queue its observation; returns the kind.

        Raises what the executor raised. The caller halts the shell on any failure.
        """
        return self._accept(flight, flight.task.result())

    def _accept(self, flight: _Flight, outcome: ExecutionOutcome) -> str:
        record = flight.record
        if isinstance(outcome, ExecutorRefusal):
            raise RuntimeExecutionError(
                record.request_id, f"executor refused after authorization: {outcome.detail}"
            )
        self._validate_observation(record.request, outcome.observation.observation)
        observed = self._registry.validate_event(outcome.observation)
        for event in outcome.owner_events:
            self._validate_owner_event(record.request, event)
        self._dispatched += 1
        self._kinds[record.request.kind] = self._kinds.get(record.request.kind, 0) + 1
        self._queue.append(
            _Input[S](
                event=observed,
                now_at=flight.now_at,
                owner_events=outcome.owner_events,
                executed=record.request_id,
            )
        )
        return record.request.kind

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
        self._time_floor = max(self._time_floor, now_at)
        self._busy = True
        try:
            await delivery.publish(
                publication, PublicationContext(now_at=self._time_floor, lease=_ShellLease(self))
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
        return tuple(flight.record.request for flight in self._flights)

    def _finish_done(self) -> bool:
        """Queue the observations of finished flights, ties in authorization order.

        A flight that failed does not drop the observations of the flights that finished
        with it: those are committed first. The failure then halts the shell, and the
        peers still running are cancelled by the caller, because a halted shell can no
        longer commit what they would return. Their records stay DISPATCHED, like the
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
        upkeep: Callable[[], None],
    ) -> ExecutorRefusal | None:
        """Start eligible requests while a slot is free; returns a refusal if one hit.

        Cancellations start whether or not a slot is free. Each start commits, so ``upkeep``
        runs after each one, as between the steps of ``settle``; when it queues an input (a
        renewal for core) this returns, for ``settle`` to commit the input before the next start.
        """
        if max_concurrent < 1:
            message = "max_concurrent must be at least 1"
            raise ValueError(message)
        while True:
            self._check_cap(cap)
            occupied = sum(1 for f in self._flights if counts_toward_concurrency(f.record.request))
            began = self._start(now_at=now_at, exempt_only=occupied >= max_concurrent)
            if isinstance(began, ExecutorRefusal):
                return began
            if began is None:
                return None
            self._flights.append(began)
            upkeep()
            if self._queue:
                return None

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
        Their records stay DISPATCHED, so the next epoch inspects them.
        """
        self._halted = True
        flights = list(self._flights)
        self._flights.clear()
        for flight in flights:
            flight.task.cancel()
        await asyncio.gather(*(f.task for f in flights), return_exceptions=True)

    def _ended_by_core(self, flight: _Flight) -> bool:
        """Whether core has ended this request.

        Core ends a record at its limit and when a watch fires (it becomes BLOCKED and
        its owner hears ``Failed``). The executor may not know, so the loop stops it.
        Core alone judges the limit: it moves a record's bound (the time a job queued, a
        quota pause held the run, or the capacity gate held a turn is never charged to it),
        so neither the limit the request was authorized with nor the record's stated
        ``until`` is when it ends (S179).
        """
        current = self.record.envelope.core.records.record(flight.record.request_id)
        return current is None or current.phase in (RequestPhase.BLOCKED, RequestPhase.COMPLETED)

    async def cancel_ended(self) -> bool:
        """Cancel the running requests core has ended, at their limit or by a watch.

        This is the only place a running request is stopped because a limit or watch
        fired. A cancelled request returns no observation; core already reported it.
        """
        ended = [f for f in self._flights if not f.task.done() and self._ended_by_core(f)]
        return await self._cancel_flights(ended)

    async def _cancel_withdrawn(self) -> bool:
        return await self._cancel_flights(
            [
                flight
                for flight in self._flights
                if pause_withdraws(self.record.envelope.core, flight.record.request)
            ]
        )

    async def _cancel_flights(self, flights: Sequence[_Flight]) -> bool:
        for flight in flights:
            self._flights.remove(flight)
            flight.task.cancel()
        await asyncio.gather(*(flight.task for flight in flights), return_exceptions=True)
        return bool(flights)

    async def wait_for_flight(self) -> None:
        """Return once a running request has finished (at once when one already has).

        Does not complete anything: ``settle`` commits the finished request's observation.
        Returns immediately when nothing is running.
        """
        if self._flights:
            await asyncio.wait([f.task for f in self._flights], return_when=asyncio.FIRST_COMPLETED)

    async def settle(  # noqa: PLR0913  # lint-waiver: LW-517306 [PLR0913]; the loop's lease upkeep joins the settle's limits as one more keyword.
        # > A settings object bundling the limits and the upkeep adds a type for two call sites;
        # > storing the upkeep on the shell makes every other settle caller inherit the loop's.
        self,
        delivery: PublicationDelivery,
        *,
        now_at: float,
        max_dispatches: int | None = None,
        max_concurrent: int = 1,
        refusal: ExecutorRefusal | None = None,
        upkeep: Callable[[], None] = lambda: None,
    ) -> ExecutorRefusal | None:
        """Do everything that does not wait for a running request, then return.

        Commits the input queue and the finished requests' observations, publishes the
        outbox, and starts prepared requests while a slot is free. Requests that are
        still running stay running and are completed by a later ``settle``, so the caller
        can decide, tick the clock and read controls while they run. A refusal (an unbound
        role) is returned; passing a refusal already hit starts nothing more.

        Publication is diagnostic, so it never blocks dispatch: prepared requests start
        before each publication is delivered, and a slow delivery holds a request back by at
        most its own length, never by the whole backlog (cancellations and closes must still
        go out before the run deadline). Publishing stops after the first failure and that
        failure is re-raised once nothing is running.

        Only this method authorizes, commits and queues observations, so the record has
        one writer; observations commit in completion order, same-tick completions in
        authorization order. A failure cancels the requests still running (see
        ``_finish_done``). ``max_dispatches`` bounds the requests executed from now on:
        a cycle that keeps issuing requests raises ``DispatchCapExceededError`` naming the
        kinds seen, so a spinning run fails loudly instead of hanging.

        ``upkeep`` runs after every step that committed, each start included. Each commit rewrites the whole record
        on the loop's thread, at a cost that grows with the run, and nothing here need suspend
        (a delivery may not), so a long queue committed back to back can hold the loop for
        longer than the lease while the heartbeat task cannot run: the caller renews it there
        when it is due.
        """
        entered = self._dispatched
        try:
            while True:
                # A failed request surfaces before anything else commits.
                if (
                    await self._cancel_withdrawn()
                    or self._finish_done()
                    or await self.cancel_ended()
                    or self.advance()
                ):
                    upkeep()
                    continue
                if refusal is None:
                    budget = None if max_dispatches is None else max_dispatches + entered
                    refusal = self._start_available(now_at, max_concurrent, budget, upkeep)
                    if self._queue:
                        continue
                if self._publication_failure is None:
                    published, self._publication_failure = await self._try_publish(delivery, now_at)
                    if published or self._publication_failure is not None:
                        # The await let requests finish, tools admit inputs and heartbeats
                        # commit: consume them before the next request starts.
                        upkeep()
                        continue
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
        self,
        delivery: PublicationDelivery,
        *,
        now_at: float,
        refusal: ExecutorRefusal,
        upkeep: Callable[[], None] = lambda: None,
    ) -> None:
        """Let the running requests finish and commit after ``refusal``, starting nothing.

        ``upkeep`` is as for ``settle``.
        """
        while True:
            await self.settle(delivery, now_at=now_at, refusal=refusal, upkeep=upkeep)
            if not self._flights:
                return
            await self.wait_for_flight()


def _drop(_generator: object) -> None:
    """Finalize an abandoned search generator by running none of its code."""


@contextlib.contextmanager
def _search_generators() -> Iterator[None]:
    """Keep the async generators a search starts (a flow's ``@resource`` bodies) from the loop.

    The search runs inside this shell's event loop but is not driven by it. The loop registers
    every async generator first iterated inside it, and closes the ones still alive at shutdown
    (or dropped) by running their release as a loop task, where each ``ctx`` wait is an
    awaitable the loop cannot run. A search's generator outlives its flow only with its run,
    when this host died or was replaced, and a dead host's search must start no more work: the
    next host settles what it held from the journal. So no loop hears of it, and it is
    finalized by running nothing.
    """
    previous = sys.get_asyncgen_hooks()
    sys.set_asyncgen_hooks(firstiter=None, finalizer=_drop)
    try:
        yield
    finally:
        sys.set_asyncgen_hooks(*previous)


def _started_at(core: CoreState, now_at: float) -> CoreState:
    """A new run's state with its clock at the time it starts.

    A new run's state is built before it starts, with no time on its clock. The search's first
    step runs before core hears of the clock (``RecoveryStarted``), so without this it would
    read time zero: its root span would start at zero and a deadline it computes from ``now``
    would already have passed.
    """
    return core.model_copy(
        update={"run": core.run.model_copy(update={"now_at": max(core.run.now_at, now_at)})}
    )
