"""Single-writer file storage with explicit tail recovery."""

from __future__ import annotations

import os
from contextlib import ExitStack
from typing import TYPE_CHECKING, BinaryIO, Self

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType

    from vs_proto.api import Record
from vs_proto.api import RUN_LOG_MAGIC, Keyframe, RunProcess, validate
from vs_runlog._atomic import publish, sync_directory, write_keyframe
from vs_runlog._fold import append_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame, read_frame
from vs_runlog._head import project_head
from vs_runlog._lock import RunLock

CHECKPOINT_RECORDS = 256


class RunLogReader:
    """Iterate complete records; inspect ``stop`` after iteration and resume at its offset."""

    def __init__(self, stream: BinaryIO, offset: int = 0) -> None:
        """Read from a frame boundary; validate magic even when resuming."""
        if offset and offset < len(RUN_LOG_MAGIC):
            message = "run.log offset must be zero or a record boundary after magic"
            raise ValueError(message)
        self._stream = stream
        self._offset = offset
        self.stop = ReadStop(RunLogStopReason.END, offset)

    def __iter__(self) -> Iterator[Record]:
        """Yield complete records, leaving torn and corrupt frames unconsumed."""
        self._stream.seek(0)
        magic = self._stream.read(len(RUN_LOG_MAGIC))
        if magic != RUN_LOG_MAGIC:
            reason = (
                RunLogStopReason.TORN_TAIL
                if RUN_LOG_MAGIC.startswith(magic)
                else RunLogStopReason.CORRUPTION
            )
            self.stop = ReadStop(
                reason,
                0,
                "incomplete magic" if reason is RunLogStopReason.TORN_TAIL else "bad magic",
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
    Each successful append is durable, then publishes its commit boundary.
    Phase 3 state.pb transactions will require a separate commit API.
    """

    def __init__(self, path: Path) -> None:
        """Create a log or scan and recover the existing log before appending."""
        self.sequence = 0
        self.epoch = 0
        self._path = path
        self._skeleton = Keyframe()
        self._checkpoint_seq = 0
        self._process: RunProcess | None = None
        self._resources = ExitStack()
        try:
            lock = RunLock(path.parent / "run.lock")
            self._resources.callback(lock.close)
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(RUN_LOG_MAGIC)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(path.parent)
            else:
                self._recover(path)
            self._stream = self._resources.enter_context(path.open("ab", buffering=0))
            if self.sequence:
                self._publish_head()
                self._publish_keyframe()
        except BaseException:
            self.close()
            raise

    def _recover(self, path: Path) -> None:
        with path.open("r+b") as stream:
            reader = RunLogReader(stream)
            for record in reader:
                if record.seq != self.sequence + 1:
                    message = f"non-dense run.log sequence: {record.seq} after {self.sequence}"
                    raise ValueError(message)
                append_skeleton(self._skeleton, record)
                self.sequence = record.seq
                self.epoch = max(self.epoch, record.epoch)
            if reader.stop.reason is RunLogStopReason.CORRUPTION:
                message = f"corrupt run.log at byte {reader.stop.offset}: {reader.stop.detail}"
                raise ValueError(message)
            if reader.stop.reason is RunLogStopReason.TORN_TAIL:
                stream.truncate(reader.stop.offset)
                if reader.stop.offset == 0:
                    stream.seek(0)
                    stream.write(RUN_LOG_MAGIC)

            stream.flush()
            os.fsync(stream.fileno())
            self._skeleton.log_offset = reader.stop.offset or len(RUN_LOG_MAGIC)

    def append(self, record: Record) -> None:
        """Commit one record whose sequence is exactly the next dense sequence."""
        if record.seq != self.sequence + 1:
            message = f"expected run.log seq {self.sequence + 1}, got {record.seq}"
            raise ValueError(message)
        frame = encode_frame(record)
        try:
            written = self._stream.write(frame)
            _check_write(written, len(frame))
            os.fsync(self._stream.fileno())
            append_skeleton(self._skeleton, record)
            self._skeleton.log_offset = self._stream.tell()
            self.sequence = record.seq
            self.epoch = max(self.epoch, record.epoch)
            self._checkpoint(record)
        except BaseException:
            self.close()
            raise

    def _checkpoint(self, record: Record) -> None:
        body = record.body
        if body and body.field == "epoch_started" and record.at is not None:
            self._process = RunProcess(pid=os.getpid(), epoch=record.epoch, started_at=record.at)
        self._publish_head()
        body = record.body
        boundary = body is not None and body.field in {"lifecycle", "epoch_started"}
        if boundary or record.seq - self._checkpoint_seq >= CHECKPOINT_RECORDS:
            self._publish_keyframe()

    def _publish_head(self) -> None:
        head = project_head(self._skeleton, self._process)
        if head is not None:
            validate(head)
            publish(self._path.parent / "run.head", head.to_binary())

    def _publish_keyframe(self) -> None:
        write_keyframe(self._path.parent / "keyframes", self._skeleton)
        self._checkpoint_seq = self.sequence

    def close(self) -> None:
        """Release the unbuffered append handle; safe to call more than once."""
        self._resources.close()

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


def _check_write(written: int | None, expected: int) -> None:
    if written != expected:
        message = "incomplete run.log append"
        raise OSError(message)
