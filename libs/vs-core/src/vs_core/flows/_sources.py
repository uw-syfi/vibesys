"""Sources: what a flow can wait on. Each yields every value once.

A flow suspends by awaiting ``First(sources)`` (``await source`` is the one-source case). The
runtime resumes it with the next value of the first source, in argument order, that has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_core._registry import ContractError
from vs_core.types.search import Event, StopRequested

from ._errors import TaskStopped, TimeUp

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from vs_core.types.common import DecisionId, Seconds
    from vs_core.types.strategy import Finish, Work

    from ._spans import SpanId


class Source[T]:
    """Something a flow can wait for. ``ready``/``take`` are the runtime's, not the author's."""

    def ready(self) -> bool:
        """Whether a value is waiting."""
        raise NotImplementedError

    def take(self) -> T:
        """Consume the waiting value."""
        raise NotImplementedError

    def alive(self) -> bool:
        """Whether a value is waiting or may still come (else waiting on it is a deadlock)."""
        raise NotImplementedError

    def describe(self) -> str:
        """What this source is, for logs and ``Deadlock``."""
        raise NotImplementedError

    def __await__(self) -> Generator[First, Any, T]:
        """Wait for this source's next value."""
        return First((self,)).__await__()


@dataclass(frozen=True)
class First:
    """A flow's suspension: it waits for the next value of any of ``sources``."""

    sources: tuple[Source[Any], ...]

    def __await__(self) -> Generator[First, Any, Any]:
        """Hand the wait to the runtime; its answer is the value."""
        return (yield self)


class _Once[T](Source[T]):
    """A source with at most one value, set from outside."""

    def __init__(self) -> None:
        self._values: list[T] = []
        self._taken = False

    def set(self, value: T) -> None:
        """Make the value available (only the first call counts)."""
        if not self._values and not self._taken:
            self._values.append(value)

    def ready(self) -> bool:
        return bool(self._values)

    def take(self) -> T:
        self._taken = True
        return self._values.pop()

    def alive(self) -> bool:
        return not self._taken


class Pending(_Once[Event]):
    """The outcome of a request started with ``ctx.start``."""

    def __init__(self, work: Work | Finish) -> None:
        super().__init__()
        self.work = work
        self.abandoned = False

    @property
    def decision(self) -> DecisionId:
        """The runtime-assigned id of the request."""
        return self.work.decision_id

    def alive(self) -> bool:
        return not self._taken and not self.abandoned

    def describe(self) -> str:
        return self.decision.root


class Timer(_Once[TimeUp]):
    """Fires once, when the run clock reaches ``at``."""

    def __init__(self, at: Seconds) -> None:
        super().__init__()
        self.at = at

    def describe(self) -> str:
        return f"timer@{self.at:g}"


class StopSource(_Once[StopRequested]):
    """The operator's stop, as a value (``ctx.stopped``)."""

    def describe(self) -> str:
        return "stop"


class Task[T](_Once[T | TaskStopped]):
    """A task started in a group: its result once, or ``TaskStopped`` if a stop ended it."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.span: SpanId | None = None
        """The task's span in the run trace, once it started."""

    def describe(self) -> str:
        return f"task {self.key}"


class Signal(Source[Event]):
    """Events of one kind that core reports about a turn, matched by a key (core's own plumbing).

    Unlike a Pending it may yield several values, and it never expires on its own: the
    code that opened it closes it.
    """

    def __init__(self, label: str, wanted: Callable[[Event], bool]) -> None:
        self.label = label
        self.wanted = wanted
        self._values: list[Event] = []
        self.closed = False

    def offer(self, event: Event) -> bool:
        """Take event if it is the one this signal waits for."""
        if self.closed or not self.wanted(event):
            return False
        self._values.append(event)
        return True

    def ready(self) -> bool:
        return bool(self._values)

    def take(self) -> Event:
        return self._values.pop(0)

    def alive(self) -> bool:
        return not self.closed

    def describe(self) -> str:
        return self.label


@dataclass(frozen=True, eq=False)
class LatchKey[T]:
    """The name of a run-wide latch, typed by the value it holds. Declare one per fact.

    A key is its declaration: two keys are the same latch only if they are the same object. A
    run refuses a second key with a name already in use, since the two could hold different
    types under one name (the type parameter is not kept at run time, so it cannot be compared).
    """

    name: str


class LatchCell:
    """The run's one copy of a latch's value; every handle on the key reads it."""

    def __init__(self, key: LatchKey[Any]) -> None:
        self.key = key
        self.held = False
        self.value: object = None


class Latch[T](Source[T]):
    """A run-wide value that one flow sets once and any flow may wait for (``ctx.latch``).

    Each handle yields the value once, so every waiter takes its own handle. The value is not
    journaled: flow code sets it, so a replay sets it again at the same point, and a restart
    after a code change starts with it unset until the restarted flows set it again.
    """

    def __init__(self, key: LatchKey[T], cell: LatchCell) -> None:
        self.key = key
        self._cell = cell
        self._taken = False

    def set(self, value: T) -> None:
        """Set the value, once for the run; a second set is a bug."""
        if self._cell.held:
            raise ContractError(("latch", self.key.name), "a latch is set once")
        self._cell.held, self._cell.value = True, value

    @property
    def value(self) -> T | None:
        """The value if it is set, without waiting; None while it is not."""
        return self._cell.value if self._cell.held else None  # ty: ignore[invalid-return-type]

    def ready(self) -> bool:
        return self._cell.held and not self._taken

    def take(self) -> T:
        self._taken = True
        return self._cell.value  # ty: ignore[invalid-return-type]

    def alive(self) -> bool:
        return not self._taken

    def describe(self) -> str:
        return f"latch {self.key.name}"
