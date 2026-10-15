"""A close that ends only on proof of release.

A close-once latch (``if self._closed: return; self._closed = True; <effect>``)
reports a failed close as success on every retry: the latch is set before the
effect is known to have worked. :class:`CloseOnce` and :class:`AsyncCloseOnce`
replace it. They remember a *failed* close, so the next call runs the effect
again, and only the :class:`Released` value the effect returns ends them.

Nobody constructs :class:`Released` directly. A :class:`ReleaseStack` mints it
once every teardown step it holds has run, keeping the steps that failed for the
next ``close``, as ``contextlib.ExitStack`` does not; :meth:`Released.shown_by`
mints it from an owner's typed :class:`ReleaseEvidence` (``docker rm`` read as
gone, a registry left empty), and refuses evidence that shows nothing released.
So "released" is never set on the caller's word alone.

``started`` says a close was asked for, so the owner can refuse new work while
a failed close waits for its retry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection
    from contextlib import AbstractContextManager

_MINT = object()


class ReleaseEvidence(Protocol):
    """An owner's typed observation of whether its resource is gone."""

    @property
    def released(self) -> bool:
        """Whether this observation shows the resource gone or going."""
        ...

    def describe(self) -> str:
        """What was observed, for the proof's ``evidence``."""
        ...


class NotReleasedError(RuntimeError):
    """Evidence offered as proof of release shows the resource still held."""


@dataclass(frozen=True)
class Released:
    """Proof that a resource is gone or going; ``evidence`` says what showed it.

    Minted only by :meth:`shown_by` and :meth:`ReleaseStack.close`.
    """

    evidence: str
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Refuse a proof built from a bare string."""
        if self._mint is not _MINT:
            message = "mint Released with Released.shown_by(evidence) or ReleaseStack.close()"
            raise TypeError(message)

    @classmethod
    def shown_by(cls, evidence: ReleaseEvidence) -> Released:
        """The proof ``evidence`` gives; ``NotReleasedError`` when it shows the resource held."""
        if not evidence.released:
            message = f"not released: {evidence.describe()}"
            raise NotReleasedError(message)
        return cls(evidence.describe(), _MINT)


@dataclass(frozen=True)
class NothingLeft:
    """Evidence that every resource an owner tracked was closed and dropped from its registry."""

    remaining: Collection[object]
    what: str

    @property
    def released(self) -> bool:
        """Whether the registry is empty."""
        return not self.remaining

    def describe(self) -> str:
        """The registry, and how much is left in it."""
        return f"{self.what}: {len(self.remaining)} left"


class ReleaseStack:
    """Teardown steps run last-in first-out, where a step that failed stays for the next close.

    ``contextlib.ExitStack.close`` drops each step before running it, so a close retried
    after a failure runs nothing and looks like a success. Here :meth:`close` runs every
    remaining step, keeps the ones that raised, and returns :class:`Released` only when
    none is left. Not thread-safe: an owner whose close can race holds its own lock.
    """

    def __init__(self) -> None:
        """Start with no steps."""
        self._steps: list[Callable[[], object]] = []

    def callback[**P](
        self, step: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs
    ) -> None:
        """Run ``step(*args, **kwargs)`` at close."""
        self._steps.append(lambda: step(*args, **kwargs))

    def enter_context[T](self, manager: AbstractContextManager[T]) -> T:
        """Enter ``manager`` now and exit it at close.

        An exit cannot be run twice, so one that raised fails every later close too:
        the stack never reports released over it.
        """
        value = manager.__enter__()
        failure: list[BaseException] = []

        def leave() -> None:
            if failure:
                message = f"{manager!r} failed to exit and cannot be retried: {failure[0]!r}"
                raise NotReleasedError(message)
            try:
                manager.__exit__(None, None, None)
            except BaseException as error:
                failure.append(error)
                raise

        self._steps.append(leave)
        return value

    def pop_all(self) -> Self:
        """Move every step to a new stack, so this one closes nothing."""
        moved = type(self)()
        for step in self._steps:
            moved.callback(step)
        self._steps = []
        return moved

    def close(self) -> Released:
        """Run the remaining steps, newest first; raise the first failure after trying all.

        A step that raised stays on the stack, so the next call runs it again.
        """
        failed: list[Callable[[], object]] = []
        first: BaseException | None = None
        for step in reversed(self._steps):
            try:
                step()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-750114 [BLE001]; every teardown step runs even after one failed, and the first failure is raised after.
                # > A narrower type would let an unexpected failure skip the later steps, which is
                # > the leak this stack exists to prevent.
                failed.append(step)
                if first is None:
                    first = error
                else:
                    first.add_note(f"another teardown step also failed: {error!r}")
        self._steps = failed[::-1]
        if first is not None:
            raise first
        return Released("every teardown step ran", _MINT)


def _proof(value: object) -> Released:
    if not isinstance(value, Released):
        message = f"a close effect must return Released, not {type(value).__name__}"
        raise TypeError(message)
    return value


def _read_outcome(attempt: asyncio.Task[Released]) -> None:
    if not attempt.cancelled():
        attempt.exception()


class CloseOnce:
    """Run a synchronous release until it returns :class:`Released`.

    Not thread-safe: an owner whose close can race holds its own lock around
    :meth:`run`.
    """

    def __init__(self) -> None:
        """Start open: no close asked for, nothing released."""
        self._started = False
        self._released: Released | None = None

    @property
    def started(self) -> bool:
        """Whether a close was asked for, whatever its outcome."""
        return self._started

    @property
    def released(self) -> Released | None:
        """The proof of release, or ``None`` while the resource may still exist."""
        return self._released

    def run(self, effect: Callable[[], Released]) -> Released:
        """Return the earlier proof, or run *effect* and keep its proof.

        An exception from *effect* propagates and leaves the latch unreleased,
        so the next call runs *effect* again.
        """
        self._started = True
        if self._released is None:
            self._released = _proof(effect())
        return self._released


class AsyncCloseOnce:
    """Run an asynchronous release until it returns :class:`Released`.

    Concurrent callers share one attempt; the attempt is shielded, so a
    cancelled caller does not cancel the release. A failed attempt is
    forgotten, so the next call starts a new one.
    """

    def __init__(self) -> None:
        """Start open: no close asked for, nothing released."""
        self._started = False
        self._released: Released | None = None
        self._attempt: asyncio.Task[Released] | None = None

    @property
    def started(self) -> bool:
        """Whether a close was asked for, whatever its outcome."""
        return self._started

    @property
    def released(self) -> Released | None:
        """The proof of release, or ``None`` while the resource may still exist."""
        return self._released

    async def run(self, effect: Callable[[], Awaitable[Released]]) -> Released:
        """Return the earlier proof, or join or start an attempt of *effect*."""
        self._started = True
        if self._released is not None:
            return self._released
        if self._attempt is None:
            self._attempt = asyncio.create_task(self._settle(effect))
            # A failure every caller stopped waiting for is still read, so asyncio does not log it.
            self._attempt.add_done_callback(_read_outcome)
        return await asyncio.shield(self._attempt)

    async def _settle(self, effect: Callable[[], Awaitable[Released]]) -> Released:
        try:
            self._released = _proof(await effect())
        finally:
            self._attempt = None
        return self._released
