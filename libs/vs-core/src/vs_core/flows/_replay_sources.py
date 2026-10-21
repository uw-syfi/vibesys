"""Pure suspension vocabulary for the acceptance-ordered scheduler."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from vs_proto.api import replay as pb

from ._replay_identity import EMPTY_CAUSALITY, SemanticKey

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator

    from protobuf import Message

    from ._replay_run import ReplayScheduler

T = TypeVar("T")


class ReplayError(ValueError):
    """Invalid replay history or a nondeterministic flow program."""


class ReplayDivergenceError(ReplayError):
    """One epoch concurrently asked for different identities under one key."""


class ReplayCausalityError(ReplayError):
    """A late exact request would reorder the accepted historical subsequence."""


class ReplayDeadlockError(ReplayError):
    """No unfinished operation can release a blocked flow."""


class ReplayConsumerError(ReplayError):
    """A current consumer would reinterpret a retained historical reply."""


@dataclass(frozen=True)
class ReplayCall(Generic[T]):
    """Typed semantic request. Prompts and execution limits are retained policy."""

    key: SemanticKey
    identity: Message
    contract: str
    policy: pb.ReplayPolicy
    reply: type[T]


@dataclass(frozen=True)
class _Delivery(Generic[T]):
    value: T
    causality: bytes


class ReplaySource(Generic[T]):
    """Only these sources may suspend a replay continuation."""

    def __await__(self) -> Generator[ReplaySource[T], _Delivery[T], T]:
        delivered = yield self
        return delivered.value


@dataclass(eq=False)
class _Operation(ReplaySource[T]):
    call: ReplayCall[T]


@dataclass(eq=False)
class ReplayTask(ReplaySource[T]):
    """A child result carries the child's entire causal frontier to its consumer."""

    ordinal: int
    coroutine: Coroutine[object, object, T]
    causality: bytes = EMPTY_CAUSALITY
    waiting: ReplaySource | None = None
    result: _Delivery[T] | None = None
    started: bool = False


@dataclass(eq=False)
class ReplayChannel(ReplaySource[T]):
    """FIFO sends retain the sender's causal frontier."""

    scheduler: ReplayScheduler
    queue: deque[_Delivery[T]] = field(default_factory=deque)

    def send(self, value: T) -> None:
        self.queue.append(_Delivery(value, self.scheduler.current_causality()))


@dataclass(eq=False)
class ReplayLatch(ReplaySource[T]):
    """A single assignment delivers the setter's causal frontier."""

    scheduler: ReplayScheduler
    result: _Delivery[T] | None = None

    def set(self, value: T) -> None:
        if self.result is not None:
            problem = "a replay latch may only be set once"
            raise ReplayError(problem)
        self.result = _Delivery(value, self.scheduler.current_causality())


@dataclass(eq=False)
class ReplayTimer(ReplaySource[float]):
    """Only a recorded clock observation fires timers; worker timestamps cannot."""

    deadline: float
    result: _Delivery[float] | None = None


@dataclass(eq=False)
class _First(ReplaySource[tuple[int, object]]):
    sources: tuple[ReplaySource, ...]


@dataclass(eq=False)
class _All(ReplaySource[tuple[object, ...]]):
    tasks: tuple[ReplayTask, ...]


@dataclass
class ReplayGroup:
    """Children inherit causal context at creation; joins retain child provenance."""

    scheduler: ReplayScheduler
    tasks: list[ReplayTask] = field(default_factory=list)

    def start(self, flow: Callable[[ReplayFlow], Coroutine[object, object, T]]) -> ReplayTask[T]:
        task = self.scheduler.start_child(flow)
        self.tasks.append(task)
        return task

    def all(self) -> ReplaySource[tuple[object, ...]]:
        return _All(tuple(self.tasks))


@dataclass(frozen=True)
class ReplayFlow:
    """Pure flow context. Reads observe the last released historical observation."""

    scheduler: ReplayScheduler

    def call(self, call: ReplayCall[T]) -> ReplaySource[T]:
        return _Operation(call)

    def clock(self) -> float:
        return self.scheduler.clock

    def allowance(self) -> pb.ReplayAllowance:
        return pb.ReplayAllowance.from_binary(self.scheduler.allowance_bytes)

    def timer(self, deadline: float) -> ReplayTimer:
        return self.scheduler.timer(deadline)

    def group(self) -> ReplayGroup:
        return ReplayGroup(self.scheduler)

    def channel(self) -> ReplayChannel:
        return ReplayChannel(self.scheduler)

    def latch(self) -> ReplayLatch:
        return ReplayLatch(self.scheduler)

    def first(self, *sources: ReplaySource) -> ReplaySource[tuple[int, object]]:
        if not sources:
            problem = "first requires at least one source"
            raise ReplayError(problem)
        return _First(sources)
