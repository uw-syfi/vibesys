"""What one running flow lends the aspects of ``ctx``: issuing, waiting, reading, shielding.

``Flow`` is what authors hold. The aspects (``ctx.agents``, ``ctx.workspaces``,
``ctx.evaluation``) and the framework's ending are core's own code, and they reach the
runtime through a ``Kit``: requests are stamped and issued here, reads are journaled here, and
a stop is delivered here. Authors never see it, so ``wait`` and ``start`` are not part of the
vocabulary of a flow.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from vs_core._registry import ContractError
from vs_core.types.strategy import Finish, Wait, Work
from vs_core.types.until import Failed, Within

from ._errors import Stopped
from ._sources import Pending

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pydantic import TypeAdapter

    from vs_core.types.common import Seconds
    from vs_core.types.search import Event, RunView

    from ._defs import _Task
    from ._labels import Label
    from ._run import _Run
    from ._sources import Signal, Timer


class Kit:
    """The runtime's handle on one task: what core's own code may do for a flow."""

    def __init__(self, run: _Run, task: _Task) -> None:
        """Bind the handle to its run and its own task."""
        self.run = run
        self.task = task

    @property
    def path(self) -> str:
        """Where the flow sits: empty for the root, ``"a/b/"`` for task ``b`` of task ``a``."""
        return self.task.path

    def stop_point(self) -> None:
        """Raise a pending stop before anything is issued, awaited or acquired (unless shielded)."""
        task = self.task
        if task.stop_pending and task.shield == 0:
            self.run.land_stop(task)
            raise Stopped

    @contextmanager
    def shielded(self) -> Iterator[None]:
        """A region a stop cannot interrupt: it is delivered at the first await after it."""
        self.task.shield += 1
        try:
            yield
        finally:
            self.task.shield -= 1

    # requests and waiting

    def start(self, request: Work, until: Within, label: Label | None = None) -> Pending:
        """Start ``request`` now, bounded by ``until``; its outcome is the ``Pending``'s value.

        ``label`` labels the request's span.
        """
        self.stop_point()
        stamped = self.run.stamp(self.task, request)
        wait = Wait(request=stamped, until=until)
        return self.run.issue(self.task, Pending(stamped), wait, label)

    async def wait(self, request: Work, until: Within, label: Label | None = None) -> Event:
        """Start ``request`` and return its outcome: the result event, or ``Failed``."""
        return await self.settle(self.start(request, until, label))

    async def settle(self, pending: Pending) -> Event:
        """The outcome of a started request; a stop abandons it (a late outcome is dropped)."""
        try:
            return await pending
        except Stopped:
            self.run.forget(pending)
            raise

    async def finish(self, finish: Finish) -> Failed:
        """End the run. Returns only if core refuses the ``Finish``; otherwise never resumes."""
        stamped = self.run.stamp(self.task, finish)
        self.run.concluded = True
        refused = await self.run.issue(self.task, Pending(stamped), stamped)
        if not isinstance(refused, Failed):
            raise ContractError(("finish",), "a finish is answered only by a failure")
        return refused

    def timer(self, at: Seconds) -> Timer:
        """A source that fires once, when the run clock reaches ``at``."""
        return self.run.timer(at)

    def signal(self, label: str, wanted: Callable[[Event], bool]) -> Signal:
        """A source of the turn-plumbing events ``wanted`` accepts; it closes with the task."""
        return self.run.signal(self.task, label, wanted)

    # reading and counting

    def read[T](self, fn: Callable[[RunView], T], as_: type[T] | TypeAdapter[T]) -> T:
        """Read the view. The value is journaled, so live and replay see the same value."""
        return self.run.read(fn, as_)

    def now(self) -> Seconds:
        """The run clock (a journaled read)."""
        return self.read(lambda view: view.now_at, float)

    def count(self, label: str) -> int:
        """The next number under ``label`` in this flow (1, 2, ...); numbers never repeat."""
        return self.run.count(self.task, label)

    def cleanup_within(self) -> Within:
        """The bound for work that must still run once the run's time is spent.

        It lasts until the run deadline, but never less than the run's cleanup budget, so a
        close or an adoption after the deadline has a limit of its own.
        """
        return self.read(_cleanup_within, Within)

    def until_deadline(self) -> Within:
        """The bound for work whose own duration nothing else limits: the run's remaining time."""
        return self.read(_until_deadline, Within)


def _until_deadline(view: RunView) -> Within:
    return Within(limit=max(view.deadline_at - view.now_at, 0.0))


def _cleanup_within(view: RunView) -> Within:
    return Within(limit=max(view.deadline_at - view.now_at, view.limits.cleanup_bound))
