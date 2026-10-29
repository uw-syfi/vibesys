"""A read-only, backend-neutral view of the queue a run's jobs wait in.

Agents decide by queue contention: a long expected wait for an evaluation slot is a
signal to keep implementing rather than to wait. ``QueueStatusReader`` is the one
interface every run environment implements; it returns a ``QueueStatus`` or a typed
``QueueStatusUnavailable``, never raises. It has no write method, so nothing reached
through it can cancel, modify or name another user's job; other users' work appears only
as anonymous counts in ``QueueContention``.

A field the backend cannot tell is ``None``, never a guess: ``items`` is ``None`` when
the backend cannot list the run's own jobs, and an empty tuple only when it listed them
and found none.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter, model_validator

if TYPE_CHECKING:
    from vs_sim.api import Clock, Threads

_NonNegativeSeconds = Annotated[FiniteFloat, Field(ge=0)]
_Count = Annotated[int, Field(ge=0)]
FRESH_FOR_S = 30.0
"""How long a host reuses one queue reading for every agent that asks (``CachedQueueStatusReader``)."""
MAX_REASON_CHARS = 500
"""The longest unavailability reason a reading carries; longer text is cut, not refused."""


class QueueKind(StrEnum):
    """What a run's jobs wait in, which says how to read the rest of the status."""

    SHARED = "shared"
    """A scheduler shared with other users: jobs wait for a slot behind others' jobs."""
    LOCAL = "local"
    """This run's own machine: work starts when the run's own capacity frees, behind no one."""
    OPAQUE = "opaque"
    """A service that does not report its queue: every field it cannot tell is ``None``."""


class QueueItemKind(StrEnum):
    """Which of the run's own jobs an item is."""

    EVALUATION = "evaluation"
    """A trusted evaluation the run submitted."""
    AGENT_GPU = "agent_gpu"
    """A GPU command an agent of this run started (profiling, tests)."""


class QueueItemState(StrEnum):
    """Whether an item still waits for resources or holds them."""

    WAITING = "waiting"
    RUNNING = "running"


class QueueItem(BaseModel):
    """One of the run's own jobs, as the backend reports it.

    ``waited_seconds`` is the time spent waiting so far (waiting) or before it started
    (running). ``jobs_ahead`` counts waiting jobs, anyone's, that the scheduler would
    start first by priority; with ``estimated_start_in_seconds`` it is an estimate the
    scheduler may revise. Both are ``None`` for a running item or when unknown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(min_length=1, max_length=64)]
    kind: QueueItemKind
    state: QueueItemState
    waited_seconds: _NonNegativeSeconds | None = None
    running_seconds: _NonNegativeSeconds | None = None
    jobs_ahead: _Count | None = None
    estimated_start_in_seconds: _NonNegativeSeconds | None = None
    wait_reason: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _fields_match_state(self) -> QueueItem:
        if self.state is QueueItemState.WAITING and self.running_seconds is not None:
            message = "a waiting item has no running time"
            raise ValueError(message)
        if self.state is QueueItemState.RUNNING and (
            self.jobs_ahead is not None
            or self.estimated_start_in_seconds is not None
            or self.wait_reason is not None
        ):
            message = "a running item has no position, start estimate or wait reason"
            raise ValueError(message)
        return self


class QueueContention(BaseModel):
    """Anonymous counts over the queue the run's jobs wait in; ``None`` where unknown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    jobs_waiting: _Count | None = None
    jobs_running: _Count | None = None
    gpus_total: _Count | None = None
    gpus_busy: _Count | None = None
    gpus_free: _Count | None = None

    @model_validator(mode="after")
    def _gpus_add_up(self) -> QueueContention:
        total, busy, free = self.gpus_total, self.gpus_busy, self.gpus_free
        if total is not None and busy is not None and free is not None and busy + free != total:
            message = "busy and free GPUs must add up to the total"
            raise ValueError(message)
        return self


class QueueStatus(BaseModel):
    """The run's own queued and running jobs and the contention they wait in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["read"] = "read"
    queue: QueueKind
    items: tuple[QueueItem, ...] | None
    contention: QueueContention = QueueContention()


class QueueStatusUnavailable(BaseModel):
    """The backend could not be read just now; ``reason`` says why, for the agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["unavailable"] = "unavailable"
    reason: Annotated[str, Field(min_length=1, max_length=MAX_REASON_CHARS)]

    @classmethod
    def because(cls, reason: str) -> QueueStatusUnavailable:
        """The reading for ``reason``, cut to the length a reading carries."""
        text = reason.strip() or "unknown failure"
        return cls(reason=text[:MAX_REASON_CHARS])


type QueueReading = Annotated[QueueStatus | QueueStatusUnavailable, Field(discriminator="status")]

QUEUE_READING: TypeAdapter[QueueStatus | QueueStatusUnavailable] = TypeAdapter(QueueReading)
"""Decodes one reading; its JSON form is what the agent tool returns."""


class QueueStatusReader(Protocol):
    """Reads the run's queue status. Read-only by construction: it has no other method."""

    def read(self) -> QueueStatus | QueueStatusUnavailable:
        """One reading; a backend failure is ``QueueStatusUnavailable``, never an exception."""
        ...


class LocalQueueStatusReader:
    """The run's own machine: no one else's work is ahead, and no queue lists the run's jobs."""

    def read(self) -> QueueStatus:
        """A local run waits behind nothing but its own capacity."""
        return QueueStatus(
            queue=QueueKind.LOCAL, items=None, contention=QueueContention(jobs_waiting=0)
        )


class OpaqueQueueStatusReader:
    """A service that reports no queue (Modal): every field is unknown."""

    def read(self) -> QueueStatus:
        """Nothing the service does not report."""
        return QueueStatus(queue=QueueKind.OPAQUE, items=None)


class CachedQueueStatusReader:
    """Answers from the last reading while it is fresh, so many agents cost one backend read.

    Concurrent callers share one read: the lock is held across it. An unavailable reading
    is cached too, so a backend that is down is not asked again by every caller.
    """

    def __init__(
        self, reader: QueueStatusReader, *, clock: Clock, threads: Threads, fresh_for_s: float
    ) -> None:
        """Wrap ``reader``; a reading is reused for ``fresh_for_s`` seconds on ``clock``."""
        if fresh_for_s <= 0:
            message = "fresh_for_s must be positive"
            raise ValueError(message)
        self._reader = reader
        self._clock = clock
        self._lock = threads.lock()
        self._fresh_for_s = fresh_for_s
        self._last: tuple[float, QueueStatus | QueueStatusUnavailable] | None = None

    def read(self) -> QueueStatus | QueueStatusUnavailable:
        """The cached reading while fresh, else a new one."""
        with self._lock:
            now = self._clock.now()
            if self._last is not None and now - self._last[0] < self._fresh_for_s:
                return self._last[1]
            reading = self._reader.read()
            self._last = (self._clock.now(), reading)
            return reading


__all__ = [
    "FRESH_FOR_S",
    "MAX_REASON_CHARS",
    "QUEUE_READING",
    "CachedQueueStatusReader",
    "LocalQueueStatusReader",
    "OpaqueQueueStatusReader",
    "QueueContention",
    "QueueItem",
    "QueueItemKind",
    "QueueItemState",
    "QueueKind",
    "QueueReading",
    "QueueStatus",
    "QueueStatusReader",
    "QueueStatusUnavailable",
]
