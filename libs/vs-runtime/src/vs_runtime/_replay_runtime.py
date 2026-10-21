"""Fenced recorder for the keyed scheduler, using today's atomic StateStore."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, assert_never

from protobuf import Message, Oneof
from protobuf.wkt.google.protobuf.any_pb import Any

from vs_core.api.replay import (
    ENCODING_VERSION,
    AcceptedClock,
    AcceptedReplay,
    ReplayArchive,
    ReplayError,
    ReplayScheduler,
    ReplaySettings,
)
from vs_project.api import Committed, StateStore, StoredEnvelope, StoreFence
from vs_proto.api import (
    Change,
    EpochStarted,
    Lifecycle,
    Notice,
    NoticeCode,
    Parked,
    Record,
    RuntimeType,
    Severity,
    Span,
    SpanRecord,
    SpanStatus,
    Stop,
    validate,
)
from vs_proto.api import replay as pb

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

    from vs_core.api.replay import ReplayFlow, ReplayFrontier

# A separate envelope codec fences old CoreRuntime readers before they can dispatch.
REPLAY_STORE_SCHEMA_VERSION = 2


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


@dataclass(frozen=True)
class ReplayAbsent:
    """The owning executor proved that the invocation never crossed its boundary."""


@dataclass(frozen=True)
class ReplayPending:
    """The owner found the invocation still running. Its eventual result remains inspectable."""


@dataclass(frozen=True)
class ReplayUnknown:
    """The owner cannot prove completion or absence. Blind redispatch is forbidden."""

    reason: str


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
        store: StateStore,
        root: Callable[[ReplayFlow], Coroutine[object, object, object]],
        allowance: pb.ReplayAllowance,
    ) -> None:
        validate(allowance)
        self._store = store
        self._root = root
        self._allowance = allowance.to_binary()
        self._archive = ReplayArchive()
        self._scheduler: ReplayScheduler | None = None
        self._fence: StoreFence | None = None
        self._revision: int | None = None
        self._halted = False
        self._paused = False
        self._dispatchable: set[str] = set()
        self._known_starts: set[str] = set()
        self._prepared_occurrences: set[tuple[bytes, bytes, int]] = set()
        self._recovery_dirty = False

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
        epochs = self._archive.epochs
        if any(
            e.replay_acknowledgement is not None
            and e.replay_acknowledgement.launch_request_id == launch.request_id
            for e in epochs
        ):
            problem = "Resume launch already recorded; inspect its epoch instead of relaunching"
            raise ReplayCommitError(problem)
        acks = self._archive.acknowledgements.resume(launch.request_id)
        epoch = deepcopy(launch.header)
        epoch.epoch = len(epochs) + 1
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
        self._revision = stored.revision

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
            epoch=(
                body.value.epoch if body.field == "epoch_started" else len(self._archive.epochs)
            ),
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
        self._record_drift(now=now, at=at)
        notice = scheduler.frontier().notice()
        if notice is not None:
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
            return ()
        unresolved = tuple(
            span for span in self._archive.unresolved if span.id not in self._known_starts
        )
        if unresolved:
            raise ReplayRecoveryRequiredError(unresolved)
        started = []
        for operation in scheduler.dispatches():
            key = operation.key
            if key is None:
                problem = "scheduler proposed a start without a semantic key"
                raise ReplayError(problem)
            occurrence = (key.to_binary(), operation.identity, operation.encoding_version)
            if occurrence in self._prepared_occurrences:
                continue
            span = Span(
                id=f"operation/{self._archive.sequence + 1}",
                runtime_type=RuntimeType.OPERATION,
                name=key.operation,
                status=SpanStatus.RUNNING,
                started_at=self._archive.clock,
                replay=operation,
            )
            self._append(
                Oneof("span", SpanRecord(change=Change.STARTED, span=span)), now=now, at=at
            )
            self._dispatchable.add(span.id)
            self._known_starts.add(span.id)
            self._prepared_occurrences.add(occurrence)
            started.append(deepcopy(span))
        return tuple(started)

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
        span = self._span(span_id)
        span.replay_observation = pb.ReplayObservation(
            outcome=Any(type_url=result.outcome.desc().type_name, value=result.outcome.to_binary()),
            paid=result.paid,
            cost_usd=result.cost_usd,
        )
        self._append(
            Oneof("span", SpanRecord(change=Change.UPDATED, span=span)), now=now, at=at, clock=clock
        )

    def accept(self, span_id: str, *, now: float, at: Timestamp) -> None:
        """Commit acceptance, then release precisely that outcome to the scheduler."""
        record = self._accept_record(self._span(span_id), now=now, at=at)
        try:
            self._active_scheduler.accept(AcceptedReplay.from_record(record))
        except BaseException:
            self._halted = True
            raise

    def _accept_record(self, span: Span, *, now: float, at: Timestamp) -> Record:
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
        return await executor.inspect(self._span(span_id), self._fence)

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
            self._accept_record(self._span(span_id), now=now, at=at)
        elif isinstance(result, ReplayAbsent):
            span.status, span.ended_at = SpanStatus.STOPPED, self._archive.clock
            span.stop = Stop(requested_at=self._archive.clock, landed=True)
            self._append(Oneof("span", SpanRecord(change=Change.ENDED, span=span)), now=now, at=at)

        else:
            assert_never(result)

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
        for span in self._archive.unresolved:
            if span.replay_observation is not None:
                self._accept_record(span, now=now, at=at)

    def _span(self, span_id: str) -> Span:
        for span in self._archive.spans:
            if span.id == span_id:
                if span.replay is None:
                    problem = f"span is not a keyed operation: {span_id}"
                    raise ReplayError(problem)
                return span
        problem = f"unknown replay span {span_id}"
        raise ReplayError(problem)

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
