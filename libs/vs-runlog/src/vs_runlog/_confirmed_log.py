"""Append to a run.log one record at a time, extending the prefix it confirmed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from protobuf import Oneof

from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    EpochStarted,
    Record,
    RunEnded,
    RunStarted,
    StepFinished,
    StepStarted,
    StrategyAdvanced,
)

if TYPE_CHECKING:
    from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

type CoreMessage = (
    EpochStarted
    | RunStarted
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

    def next_record(
        self, message: CoreMessage, *, epoch: int, at: Timestamp, clock: float
    ) -> Record:
        """``message`` stamped as the record that follows the confirmed prefix, not yet appended.

        A caller that must accept a record before it is durable (a fold that refuses it) builds
        it here and passes it to ``extend``.
        """
        return Record(
            seq=len(self._prefix()) + 1, epoch=epoch, at=at, clock=clock, body=_body(message)
        )

    def append(
        self, message: CoreMessage, *, epoch: int, at: Timestamp, clock: float
    ) -> Appended | Uncertain:
        """Stamp ``message`` with the next sequence and append it as a record."""
        return self.extend(self.next_record(message, epoch=epoch, at=at, clock=clock))


def _body(message: CoreMessage) -> _CoreBody:
    match message:
        case EpochStarted():
            return Oneof("epoch_started", message)
        case RunStarted():
            return Oneof("run_started", message)
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
