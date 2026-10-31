"""Append to a run.log one record at a time, extending the prefix it confirmed.

``ConfirmedLog`` is the one appender of a run: every record of the run, core's and the display
projection's alike, goes through it, one dense sequence under one lock. A caller that builds a
record from the prefix (a fold that must accept it first) holds the lock across the whole
decision with ``transaction``.

It is also the one place a record is dated. Whoever asks for a clock, the record gets that clock
no later than the run's present instant and no earlier than the log's last record: the clocks
of a log are non-decreasing in log order, and no record is dated after an instant a caller of the
run has already read. A fact the loop dated at the present instant is then never older than a
record of the log, whoever appended it, so a fold of the whole log accepts it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from protobuf import Oneof

from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    CoreSnapshot,
    EpochStarted,
    Record,
    RunEnded,
    RunStarted,
    StepFinished,
    StepStarted,
    StrategyAdvanced,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

    from vs_runlog._epoch import CurrentEpoch
    from vs_runlog._file import FileRunLogWriter
    from vs_sim.api import Lock, Threads

type CoreMessage = (
    EpochStarted
    | RunStarted
    | CoreSnapshot
    | CoreInput
    | StrategyAdvanced
    | StepStarted
    | StepFinished
    | AttemptInterrupted
    | RunEnded
)
"""The messages core folds into its state, each the body of one record.

The epoch header opens an epoch: its first record proves the earlier epoch's process dead."""

type _CoreBody = (
    Oneof[Literal["epoch_started"], EpochStarted]
    | Oneof[Literal["run_started"], RunStarted]
    | Oneof[Literal["core_snapshot"], CoreSnapshot]
    | Oneof[Literal["core_input"], CoreInput]
    | Oneof[Literal["strategy_advanced"], StrategyAdvanced]
    | Oneof[Literal["step_started"], StepStarted]
    | Oneof[Literal["step_finished"], StepFinished]
    | Oneof[Literal["attempt_interrupted"], AttemptInterrupted]
    | Oneof[Literal["run_ended"], RunEnded]
)


class RunLogRecords(Protocol):
    """The writer surface the confirmed log needs; ``FileRunLogWriter`` provides it."""

    def append(self, record: Record) -> None: ...

    def commit(self) -> None: ...

    def read(self) -> tuple[Record, ...]: ...


@dataclass(frozen=True)
class Appended:
    """The record is in the log and durable."""

    record: Record


@dataclass(frozen=True)
class Uncertain:
    """The write or its sync failed: the record may or may not be in the log.

    An uncertain append authorizes nothing that depends on the record. The confirmed prefix is
    forgotten, so the next read of the log says what happened.
    """


