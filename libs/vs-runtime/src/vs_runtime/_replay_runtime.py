"""Fenced recorder for the keyed scheduler, using today's atomic StateStore."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, assert_never, runtime_checkable

from protobuf import Message, Oneof
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import (
    ENCODING_VERSION,
    AcceptedClock,
    AcceptedReplay,
    ReplayAccounting,
    ReplayArchive,
    ReplayError,
    ReplayIterationBlocked,
    ReplayResourcesBlocked,
    ReplayScheduler,
    ReplaySettings,
    ReplayWriterRecovery,
    physical_start,
    proof_covers,
    proof_digest,
    replay_descriptors,
    reply_schema_digest,
)
from vs_project.api import Committed, StoredEnvelope, StoreFence
from vs_proto.api import (
    Change,
    Ended,
    EpochStarted,
    Lifecycle,
    Notice,
    NoticeCode,
    Outcome,
    Parked,
    Record,
    RuntimeType,
    Severity,
    Span,
    SpanRecord,
    SpanStatus,
    Stop,
    TokenUsage,
    replay_agent,
    validate,
)
from vs_proto.api import (
    ReplayStore as ReplayRecords,
)
from vs_proto.api import replay as pb
from vs_runlog.api import CurrentEpoch, current_epoch, epoch_has_ended
from vs_runtime._replay_store import REPLAY_STORE_SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_core.api.replay import ReplayFlow, ReplayFrontier, ReplayRootReuse
    from vs_runtime._replay_store import ReplayStore
    from vs_runtime._writer_reconciliation import WriterRegistry
    from vs_sim.api import Clock


@dataclass(frozen=True)
class ReplayLaunch:
    """Ordinary Resume identity and writer lease settings, supplied by composition."""

    header: EpochStarted
    request_id: str
    host_id: str
    lease_duration: float


class ReplayCommitError(RuntimeError):
    """No commit confirmation means no further dispatch from this shell."""


class ReplayRecoveryRequiredError(RuntimeError):
    """An unfinished start needs inspection by its owning executor before dispatch."""

    def __init__(self, spans: tuple[Span, ...], reasons: tuple[str, ...] = ()) -> None:
        self.spans, self.reasons = spans, reasons
        detail = "; ".join(reasons)
        super().__init__(
            f"reconcile unfinished replay operations: {', '.join(s.id for s in spans)}; {detail}"
        )


@dataclass(frozen=True)
class ReplayResult:
    """Validated executor outcome; usage is retained even when history is not reused."""

    outcome: Message
    paid: bool = False
    cost_usd: float | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class ReplayAbsent:
    """The owner proved absence and fenced off later delivery of this invocation."""


@dataclass(frozen=True)
class ReplayPending:
    """The owner found the invocation still running. Its eventual result remains inspectable."""


@dataclass(frozen=True)
class ReplayUnknown:
    """The owner cannot prove completion or absence. Blind redispatch is forbidden."""

    reason: str


@dataclass(frozen=True)
class ReplayEnding:
    """An explicit native epoch result plus the typed proof for its optional detail."""

    state: Ended
    detail: Message | None = None


class ReplayExecutor(Protocol):
    """Execute a durable start under its fence; do not invent absence on recovery.

    A repeated physical invocation must be inspected by its owning library.
    ``ReplayRuntime.dispatch`` calls this only for a start it just confirmed in this process.
    """

    async def execute(self, span: Span, fence: StoreFence) -> ReplayResult: ...

    async def inspect(
        self, span: Span, fence: StoreFence
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        """Classify the exact durable invocation; Unknown must never be reported as absent."""
        ...


@runtime_checkable
class ReplayRetirement(Protocol):
    """Settle a withdrawn physical invocation after environment-issued death proof."""

    async def retire(self, span: Span, fence: StoreFence, reuse: ReplayRootReuse) -> None: ...


class ReplayRuntime:
    """One writer drives the pure scheduler through independently durable boundaries.

    Call ``start`` with the ordinary Resume launch id. Prepare persists starts
    before returning them. Observe persists the external result; accept persists
    logical acceptance before releasing it to the flow. A crash after observe
    is completed on recovery without executing again. A start without observe
    requires explicit owning-executor reconciliation, never blind redispatch.
    """

    def __init__(
        self,
        store: ReplayStore,
        root: Callable[[ReplayFlow], Coroutine[object, object, object]],
        allowance: pb.ReplayAllowance,
    ) -> None:
        validate(allowance)
        self._store = store
        self._root = root
        self._allowance = allowance.to_binary()
        self._archive = ReplayArchive()
        self._epoch = CurrentEpoch()
        self._scheduler: ReplayScheduler | None = None
        self._fence: StoreFence | None = None
        self._revision: int | None = None
        self._halted = False
        self._paused = False
        self._dispatchable: set[str] = set()
        self._known_starts: set[str] = set()
        self._prepared_occurrences: set[tuple[bytes, bytes, int]] = set()
        self._recovery_dirty = False
        self._schema_types: set[type[Message]] = set()
        self._admission_limited = False

    @property
    def epoch(self) -> int:
        """The public writer epoch, derived by P1's shared committed-record fold."""
        return self._epoch.number

    @property
    def epoch_state(self) -> CurrentEpoch:
        """Detached native lifecycle facts, with amendments carrying no authority."""
        return current_epoch((), self._epoch)

    @property
    def archive(self) -> ReplayArchive:
        """A detached disposable projection of the last confirmed record prefix."""
        return ReplayArchive.decode(self._archive.encode())

    @property
    def _active_scheduler(self) -> ReplayScheduler:
        if self._scheduler is None or self._halted:
            problem = "replay runtime is not started or has halted"
            raise ReplayCommitError(problem)
        return self._scheduler

    @property
    def completed(self) -> bool:
        return self._active_scheduler.completed

    @property
    def result(self) -> object:
        return self._active_scheduler.result

    @property
    def clock(self) -> float:
        return self._active_scheduler.clock

    @property
    def allowance(self) -> pb.ReplayAllowance:
        return pb.ReplayAllowance.from_binary(self._active_scheduler.allowance_bytes)

    @property
    def frontier(self) -> ReplayFrontier:
        return self._active_scheduler.frontier()

    @property
    def accounting(self) -> ReplayAccounting:
        """Actual usage and iteration grants derived from the confirmed native prefix."""
        records = tuple(ReplayRecords.from_binary(self._archive.encode()).records)
        return ReplayAccounting.from_records(records, epoch=self._epoch.number)

    def start(
        self,
        launch: ReplayLaunch,
        *,
        now: float,
        at: Timestamp,
    ) -> None:
        """Acquire, validate, acknowledge the preceding notice and reconstruct the root."""
        if self._fence is not None or self._halted:
            problem = "start requires a fresh shell"
            raise ReplayCommitError(problem)
        self._fence = self._store.acquire(launch.host_id, now, launch.lease_duration)
        if self._fence is None:
            problem = "replay store lease is unavailable"
            raise ReplayCommitError(problem)
        try:
            self._load()
            self._open_epoch(launch, now=now, at=at)
            self._complete_observed(now=now, at=at)
            self._reconstruct()
        except BaseException:
            self._halted = True
            self._store.release(self._fence, now)
            raise

    def _open_epoch(self, launch: ReplayLaunch, *, now: float, at: Timestamp) -> None:
        acks = self._archive.acknowledgements.resume(launch.request_id)
        if acks == self._archive.acknowledgements:
            problem = "Resume launch already recorded; inspect its epoch instead of relaunching"
            raise ReplayCommitError(problem)
        if self._epoch.number and not epoch_has_ended(self._epoch):
            self._append(
                Oneof(
                    "lifecycle",
                    Lifecycle(
                        state=Oneof(
                            "ended",
                            Ended(
                                outcome=Outcome.INTERRUPTED,
                                summary="The epoch writer was interrupted. External work is retained for inspection.",
                                resumable=True,
                                interruption_reason="writer_restarted",
                            ),
                        )
                    ),
                ),
                now=now,
                at=at,
            )
        epoch = deepcopy(launch.header)
        epoch.epoch = self._epoch.number + 1
        epoch.identity_encoding_version = ENCODING_VERSION
        epoch.initial_allowance = pb.ReplayAllowance.from_binary(self._allowance)
        epoch.replay_acknowledgement = pb.ReplayAcknowledgement.from_binary(
            acks.acknowledgements[-1]
        )
        self._append(Oneof("epoch_started", epoch), now=now, at=at)

    def _load(self) -> None:
        stored = self._store.load()
        if stored is None:
            return
        if (
            not isinstance(stored, StoredEnvelope)
            or stored.schema_version != REPLAY_STORE_SCHEMA_VERSION
        ):
            problem = "keyed replay requires facts-only import of the retained store format"
            raise ReplayCommitError(problem)
        self._archive = ReplayArchive.decode(stored.payload)
        self._epoch = current_epoch(ReplayRecords.from_binary(stored.payload).records)
        self._revision = stored.revision
        _ = self.accounting

    def _append(
        self,
        body: Oneof,
        *,
        now: float,
        at: Timestamp,
        clock: float | None = None,
        replay_clock: pb.ReplayClock | None = None,
    ) -> Record:
        if self._halted or self._fence is None:
            problem = "replay writer has no dispatch authority"
            raise ReplayCommitError(problem)
        record = Record(
            seq=self._archive.sequence + 1,
            epoch=(body.value.epoch if body.field == "epoch_started" else self._epoch.number),
            clock=self._archive.clock if clock is None else clock,
            replay_clock=replay_clock,
            at=at,
            body=body,
        )
        candidate = self._archive.fork()
        candidate.append(record)
        stored = StoredEnvelope(
            revision=0 if self._revision is None else self._revision + 1,
            schema_version=REPLAY_STORE_SCHEMA_VERSION,
            payload=candidate.encode(),
        )
        try:
            outcome = self._store.commit(self._revision, stored, self._fence, now)
        except BaseException:
            self._halted = True
            raise
        if not isinstance(outcome, Committed):
            self._halted = True
            # Even a visible uncertain append does not authorize this process to execute.
            self._store.load()
            problem = f"replay commit not confirmed: {outcome.kind}; restart and inspect"
            raise ReplayCommitError(problem)
        self._archive, self._revision = candidate, stored.revision
        self._epoch = current_epoch((record,), self._epoch)
        return record

    def prepare(self, *, now: float, at: Timestamp) -> tuple[Span, ...]:
        """Disclose unused paid results or durably start the complete current frontier."""
        scheduler = self._active_scheduler
        if self._paused:
            return ()
        if self._recovery_dirty:
            problem = "finish recovery before preparing more work"
            raise ReplayCommitError(problem)
        if self._archive.pending_observation is not None:
            problem = "accept the pending observation before preparing more work"
            raise ReplayError(problem)
        unresolved = self.recovery
        if unresolved:
            raise ReplayRecoveryRequiredError(unresolved)
        started: list[Span] = []
        while True:
            self._record_schemas(scheduler.schema_types, now=now, at=at)
            self._record_drift(now=now, at=at)
            if self.frontier.unused and started:
                return tuple(started)
            if self._disclose_unused(now=now, at=at):
                return tuple(started)
            changed = self._prepare_operations(started, now=now, at=at)
            if not changed:
                break
        return tuple(started)

    def _prepare_operations(self, started: list[Span], *, now: float, at: Timestamp) -> bool:
        changed = False
        self._admission_limited = False
        for operation in self._active_scheduler.dispatches():
            if operation.key is None:
                problem = "scheduler proposed a start without a semantic key"
                raise ReplayError(problem)
            occurrence = (operation.key.to_binary(), operation.identity, operation.encoding_version)
            if occurrence in self._prepared_occurrences:
                continue
            retained = self._archive.pending_logical(operation)
            execution = (
                operation if retained is None or retained.replay is None else retained.replay
            )
            outcome = self.accounting.decide(execution, self.allowance)
            if isinstance(outcome, (ReplayIterationBlocked, ReplayResourcesBlocked)):
                self._admission_limited = True
                continue
            span = self._start_operation(execution, now=now, at=at)
            self._prepared_occurrences.add(occurrence)
            if outcome is None:
                started.append(span)
            else:
                self.observe(span.id, ReplayResult(outcome), now=now, at=at)
                self.accept(span.id, now=now, at=at)
                changed = True
        return changed

    def _physical_within(self, operation: pb.OperationReplay) -> float | None:
        policy = operation.policy
        if policy is None:
            return None
        if policy.has_field("physical_within_seconds"):
            return policy.physical_within_seconds
        request = policy.request
        if (
            request is not None
            and request.type_url == replay_agent.ReplayAgentRequest.desc().type_name
        ):
            native = replay_agent.ReplayAgentRequest.from_binary(request.value)
            validate(native)
            if native.action is not None and native.action.field == "turn":
                return native.within_seconds
        return None

    def _start_operation(self, operation: pb.OperationReplay, *, now: float, at: Timestamp) -> Span:
        key = operation.key
        if key is None:
            problem = "operation requires a semantic key"
            raise ReplayError(problem)
        within = self._physical_within(operation)
        parent = None if within is None else self._archive.pending_logical(operation)
        if parent is None:
            parent = Span(
                id=f"operation/{self._archive.sequence + 1}",
                runtime_type=RuntimeType.OPERATION,
                name=key.operation,
                status=SpanStatus.RUNNING,
                started_at=self._archive.clock,
                replay=operation,
            )
            self._append(
                Oneof("span", SpanRecord(change=Change.STARTED, span=parent)), now=now, at=at
            )
        span = (
            parent
            if within is None
            else self._start_physical(parent, within=within, now=now, at=at)
        )
        self._dispatchable.add(span.id)
        self._known_starts.add(span.id)
        return deepcopy(span)

    def _start_physical(self, parent: Span, *, within: float, now: float, at: Timestamp) -> Span:
        if self._fence is None:
            problem = "physical execution requires current host exclusion"
            raise ReplayCommitError(problem)
        children = self._archive.children(parent.id)
        unresolved = tuple(child for child in children if not child.has_field("ended_at"))
        if unresolved:
            raise ReplayRecoveryRequiredError(unresolved)
        prior = None if not children else children[0].replay_physical
        deadline = now + within if prior is None else prior.deadline
        child = physical_start(
            parent,
            identity=f"physical/{self._archive.current_epoch.run_id}/{self._archive.sequence + 1}",
            host=f"{self._fence.host_id}/{self._fence.epoch}",
            deadline=deadline,
            ordinal=len(children),
        )
        child.started_at = self._archive.clock
        self._append(Oneof("span", SpanRecord(change=Change.STARTED, span=child)), now=now, at=at)
        return child

    @property
    def admission_limited(self) -> bool:
        return self._admission_limited

    def park_admission_limit(self, *, now: float, at: Timestamp) -> None:
        """End at the unstarted admission frontier; denial is never a replayed answer."""
        if not self._admission_limited or self._archive.unresolved:
            problem = "admission limit can park only after admitted work settles"
            raise ReplayError(problem)
        self._append(
            Oneof(
                "notice",
                Notice(
                    severity=Severity.WARNING,
                    code=NoticeCode.FRAMEWORK_WARNING,
                    message="The epoch's allowance cannot admit the next operation. Resume with fresh allowances to continue.",
                ),
            ),
            now=now,
            at=at,
        )
        self._append(Oneof("lifecycle", Lifecycle(state=Oneof("parked", Parked()))), now=now, at=at)
        self._paused = True

    def _disclose_unused(self, *, now: float, at: Timestamp) -> bool:
        notice = self.frontier.notice()
        if notice is not None:
            if self._archive.unresolved:
                raise ReplayRecoveryRequiredError(self._archive.unresolved)
            if notice.to_binary() not in self._archive.acknowledgements.notices:
                self._append(
                    Oneof(
                        "notice",
                        Notice(
                            severity=Severity.WARNING,
                            code=NoticeCode.FRAMEWORK_WARNING,
                            message="Earlier paid results are retained but this version did not use them. Resume to continue with new work.",
                            detail=Any(type_url=notice.desc().type_name, value=notice.to_binary()),
                        ),
                    ),
                    now=now,
                    at=at,
                )
            self._append(
                Oneof("lifecycle", Lifecycle(state=Oneof("parked", Parked()))), now=now, at=at
            )
            self._paused = True
            return True
        return False

    def _record_schemas(
        self, messages: tuple[type[Message], ...], *, now: float, at: Timestamp
    ) -> None:
        messages = tuple(message for message in messages if message not in self._schema_types)
        if not messages:
            return
        schemas = replay_descriptors(*messages)
        epoch = self._archive.current_epoch
        previous = epoch.descriptors
        files = {} if previous is None else {file.name: file for file in previous.file}
        added = False
        for file in schemas.file:
            known = files.get(file.name)
            if known is not None and known.to_binary() != file.to_binary():
                problem = f"epoch descriptor {file.name} conflicts with the retained schema"
                raise ReplayError(problem)
            if known is None:
                files[file.name] = file
                added = True
        if added:
            # P1 amendments extend declarations without opening an epoch or
            # changing lifecycle, identity, allowance or launch acknowledgement.
            epoch.descriptors = type(schemas)(file=[files[name] for name in sorted(files)])
            self._append(Oneof("declaration_amended", epoch), now=now, at=at)
        self._schema_types.update(messages)

    def _record_drift(self, *, now: float, at: Timestamp) -> None:
        drift = self.frontier.drift
        if drift.count and self._archive.policy_drift is None:
            detail = pb.ReplayPolicyDrift(
                results=drift.count, roles=list(drift.roles), templates=list(drift.templates)
            )
            self._append(
                Oneof(
                    "notice",
                    Notice(
                        severity=Severity.WARNING,
                        code=NoticeCode.FRAMEWORK_WARNING,
                        message=f"{drift.count} results were produced under earlier prompts or limits. Earlier answers are kept. Roles: {', '.join(drift.roles)}. Templates: {', '.join(drift.templates)}.",
                        detail=Any(type_url=detail.desc().type_name, value=detail.to_binary()),
                    ),
                ),
                now=now,
                at=at,
            )

    async def dispatch(self, span_id: str, executor: ReplayExecutor, *, now: float) -> ReplayResult:
        """Execute once, only after this process confirmed the start and still holds its fence."""
        _ = self._active_scheduler
        if (
            self._paused
            or span_id not in self._dispatchable
            or self._fence is None
            or not self._store.verify(self._fence, now)
        ):
            problem = f"replay dispatch has no current authorization: {span_id}"
            raise ReplayCommitError(problem)
        self._dispatchable.remove(span_id)
        try:
            return await executor.execute(self._span(span_id), self._fence)
        except BaseException:
            self._halted = True
            raise

    def observe(
        self,
        span_id: str,
        result: ReplayResult,
        *,
        now: float,
        at: Timestamp,
        clock: float | None = None,
    ) -> None:
        """Retain a validated observation before attempting logical acceptance."""
        validate(result.outcome)
        cost = result.cost_usd
        if result.usage is not None:
            validate(result.usage)
            if result.usage.has_field("cost_usd"):
                if cost is not None and cost != result.usage.cost_usd:
                    problem = "executor cost contradicts its reported token usage"
                    raise ReplayError(problem)
                cost = result.usage.cost_usd
        span = self._span(span_id)
        if (
            span.replay is None
            or span.replay.policy is None
            or result.outcome.desc().type_name != span.replay.policy.reply_type
            or reply_schema_digest(type(result.outcome)) != span.replay.policy.reply_schema_digest
        ):
            problem = "observation does not match the operation's reply contract"
            raise ReplayError(problem)
        # The start owns the retained reply descriptors. A compatible recovered
        # reply may belong to a file whose unrelated declarations have changed.
        observed = pb.ReplayObservation(
            outcome=Any(type_url=result.outcome.desc().type_name, value=result.outcome.to_binary()),
            paid=result.paid,
            cost_usd=cost,
            usage=deepcopy(result.usage),
        )
        if span.replay_observation is not None:
            if span.replay_observation == observed:
                return
            problem = "physical observation cannot be replaced or removed"
            raise ReplayError(problem)
        span.replay_observation = observed
        physical = span.replay_physical
        if physical is not None:
            physical.observed_sequence = self._archive.sequence + 1
            if not physical.has_field("withdrawn_sequence"):
                span.status = SpanStatus.DONE
                span.ended_at = self._archive.clock if clock is None else clock
        self._append(
            Oneof("span", SpanRecord(change=Change.UPDATED, span=span)), now=now, at=at, clock=clock
        )
        self._dispatchable.discard(span_id)

    def accept(self, span_id: str, *, now: float, at: Timestamp) -> None:
        """Commit acceptance, then release precisely that outcome to the scheduler."""
        record = self._accept_record(self._span(span_id), now=now, at=at)
        try:
            self._active_scheduler.accept(AcceptedReplay.from_record(record))
        except BaseException:
            self._halted = True
            raise

    def _accept_record(self, span: Span, *, now: float, at: Timestamp) -> Record:
        if span.replay_physical is not None:
            winner = self._archive.eligible_child(span.replay_physical.logical)
            if winner is None or winner.id != span.id:
                problem = "only the first eligible physical child may be accepted"
                raise ReplayError(problem)
            parent = self._span(span.replay_physical.logical)
            if parent.replay is None or parent.replay.outcome is not None:
                problem = "logical operation already accepted a physical child"
                raise ReplayError(problem)
            parent.replay.accepted_child = span.id
            parent.replay_observation = deepcopy(span.replay_observation)
            span = parent
        observed, operation = span.replay_observation, span.replay
        if observed is None or operation is None:
            problem = "acceptance needs a durable observation"
            raise ReplayError(problem)
        operation.outcome = deepcopy(observed.outcome)
        operation.paid = observed.paid
        if observed.has_field("cost_usd"):
            operation.cost_usd = observed.cost_usd
        else:
            operation.clear_field("cost_usd")
        operation.accepted_sequence = self._archive.sequence + 1
        span.status, span.ended_at = SpanStatus.DONE, self._archive.clock
        return self._append(
            Oneof("span", SpanRecord(change=Change.ENDED, span=span)), now=now, at=at
        )

    def writer_intent(self, manifest: pb.WriterManifest, *, now: float, at: Timestamp) -> None:
        """W2: commit the environment's complete closed grant set before cleanup I/O."""
        self._writer_fact(manifest, now=now, at=at)

    def writer_observation(
        self, result: pb.WriterReconciliation, *, now: float, at: Timestamp
    ) -> None:
        """W7: persist proof or a typed blocked diagnostic before any hold release."""
        self._writer_fact(result, now=now, at=at)

    def _writer_fact(
        self, value: pb.WriterManifest | pb.WriterReconciliation, *, now: float, at: Timestamp
    ) -> None:
        validate(value)
        self._record_schemas((type(value),), now=now, at=at)
        records = ReplayRecords.from_binary(self._archive.encode()).records
        candidate = Record(
            body=Oneof(
                "notice",
                Notice(detail=Any(type_url=value.desc().type_name, value=value.to_binary())),
            )
        )
        ReplayWriterRecovery.from_records((*records, candidate))
        blocked = isinstance(value, pb.WriterReconciliation) and value.proof is None
        message = (
            "Recovery blocked: previous writers may still modify this workspace"
            if blocked
            else "Writer reconciliation evidence retained."
        )
        self._append(
            Oneof(
                "notice",
                Notice(
                    severity=Severity.WARNING,
                    code=NoticeCode.FRAMEWORK_WARNING,
                    message=message,
                    detail=Any(type_url=value.desc().type_name, value=value.to_binary()),
                ),
            ),
            now=now,
            at=at,
        )

    def advance_clock(self, clock: float, *, now: float, at: Timestamp) -> None:
        """Record timer progress before releasing any waiting pure continuation."""
        scheduler = self._active_scheduler
        record = self._append(
            Oneof(
                "notice",
                Notice(
                    severity=Severity.WARNING,
                    code=NoticeCode.FRAMEWORK_WARNING,
                    message="Replay clock advanced.",
                ),
            ),
            now=now,
            at=at,
            clock=clock,
            replay_clock=pb.ReplayClock(
                allowance=pb.ReplayAllowance.from_binary(scheduler.allowance_bytes)
            ),
        )
        try:
            scheduler.accept(AcceptedClock.from_record(record))
        except BaseException:
            self._halted = True
            raise

    @property
    def recovery(self) -> tuple[Span, ...]:
        """Inherited starts requiring their owner's inspection before any live dispatch."""
        _ = self._active_scheduler
        return tuple(span for span in self._archive.unresolved if span.id not in self._known_starts)

    async def inspect(
        self, span_id: str, executor: ReplayExecutor, *, now: float
    ) -> ReplayResult | ReplayAbsent | ReplayPending | ReplayUnknown:
        """Inspect under the current fence; the caller timestamps the returned observation."""
        _ = self._active_scheduler
        if self._known_starts or self._fence is None or not self._store.verify(self._fence, now):
            problem = "recovery requires a current fence before preparing live work"
            raise ReplayCommitError(problem)
        span = self._span(span_id)
        if span.replay is not None:
            internal = self.accounting.recover(span.replay)
            if internal is not None:
                return ReplayResult(internal)
        return await executor.inspect(span, self._fence)

    def recover(
        self, span_id: str, result: ReplayResult | ReplayAbsent, *, now: float, at: Timestamp
    ) -> None:
        """Retain the owner's result or confirmed absence, never a guessed retry.

        An absent invocation is stopped, so a reachable request gets a new durable
        start with today's policy. The original start remains historical.
        """
        _ = self._active_scheduler
        if self._known_starts:
            problem = "recovery must finish before preparing live work"
            raise ReplayCommitError(problem)
        span = self._span(span_id)
        self._recovery_dirty = True
        if isinstance(result, ReplayResult):
            self.observe(span_id, result, now=now, at=at)
            if span.replay_physical is None or not span.replay_physical.has_field(
                "withdrawn_sequence"
            ):
                self._accept_record(self._span(span_id), now=now, at=at)
        elif isinstance(result, ReplayAbsent):
            if span.replay_physical is not None:
                self.withdraw(span_id, now=now, at=at)
                span = self._span(span_id)
                if span.replay_physical is not None:
                    span.replay_physical.confirmed_absent = True
            span.status, span.ended_at = SpanStatus.STOPPED, self._archive.clock
            span.stop = Stop(requested_at=self._archive.clock, landed=True)
            self._append(Oneof("span", SpanRecord(change=Change.ENDED, span=span)), now=now, at=at)

        else:
            assert_never(result)

    def withdraw(self, span_id: str, *, now: float, at: Timestamp) -> None:
        """W4: durable ineligibility precedes cancellation; it proves no writer death."""
        span = self._span(span_id)
        physical = span.replay_physical
        if physical is None:
            problem = "withdrawal requires a physical child"
            raise ReplayError(problem)
        parent = self._span(physical.logical)
        if parent.replay is not None and parent.replay.accepted_child == span.id:
            problem = "an accepted child cannot be withdrawn"
            raise ReplayError(problem)
        if physical.has_field("withdrawn_sequence"):
            return
        if span.replay_observation is not None:
            problem = "a durable eligible observation must be accepted before withdrawing more work"
            raise ReplayError(problem)
        physical.withdrawn_sequence = self._archive.sequence + 1
        span.stop = Stop(requested_at=self._archive.clock, landed=span.has_field("ended_at"))
        if span.has_field("ended_at"):
            span.status = SpanStatus.STOPPED
        self._append(Oneof("span", SpanRecord(change=Change.UPDATED, span=span)), now=now, at=at)
        self._dispatchable.discard(span_id)

    def _reuse_for(self, span: Span, reuse: ReplayRootReuse) -> None:
        proof = reuse.proof()
        if proof.manifest is None or proof.manifest.backing is None:
            problem = "writer proof omitted its backing"
            raise ReplayError(problem)
        recorded = ReplayWriterRecovery.from_records(
            ReplayRecords.from_binary(self._archive.encode()).records
        ).require(proof.manifest.backing)
        if recorded != reuse:
            problem = "physical termination requires its committed environment proof"
            raise ReplayError(problem)
        proof_covers(span, proof, run_id=self._archive.current_epoch.run_id)

    async def retire(
        self,
        span_id: str,
        executor: ReplayRetirement,
        writers: WriterRegistry,
        *,
        clock: Clock,
    ) -> None:
        """Settle access under backing exclusion after its committed environment proof."""
        now = clock.now()
        at = Timestamp(seconds=int(now), nanos=int((now - int(now)) * 1_000_000_000))
        if self._fence is None or not self._store.verify(self._fence, now):
            problem = "retirement requires current host exclusion"
            raise ReplayCommitError(problem)
        reuse = ReplayWriterRecovery.from_records(
            ReplayRecords.from_binary(self._archive.encode()).records
        ).require(writers.backing)
        with writers.acquire(reuse, run_id=self._archive.current_epoch.run_id, fence=self._fence):
            span = self._span(span_id)
            self._reuse_for(span, reuse)
            if span.replay_physical is None or not span.replay_physical.has_field(
                "withdrawn_sequence"
            ):
                problem = "commit withdrawal before stopping a writer"
                raise ReplayError(problem)
            if not span.replay_physical.has_field("settlement_sequence"):
                span.replay_physical.settlement_sequence = self._archive.sequence + 1
                span.replay_physical.termination_proof_digest = proof_digest(reuse.proof())
                self._append(
                    Oneof("span", SpanRecord(change=Change.UPDATED, span=span)), now=now, at=at
                )
            try:
                await executor.retire(span, self._fence, reuse)
                now = clock.now()
                at = Timestamp(seconds=int(now), nanos=int((now - int(now)) * 1_000_000_000))
                self._confirm_stopped(span_id, reuse, now=now, at=at)
            except BaseException:
                self._halted = True
                raise

    def _confirm_stopped(
        self, span_id: str, reuse: ReplayRootReuse, *, now: float, at: Timestamp
    ) -> None:
        span = self._span(span_id)
        self._reuse_for(span, reuse)
        physical = span.replay_physical
        if physical is None or not physical.has_field("withdrawn_sequence"):
            problem = "commit withdrawal before confirming writer termination"
            raise ReplayError(problem)
        # The settlement intent retains its historical death witness. Current
        # reuse authority was checked above and may belong to a later generation.
        if span.has_field("ended_at"):
            return
        span.status = SpanStatus.STOPPED
        if not span.has_field("ended_at"):
            span.ended_at = self._archive.clock
        span.stop = Stop(
            requested_at=span.stop.requested_at if span.stop is not None else self._archive.clock,
            landed=True,
        )
        self._append(Oneof("span", SpanRecord(change=Change.ENDED, span=span)), now=now, at=at)
        self._recovery_dirty = True

    def finish_recovery(self) -> None:
        """Reconstruct the root from inspected outcomes before permitting fresh work."""
        if self.recovery:
            raise ReplayRecoveryRequiredError(self.recovery)
        if not self._recovery_dirty:
            return
        self._active_scheduler.close()
        self._reconstruct()
        self._recovery_dirty = False

    def _reconstruct(self) -> None:
        try:
            self._scheduler = ReplayScheduler(
                self._root,
                self._archive.history,
                ReplaySettings(
                    initial_clock=0,
                    initial_allowance=self._archive.initial_allowance.to_binary(),
                    fresh_allowance=self._allowance,
                ),
                self._archive.acknowledgements,
            )
        except BaseException:
            self._halted = True
            raise

    def _complete_observed(self, *, now: float, at: Timestamp) -> None:
        pending = self._archive.pending_observation
        if pending is not None and pending.replay_physical is not None:
            self._accept_record(pending, now=now, at=at)
        for span in self._archive.unresolved:
            if span.replay_physical is None and span.replay_observation is not None:
                self._accept_record(span, now=now, at=at)

    def _span(self, span_id: str) -> Span:
        span = self._archive.span(span_id)
        if span.replay is None:
            problem = f"span is not a keyed operation: {span_id}"
            raise ReplayError(problem)
        return span

    def finish(self, ending: ReplayEnding, *, now: float, at: Timestamp) -> None:
        """Retain a completed epoch's explicit policy result; every replay run stays resumable."""
        scheduler = self._active_scheduler
        state = deepcopy(ending.state)
        if ending.detail is not None:
            validate(ending.detail)
            packed = Any(type_url=ending.detail.desc().type_name, value=ending.detail.to_binary())
            if state.detail is not None and state.detail != packed:
                problem = "ending detail contradicts its typed value"
                raise ReplayError(problem)
            state.detail = packed
        elif state.detail is not None:
            problem = "ending detail requires its typed value"
            raise ReplayError(problem)
        validate(state)
        if not state.resumable:
            problem = "replay epochs must remain resumable"
            raise ReplayError(problem)
        if (
            not scheduler.completed
            or scheduler.frontier().unused
            or self._archive.unresolved
            or self._archive.epoch_closed
        ):
            problem = "an epoch can finish only after replay, disclosure and owned work settle"
            raise ReplayError(problem)
        self._record_schemas(scheduler.schema_types, now=now, at=at)
        self._record_drift(now=now, at=at)
        if ending.detail is not None:
            self._record_schemas((type(ending.detail),), now=now, at=at)
        self._append(Oneof("lifecycle", Lifecycle(state=Oneof("ended", state))), now=now, at=at)

    def clock_requests(self) -> tuple[float, ...]:
        """Pending flow deadlines, released only through durable clock observations."""
        return self._active_scheduler.clock_requests()

    @property
    def paused(self) -> bool:
        """The current epoch ended at an unused-results notice."""
        _ = self._active_scheduler
        return self._paused

    def renew(self, *, now: float, duration: float) -> None:
        """Renew the writer while executor calls are in flight; uncertainty halts dispatch."""
        _ = self._active_scheduler
        if self._fence is None:
            problem = "replay writer has no lease to renew"
            raise ReplayCommitError(problem)
        try:
            renewed = self._store.renew(self._fence, now, duration)
        except BaseException:
            self._halted = True
            raise
        if renewed is None:
            self._halted = True
            problem = "replay writer lease was lost"
            raise ReplayCommitError(problem)
        self._fence = renewed

    def close(self, *, now: float) -> None:
        """Discard local continuations and release ownership; retained starts remain unfinished."""
        self._halted = True
        try:
            if self._scheduler is not None:
                self._scheduler.close()
        finally:
            if self._fence is not None:
                self._store.release(self._fence, now)
