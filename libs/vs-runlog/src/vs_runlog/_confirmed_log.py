"""Append to a run.log one record at a time, extending the prefix it confirmed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from protobuf import Oneof

from vs_proto.api import AttemptInterrupted, CoreInput, Record, StepFinished, StepStarted

if TYPE_CHECKING:
    from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

type CoreMessage = CoreInput | StepStarted | StepFinished | AttemptInterrupted
"""The messages core folds into its state, each the body of one record."""

type _CoreBody = (
    Oneof[Literal["core_input"], CoreInput]
    | Oneof[Literal["step_started"], StepStarted]
    | Oneof[Literal["step_finished"], StepFinished]
    | Oneof[Literal["attempt_interrupted"], AttemptInterrupted]
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

    def __init__(self, writer: RunLogRecords) -> None:
        """Append through ``writer``; the prefix is read on first use."""
        self._writer = writer
        self._confirmed: list[Record] | None = None

    def records(self) -> tuple[Record, ...]:
        """The confirmed prefix: every durable record, in order."""
        return tuple(self._prefix())

    def _prefix(self) -> list[Record]:
        if self._confirmed is None:
            self._confirmed = list(self._writer.read())
        return self._confirmed

    def forget(self) -> None:
        """Read the log again on next use: another appender may have extended it."""
        self._confirmed = None

    def extend(self, record: Record) -> Appended | Uncertain:
        """Append ``record``, which must be the one record that follows the confirmed prefix."""
        prefix = self._prefix()
        if record.seq != len(prefix) + 1:
            message = f"run.log record {record.seq} does not follow {len(prefix)} confirmed"
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

    def append(
        self, message: CoreMessage, *, epoch: int, at: Timestamp, clock: float
    ) -> Appended | Uncertain:
        """Stamp ``message`` with the next sequence and append it as a record."""
        record = Record(
            seq=len(self._prefix()) + 1, epoch=epoch, at=at, clock=clock, body=_body(message)
        )
        return self.extend(record)


def _body(message: CoreMessage) -> _CoreBody:
    match message:
        case CoreInput():
            return Oneof("core_input", message)
        case StepStarted():
            return Oneof("step_started", message)
        case StepFinished():
            return Oneof("step_finished", message)
        case AttemptInterrupted():
            return Oneof("attempt_interrupted", message)