class ConfirmedLog:
    """The one appender of a run.log under its writer lock, and the prefix it confirmed.

    Under one writer only this object appends, so the prefix it confirmed stays current and an
    append costs its own record. A write or sync error is ``Uncertain``. The writer drops only a
    torn final frame on open; complete corruption fails there, never here.
    """

    def __init__(
        self,
        writer: RunLogRecords,
        threads: Threads,
        *,
        present: Callable[[], float | None] = lambda: None,
    ) -> None:
        """Append through ``writer``; the prefix is read on first use.

        ``threads`` makes the lock the appends of every thread (the loop's, the projection's)
        go through. ``present`` is the run's present instant on its clock, None while the run
        has none (its clock starts when the loop takes the log over): no record is dated after it.
        """
        self._writer = writer
        self._confirmed: list[Record] | None = None
        self._lock: Lock = threads.rlock()
        self._present = present

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the log: no other thread appends until the block ends.

        For a caller that reads the prefix, decides on it and appends, so that nothing lands
        between the read and the append.
        """
        with self._lock:
            yield

    def records(self) -> tuple[Record, ...]:
        """The confirmed prefix: every durable record, in order."""
        with self._lock:
            return tuple(self._prefix())

    def _prefix(self) -> list[Record]:
        if self._confirmed is None:
            self._confirmed = list(self._writer.read())
        return self._confirmed

    def forget(self) -> None:
        """Read the log again on next use: another appender may have extended it."""
        with self._lock:
            self._confirmed = None

    def extend(self, record: Record) -> Appended | Uncertain:
        """Append ``record``, which must be the one record that follows the confirmed prefix.

        The record is durable when this returns, and so is every record staged before it.
        """
        with self._lock:
            prefix = self._prefix()
            if record.seq != len(prefix) + 1:
                message = f"run.log record {record.seq} does not follow {len(prefix)} confirmed"
                raise ValueError(message)
            if record.clock < _latest(prefix):
                message = f"run.log record {record.seq} is dated before the record that precedes it"
                raise ValueError(message)
            self._confirmed = None
            try:
                self._writer.append(record)
                self._writer.commit()
            except OSError:
                return Uncertain()
            prefix.append(record)
            self._confirmed = prefix
            return Appended(record)

    def stage(self, record: Record) -> None:
        """Append ``record`` as the next record of the run, numbered here, durable at ``commit``.

        For the display projection, whose records are many and small and which commits at the
        boundaries of a run. The record's own ``seq`` is replaced: the projection cannot know how
        many records core appended since it last looked, and its ``clock`` is dated as every
        record is. ``record`` is numbered and dated in place.
        """
        with self._lock:
            prefix = self._prefix()
            record.seq = len(prefix) + 1
            record.clock = self._dated(record.clock, prefix)
            self._confirmed = None
            self._writer.append(record)
            prefix.append(record)
            self._confirmed = prefix

    def commit(self) -> None:
        """Make every staged record durable."""
        with self._lock:
            self._writer.commit()

    def next_record(
        self, message: CoreMessage, *, epoch: int, at: Timestamp, clock: float
    ) -> Record:
        """``message`` stamped as the record that follows the confirmed prefix, not yet appended.

        A caller that must accept a record before it is durable (a fold that refuses it) builds
        it here and passes it to ``extend``. The record carries ``clock`` as the log dates it.
        """
        with self._lock:
            prefix = self._prefix()
            return Record(
                seq=len(prefix) + 1,
                epoch=epoch,
                at=at,
                clock=self._dated(clock, prefix),
                body=_body(message),
            )

    def _dated(self, clock: float, prefix: list[Record]) -> float:
        """``clock`` no later than the present instant, and no earlier than the last record's."""
        present = self._present()
        return max(clock if present is None else min(clock, present), _latest(prefix))

    def append(
        self, message: CoreMessage, *, epoch: int, at: Timestamp, clock: float
    ) -> Appended | Uncertain:
        """Stamp ``message`` with the next sequence and append it as a record."""
        with self._lock:
            return self.extend(self.next_record(message, epoch=epoch, at=at, clock=clock))


def _latest(prefix: list[Record]) -> float:
    """The clock of the last record, 0 for a log with none."""
    return prefix[-1].clock if prefix else 0.0


def _body(message: CoreMessage) -> _CoreBody:
    match message:
        case EpochStarted():
            return Oneof("epoch_started", message)
        case RunStarted():
            return Oneof("run_started", message)
        case CoreSnapshot():
            return Oneof("core_snapshot", message)
        case CoreInput():
            return Oneof("core_input", message)
        case StrategyAdvanced():
            return Oneof("strategy_advanced", message)
        case _:
            return _step_body(message)


def _step_body(
    message: StepStarted | StepFinished | AttemptInterrupted | RunEnded,
) -> _CoreBody:
    match message:
        case StepStarted():
            return Oneof("step_started", message)
        case StepFinished():
            return Oneof("step_finished", message)
        case AttemptInterrupted():
            return Oneof("attempt_interrupted", message)
        case RunEnded():
            return Oneof("run_ended", message)


class ProjectionWriter(RunLogRecords, Protocol):
    """What the display projection needs of the writer it appends to.

    ``FileRunLogWriter`` and ``SharedWriter`` both provide it.
    """

    @property
    def sequence(self) -> int:
        """The number of the last record."""
        ...

    @property
    def epoch(self) -> int:
        """The current epoch."""
        ...

    @property
    def current_epoch(self) -> CurrentEpoch:
        """The current epoch's facts."""
        ...

    def close(self) -> None: ...


class SharedWriter:
    """The writer surface of the display projection, over the run's one appender.

    The projection numbers nothing itself: each record it appends takes the next number of the
    run, after whatever core appended in between, and is durable at ``commit``.
    """

    def __init__(self, log: ConfirmedLog, writer: FileRunLogWriter) -> None:
        """Append through ``log``; epoch facts and ``close`` are the file writer's."""
        self._log = log
        self._writer = writer

    @property
    def sequence(self) -> int:
        """The number of the last record of the run."""
        return len(self._log.records())

    @property
    def epoch(self) -> int:
        """The writer's current epoch."""
        return self._writer.epoch

    @property
    def current_epoch(self) -> CurrentEpoch:
        """The writer's epoch facts."""
        return self._writer.current_epoch

    def append(self, record: Record) -> None:
        """Append ``record`` as the run's next record."""
        self._log.stage(record)

    def commit(self) -> None:
        """Make every appended record durable."""
        self._log.commit()

    def read(self) -> tuple[Record, ...]:
        """Every record of the run, in order."""
        return self._log.records()

    def close(self) -> None:
        """Close the file writer."""
        self._writer.close()
