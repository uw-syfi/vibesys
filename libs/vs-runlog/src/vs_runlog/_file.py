"""Single-writer file storage with explicit tail recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, BinaryIO, Self

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType

    from vs_proto.api import Record
from vs_proto.api import RUN_LOG_MAGIC
from vs_runlog._framing import ReadStop, StopReason, encode_frame, read_frame


class RunLogReader:
    """Iterate complete records; inspect ``stop`` after iteration and resume at its offset."""

    def __init__(self, stream: BinaryIO, offset: int = 0) -> None:
        """Read from a frame boundary; validate magic even when resuming."""
        if offset and offset < len(RUN_LOG_MAGIC):
            message = "run.log offset must be zero or a record boundary after magic"
            raise ValueError(message)
        self._stream = stream
        self._offset = offset
        self.stop = ReadStop(StopReason.END, offset)

    def __iter__(self) -> Iterator[Record]:
        """Yield complete records, leaving torn and corrupt frames unconsumed."""
        self._stream.seek(0)
        magic = self._stream.read(len(RUN_LOG_MAGIC))
        if magic != RUN_LOG_MAGIC:
            reason = (
                StopReason.TORN_TAIL if RUN_LOG_MAGIC.startswith(magic) else StopReason.CORRUPTION
            )
            self.stop = ReadStop(
                reason, 0, "incomplete magic" if reason is StopReason.TORN_TAIL else "bad magic"
            )
            return
        self._stream.seek(self._offset or len(RUN_LOG_MAGIC))
        while True:
            record, self.stop = read_frame(self._stream, self._stream.tell())
            if record is None:
                return
            yield record


class FileRunLogWriter:
    """Append dense sequences, recovering only an incomplete final frame on open.

    One process owns a file. Complete corruption is never repaired implicitly.
    Each successful append is committed without a per-record fsync in phase 1.
    """

    def __init__(self, path: Path) -> None:
        """Create a log or scan and recover the existing log before appending."""
        self.path = path
        self.sequence = 0
        self.epoch = 0
        if not path.exists():
            with path.open("xb") as stream:
                stream.write(RUN_LOG_MAGIC)
        else:
            self._recover(path)
        self._stream = path.open("ab", buffering=0)

    def _recover(self, path: Path) -> None:
        with path.open("r+b") as stream:
            reader = RunLogReader(stream)
            for record in reader:
                if record.seq != self.sequence + 1:
                    message = f"non-dense run.log sequence: {record.seq} after {self.sequence}"
                    raise ValueError(message)
                self.sequence = record.seq
                self.epoch = max(self.epoch, record.epoch)
            if reader.stop.reason is StopReason.CORRUPTION:
                message = f"corrupt run.log at byte {reader.stop.offset}: {reader.stop.detail}"
                raise ValueError(message)
            if reader.stop.reason is StopReason.TORN_TAIL:
                stream.truncate(reader.stop.offset)
                if reader.stop.offset == 0:
                    stream.seek(0)
                    stream.write(RUN_LOG_MAGIC)

    def append(self, record: Record) -> None:
        """Commit one record whose sequence is exactly the next dense sequence."""
        if record.seq != self.sequence + 1:
            message = f"expected run.log seq {self.sequence + 1}, got {record.seq}"
            raise ValueError(message)
        frame = encode_frame(record)
        try:
            written = self._stream.write(frame)
        except BaseException:
            self.close()
            raise
        if written != len(frame):
            self.close()
            message = "incomplete run.log append"
            raise OSError(message)
        self.sequence = record.seq
        self.epoch = max(self.epoch, record.epoch)

    def close(self) -> None:
        """Release the unbuffered append handle; safe to call more than once."""
        self._stream.close()

    def __enter__(self) -> Self:
        """Own the append handle for a lexical scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the append handle on every exit."""
        self.close()
