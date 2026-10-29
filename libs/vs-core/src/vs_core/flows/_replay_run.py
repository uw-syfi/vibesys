"""One deterministic scheduler for live acceptance and frozen-prefix reconstruction."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, TypeVar, cast

from protobuf import Message
from protobuf.wkt.google.protobuf.any_pb import Any

from vs_proto.api import replay as pb
from vs_proto.api import validate

from ._replay_causality import EMPTY_FRONTIER, CausalGraph, _Frontier
from ._replay_history import (
    AcceptedClock,
    AcceptedReplay,
    PolicyDrift,
    ReplayAcknowledgements,
    ReplayFrontier,
    replay_operation,
)
from ._replay_identity import (
    ClassifiedContext,
    EncodedIdentity,
    ReplayIdentityError,
    SemanticKey,
    _normalize,
    classify_context,
    encode_identity,
)
from ._replay_schema import replay_descriptors, reply_schema_digest
from ._replay_sources import (
    ReplayCausalityError,
    ReplayChannel,
    ReplayConsumerError,
    ReplayDeadlockError,
    ReplayDivergenceError,
    ReplayError,
    ReplayFlow,
    ReplayGroup,
    ReplayLatch,
    ReplaySource,
    ReplayTask,
    ReplayTimer,
    _All,
    _Delivery,
    _First,
    _Operation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable, Sequence

    from protobuf.wkt.google.protobuf.descriptor_pb import FileDescriptorSet

    from ._replay_sources import ReplayCall


def _call_identity(call: ReplayCall, frontier: _Frontier) -> EncodedIdentity:
    if call.policy.template and not isinstance(call.identity, ClassifiedContext):
        problem = f"prompt {call.key.operation} requires a classified typed context"
        raise ReplayIdentityError(problem)
    if isinstance(call.identity, ClassifiedContext):
        return classify_context(
            call.identity.message(), contract=call.contract, dependencies=frontier.digest
        ).identity
    return encode_identity(call.identity, contract=call.contract, dependencies=frontier.digest)


def _call_policy(call: ReplayCall) -> pb.ReplayPolicy:
    if not isinstance(call.reply, type) or not issubclass(call.reply, Message):
        problem = "replay reply must be a protobuf message type"
        raise ReplayConsumerError(problem)
    policy = deepcopy(call.policy)
    request = _normalize(
        call.identity.message() if isinstance(call.identity, ClassifiedContext) else call.identity
    )
    retained = Any(type_url=request.desc().type_name, value=request.to_binary())
    if policy.request is not None and policy.request != retained:
        problem = "retained request contradicts the typed operation input"
        raise ReplayIdentityError(problem)
    policy.request = retained
    digest = reply_schema_digest(call.reply)
    if policy.has_field("reply_schema_digest") and policy.reply_schema_digest != digest:
        problem = "reply schema proof contradicts typed consumer"
        raise ReplayConsumerError(problem)
    policy.reply_schema_digest = digest
    validate(policy)
    if policy.reply_type != call.reply.desc().type_name:
        problem = "replay policy reply type contradicts typed consumer"
        raise ReplayConsumerError(problem)
    return policy


T = TypeVar("T")
Occurrence = tuple[SemanticKey, EncodedIdentity]


@dataclass(eq=False)
class _Pending:
    key: SemanticKey
    identity: EncodedIdentity
    frontier: _Frontier
    policy_bytes: bytes
    reply: type[Message]
    outcome: _Delivery | None = None
    registered: bool = False
    owners: set[int] = field(default_factory=set)
    joined_policies: set[bytes] = field(default_factory=set)


@dataclass(frozen=True)
class ReplaySettings:
    """Historical initial reads and fresh frontier allowance are separate inputs."""

    initial_clock: float
    initial_allowance: bytes
    fresh_allowance: bytes

    def __post_init__(self) -> None:
        if not isfinite(self.initial_clock) or self.initial_clock < 0:
            problem = "initial replay clock must be finite and nonnegative"
            raise ReplayError(problem)
        validate(pb.ReplayAllowance.from_binary(self.initial_allowance))
        validate(pb.ReplayAllowance.from_binary(self.fresh_allowance))


class ReplayScheduler:
    """Drive pure coroutines to quiescence between each serial accepted observation.

    The object is disposable epoch-local execution state. Only protobuf history,
    notices and acknowledgement relations are durable. Construction never dispatches.
    Shells must persist every returned start and accepted observation before I/O or
    delivery respectively. Foreign awaitables and orphan waits are diagnostics.
    """

    def __init__(
        self,
        root: Callable[[ReplayFlow], Coroutine[object, object, object]],
        history: Sequence[AcceptedReplay | AcceptedClock],
        settings: ReplaySettings,
        acknowledgements: ReplayAcknowledgements | None = None,
    ) -> None:
        self.clock = settings.initial_clock
        self.allowance_bytes = settings.initial_allowance
        self._fresh_allowance = settings.fresh_allowance
        self._acks = acknowledgements or ReplayAcknowledgements()
        self._tasks: list[ReplayTask] = []
        self._causal = CausalGraph()
        self._released_frontier = EMPTY_FRONTIER
        self._current: ReplayTask | None = None
        self._timers: list[ReplayTimer] = []
        self._pending: dict[Occurrence, _Pending] = {}
        self._schema_types: dict[type[Message], None] = {}
        self._sources: dict[_Operation, _Pending] = {}
        self._active: dict[SemanticKey, _Pending] = {}
        self._past: set[Occurrence] = set()
        self._consumed: set[int] = set()
        self._drift_count = 0
        self._drift_roles: set[str] = set()
        self._drift_templates: set[str] = set()
        self._history = tuple(sorted(history, key=lambda item: item.sequence))
        self._last_sequence = 0
        self._last_clock = settings.initial_clock
        self._reconstructing = True
        try:
            self._reconstruct(root)
        except BaseException:
            self.close()
            raise

    def _reconstruct(self, root: Callable[[ReplayFlow], Coroutine[object, object, object]]) -> None:
        self._validate_history()
        self._create(root, EMPTY_FRONTIER)
        self._quiesce()
        for accepted in self._history:
            self._release(accepted)
        self._reconstructing = False
        self.allowance_bytes = self._fresh_allowance
        self._check_deadlock()

    def _validate_history(self) -> None:
        seen: dict[Occurrence, bytes] = {}
        sequence = 0
        clock = self.clock
        for accepted in self._history:
            if accepted.sequence <= sequence or accepted.clock < clock:
                problem = "accepted replay sequences must be unique and clocks monotone"
                raise ReplayError(problem)
            if isinstance(accepted, AcceptedClock):
                sequence, clock = accepted.sequence, accepted.clock
                continue
            occurrence = (accepted.key, accepted.identity)
            if occurrence in seen:
                problem = "one logical occurrence has multiple accepted outcomes"
                raise ReplayError(problem)
            seen[occurrence] = accepted.operation_bytes
            sequence, clock = accepted.sequence, accepted.clock

    def _create(
        self, flow: Callable[[ReplayFlow], Coroutine[object, object, T]], causal: _Frontier
    ) -> ReplayTask[T]:
        task = ReplayTask(len(self._tasks), flow(ReplayFlow(self)), causal)
        self._tasks.append(task)
        return task

    def start_child(
        self, flow: Callable[[ReplayFlow], Coroutine[object, object, T]]
    ) -> ReplayTask[T]:
        return self._create(flow, self.current_causality())

    def current_causality(self) -> _Frontier:
        if self._current is None:
            problem = "flow primitive used outside a running continuation"
            raise ReplayError(problem)
        return self._current.causality

    def observe(self, frontier: _Frontier) -> None:
        """Include a synchronous latch read in the current continuation's provenance."""
        if self._current is None:
            problem = "latch value read outside a running continuation"
            raise ReplayError(problem)
        self._current.causality = self._causal.merge(self._current.causality, frontier)

    def observe_absence(self) -> None:
        """An unset shared latch reflects all observations already released to the run."""
        self.observe(self._released_frontier)

    @property
    def result(self) -> object:
        root = self._tasks[0]
        if root.result is None:
            problem = "root has not completed"
            raise ReplayError(problem)
        return root.result.value

    @property
    def completed(self) -> bool:
        return all(task.result is not None for task in self._tasks)

    def frontier(self) -> ReplayFrontier:
        disclosed = self._acks.disclosed()
        unused = tuple(
            item
            for item in self._history
            if isinstance(item, AcceptedReplay)
            and item.sequence not in self._consumed
            and item.paid
            and item.occurrence not in disclosed
        )
        drift = PolicyDrift(
            self._drift_count,
            tuple(sorted(self._drift_roles)),
            tuple(sorted(self._drift_templates)),
        )
        return ReplayFrontier(unused, tuple(pending.key for pending in self._waiting()), drift)

    @property
    def schema_types(self) -> tuple[type[Message], ...]:
        """Reached request and reply classes, in deterministic first-use order."""
        return tuple(self._schema_types)

    @property
    def schemas(self) -> FileDescriptorSet:
        """All typed inputs and consumers reached by this root, with transitive dependencies."""
        return replay_descriptors(*self._schema_types)

    def dispatches(self) -> tuple[pb.OperationReplay, ...]:
        """Return starts only at a disclosed frontier; this method performs no I/O."""
        if self._reconstructing or self.frontier().unused:
            return ()
        return tuple(self._start(pending) for pending in self._waiting())

    def _start(self, pending: _Pending) -> pb.OperationReplay:
        operation = replay_operation(
            pending.key, pending.identity, pb.ReplayPolicy.from_binary(pending.policy_bytes)
        )
        operation.allowance = pb.ReplayAllowance.from_binary(self.allowance_bytes)
        return operation

    def _awaited_sources(self) -> Iterable[ReplaySource]:
        for task in self._tasks:
            stack = [task.waiting] if task.waiting is not None and task.result is None else []
            while stack:
                source = stack.pop()
                if isinstance(source, _First):
                    stack.extend(reversed(source.sources))
                else:
                    yield source

    def _has_waiter(self, pending: _Pending) -> bool:
        return any(
            self._sources.get(source) is pending
            for source in self._awaited_sources()
            if isinstance(source, _Operation)
        )

    def _registered(self, pending: _Pending) -> bool:
        return pending.registered and any(
            self._tasks[owner].result is None for owner in pending.owners
        )

    def _waiting(self) -> Iterable[_Pending]:
        active = {
            self._sources[source]
            for source in self._awaited_sources()
            if isinstance(source, _Operation)
        }
        return (
            pending
            for pending in self._pending.values()
            if pending.outcome is None and pending in active
        )

    def accept(self, accepted: AcceptedReplay | AcceptedClock) -> None:
        """Deliver a durably accepted live result using the same release step as replay."""
        if isinstance(accepted, AcceptedClock):
            self._release(accepted)
            self._history = (*self._history, accepted)
            self._check_deadlock()
            return
        occurrence = (accepted.key, accepted.identity)
        if occurrence in self._past:
            problem = "one logical occurrence has multiple accepted outcomes"
            raise ReplayError(problem)
        if occurrence not in self._pending or self._pending[occurrence].outcome is not None:
            problem = "live acceptance has no pending logical operation"
            raise ReplayError(problem)
        self._release(accepted)
        self._history = (*self._history, accepted)
        self._check_deadlock()

    def _release(self, accepted: AcceptedReplay | AcceptedClock) -> None:
        if accepted.sequence <= self._last_sequence or accepted.clock < self._last_clock:
            problem = "acceptance must advance log sequence and monotone Record.clock"
            raise ReplayError(problem)
        self._last_sequence, self._last_clock = accepted.sequence, accepted.clock
        if isinstance(accepted, AcceptedClock):
            self._release_clock(accepted)
            return
        occurrence = (accepted.key, accepted.identity)
        pending = self._pending.get(occurrence)
        self._past.add(occurrence)
        if pending is None or not self._registered(pending):
            return
        operation = accepted.operation()
        reply = operation.outcome
        if reply is None or reply.type_url.rsplit("/", 1)[-1] != pending.reply.desc().type_name:
            problem = "historical reply consumer is incompatible; bump the semantic data contract"
            raise ReplayConsumerError(problem)
        policy = pb.ReplayPolicy.from_binary(pending.policy_bytes)
        if operation.policy is None:
            problem = "historical operation has no policy"
            raise ReplayError(problem)
        if policy.reply_schema_digest != operation.policy.reply_schema_digest:
            problem = "historical reply schema is incompatible; bump the semantic data contract"
            raise ReplayConsumerError(problem)
        value = pending.reply.from_binary(reply.value)
        validate(value)
        self._policy_drift(pending, operation.policy)
        self.clock = accepted.clock
        if operation.allowance is None:
            problem = "historical operation has no positional allowance"
            raise ReplayError(problem)
        self.allowance_bytes = operation.allowance.to_binary()
        self._released_frontier = self._causal.append(self._released_frontier, accepted.token)
        pending.outcome = _Delivery(value, self._causal.append(pending.frontier, accepted.token))
        self._active.pop(pending.key, None)
        self._consumed.add(accepted.sequence)
        self._quiesce()

    def _policy_drift(self, pending: _Pending, historical: pb.ReplayPolicy) -> None:
        policies = {pending.policy_bytes, *pending.joined_policies}
        if policies == {historical.to_binary()}:
            return
        self._drift_count += 1
        for value in (*policies, historical.to_binary()):
            policy = pb.ReplayPolicy.from_binary(value)
            if policy.role:
                self._drift_roles.add(policy.role)
            if policy.template:
                self._drift_templates.add(policy.template)

    def _register(self, source: ReplaySource, task: ReplayTask) -> None:
        if isinstance(source, _Operation):
            self._register_operation(source, task)
        elif isinstance(source, _First):
            for child in source.sources:
                self._register(child, task)
        elif isinstance(source, _All):
            self._owned_tasks(source.tasks)
        elif isinstance(source, ReplayTask):
            self._owned_tasks((source,))
        elif isinstance(source, ReplayTimer):
            self._register_timer(source, task)
        elif isinstance(source, (ReplayChannel, ReplayLatch, ReplayGroup)):
            if source.scheduler is not self:
                problem = "source belongs to another replay scheduler"
                raise ReplayError(problem)
        else:
            problem = "foreign awaitable in replay flow"
            raise ReplayError(problem)

    def _register_operation(self, source: _Operation, task: ReplayTask) -> None:
        pending = self._sources.get(source)
        if pending is None or (
            pending.outcome is None
            and (not pending.registered or (pending.key, pending.identity) in self._past)
        ):
            self._sources.pop(source, None)
            self._register_call(source, task)
        else:
            pending.owners.add(task.ordinal)

    def _register_timer(self, source: ReplayTimer, task: ReplayTask) -> None:
        if source not in self._timers:
            problem = "timer belongs to another replay scheduler"
            raise ReplayError(problem)
        source.registered = True
        if source.deadline <= self.clock and source.result is None:
            source.result = _Delivery(self.clock, task.causality)

    def _owned_tasks(self, tasks: Sequence[ReplayTask]) -> None:
        for task in tasks:
            if task.ordinal >= len(self._tasks) or self._tasks[task.ordinal] is not task:
                problem = "task belongs to another replay scheduler"
                raise ReplayError(problem)

    def _register_call(self, source: _Operation, task: ReplayTask) -> None:
        call = source.call
        identity = _call_identity(call, task.causality)
        occurrence = (call.key, identity)
        if occurrence in self._past:
            problem = f"late exact request behind replay cursor: {call.key}"
            raise ReplayCausalityError(problem)
        active = self._active.get(call.key)
        if active is not None and not self._registered(active) and not self._has_waiter(active):
            active = None
        if active is not None and active.identity != identity:
            problem = f"concurrent divergent identities for semantic key {call.key}"
            raise ReplayDivergenceError(problem)
        policy = _call_policy(call)
        request = (
            call.identity.message()
            if isinstance(call.identity, ClassifiedContext)
            else call.identity
        )
        self._schema_types[type(request)] = None
        self._schema_types[call.reply] = None
        pending = self._pending.get(occurrence)
        if pending is None:
            pending = _Pending(
                call.key,
                identity,
                task.causality,
                policy.to_binary(),
                cast("type[Message]", call.reply),
            )
            self._pending[occurrence] = pending
        elif pending.reply is not call.reply:
            problem = "equal concurrent occurrences have incompatible reply consumers"
            raise ReplayConsumerError(problem)
        if policy.to_binary() != pending.policy_bytes:
            pending.joined_policies.add(policy.to_binary())
        pending.owners.add(task.ordinal)
        self._sources[source] = pending
        self._active[call.key] = pending

    def _ready(self, source: ReplaySource) -> bool:
        if isinstance(source, _Operation):
            return not source.consumed and self._sources[source].outcome is not None
        if isinstance(source, (ReplayTask, ReplayLatch, ReplayTimer)):
            return not source.consumed and source.result is not None
        if isinstance(source, (ReplayChannel, ReplayGroup)):
            return bool(source.queue if isinstance(source, ReplayChannel) else source.results)
        if isinstance(source, _First):
            return any(self._ready(child) for child in source.sources)
        if isinstance(source, _All):
            return not source.consumed and all(child.result is not None for child in source.tasks)
        return False

    def _take(self, source: ReplaySource) -> _Delivery:
        if isinstance(source, _Operation):
            source.consumed = True
            delivered = cast("_Delivery", self._sources[source].outcome)
            value = delivered.value
            return _Delivery(type(value).from_binary(value.to_binary()), delivered.causality)
        if isinstance(source, (ReplayTask, ReplayLatch, ReplayTimer)):
            source.consumed = True
            return cast("_Delivery", source.result)
        if isinstance(source, ReplayChannel):
            return source.queue.popleft()
        if isinstance(source, ReplayGroup):
            return source.results.popleft()
        if isinstance(source, _First):
            index = next(i for i, child in enumerate(source.sources) if self._ready(child))
            delivered = self._take(source.sources[index])
            return _Delivery((index, delivered.value), delivered.causality)
        if isinstance(source, _All):
            results = tuple(cast("_Delivery", task.result) for task in source.tasks)
            source.consumed = True
            causal = EMPTY_FRONTIER
            for result in results:
                causal = self._causal.merge(causal, result.causality)
            return _Delivery(tuple(result.value for result in results), causal)
        problem = "unsupported replay source"
        raise ReplayError(problem)

    def _quiesce(self) -> None:
        while True:
            task = next(
                (
                    task
                    for task in self._tasks
                    if task.result is None
                    and (
                        not task.started or (task.waiting is not None and self._ready(task.waiting))
                    )
                ),
                None,
            )
            if task is None:
                # A parked request remains registered even when a first() winner
                # moves its consumer elsewhere. Its retained answer can be awaited
                # later, just like a buffered channel or completed child result.
                for source in self._awaited_sources():
                    if isinstance(source, _Operation):
                        self._sources[source].registered = True
                return
            self._advance(task)

    def _advance(self, task: ReplayTask) -> None:
        self._current = task
        try:
            if task.started:
                delivery = self._take(cast("ReplaySource", task.waiting))
                task.causality = self._causal.merge(task.causality, delivery.causality)
                source = task.coroutine.send(delivery)
            else:
                task.started = True
                source = task.coroutine.send(None)
            if not isinstance(source, ReplaySource):
                problem = "foreign awaitable in replay continuation"
                raise ReplayError(problem)
            task.waiting = source
            self._register(source, task)
        except StopIteration as completed:
            task.waiting = None
            task.result = _Delivery(completed.value, task.causality)
            if task.group is not None:
                task.group.results.append(task.result)
        finally:
            self._current = None

    def timer(self, deadline: float) -> ReplayTimer:
        if not isfinite(deadline) or deadline < 0:
            problem = "timer deadline must be finite and nonnegative"
            raise ReplayError(problem)
        timer = ReplayTimer(deadline)
        self._timers.append(timer)
        return timer

    def _awaited_timers(self) -> tuple[ReplayTimer, ...]:
        return tuple(
            source for source in self._awaited_sources() if isinstance(source, ReplayTimer)
        )

    def clock_requests(self) -> tuple[float, ...]:
        """Deadlines whose firings must return as recorded serial clock observations."""
        return tuple(
            sorted({timer.deadline for timer in self._awaited_timers() if timer.result is None})
        )

    def _release_clock(self, accepted: AcceptedClock) -> None:
        ready = [
            timer
            for timer in self._awaited_timers()
            if timer.result is None and timer.deadline <= accepted.clock
        ]
        if not ready:
            return
        self.clock = accepted.clock
        self.allowance_bytes = accepted.allowance_bytes
        self._released_frontier = self._causal.append(self._released_frontier, accepted.token)
        for timer in ready:
            timer.result = _Delivery(
                accepted.clock, self._causal.append(EMPTY_FRONTIER, accepted.token)
            )
        self._quiesce()

    def _check_deadlock(self) -> None:
        if (
            not self.completed
            and not any(True for _ in self._waiting())
            and not self.clock_requests()
        ):
            problem = "replay branches wait on channels, latches or children with no producer"
            raise ReplayDeadlockError(problem)

    def close(self) -> None:
        """Discard epoch-local coroutines; no I/O or historical state is changed."""
        for task in reversed(self._tasks):
            task.coroutine.close()
