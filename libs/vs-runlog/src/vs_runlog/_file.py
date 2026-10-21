"""Single-writer file storage with explicit tail recovery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from contextlib import suppress
from typing import TYPE_CHECKING, BinaryIO, Self

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType

    from vs_proto.api import Record
    from vs_sim.api import SleepingClock, Threads
from vs_proto.api import RUN_LOG_MAGIC, Keyframe, RunHead, RunProcess, validate
from vs_runlog._atomic import publish, sync_directory, write_keyframe
from vs_runlog._fold import append_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame, read_frame
from vs_runlog._head import project_head
from vs_runlog._lock import RunLock
from vs_sim.api import CloseOnce, ReleaseStack

CHECKPOINT_RECORDS = 256
COMMIT_INTERVAL = 0.1

_STREAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")


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
    Appends write immediately; commit makes the pending suffix durable before
    publishing run.head. With a running event loop, the first pending append
    schedules a commit within 100 ms (plus scheduling and storage latency).
    Synchronous/offline callers must commit explicitly or close. Lifecycle and
    epoch boundaries commit immediately. state.pb transactions are not wired yet.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: SleepingClock,
        threads: Threads,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Recover a log; pass the owning ``loop`` when constructing off-loop.

        ``clock`` drives deferred commits and ``threads`` supplies the lock that
        serializes worker appends with commits. Otherwise the current loop is
        used; offline writers commit explicitly.
        """
        self._mutex = threads.rlock()
        self._clock = clock
        self._timer: asyncio.Task[None] | None = None
        self._generation = 0
        self._failure: BaseException | None = None
        self._committed_seq = 0
        self._loop = loop
        if self._loop is None:
            with suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()
        self.sequence = 0
        self.epoch = 0
        self._path = path
        self._skeleton = Keyframe()
        self._checkpoint_seq = 0
        self._process: RunProcess | None = None
        self._resources = ReleaseStack()
        self._close = CloseOnce()
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
            self._stream = path.open("ab", buffering=0)
            self._resources.callback(self._stream.close)
            if self.sequence:
                self._publish_head()
                self._publish_keyframe()
                self._committed_seq = self.sequence
        except BaseException:
            self._resources.close()
            raise

    def _recover(self, path: Path) -> None:
        head_path = path.parent / "run.head"
        committed = 0
        if head_path.exists():
            head = RunHead.from_binary(head_path.read_bytes())
            validate(head)
            committed = head.committed_seq
        with path.open("r+b") as stream:
            reader = RunLogReader(stream)
            for record in reader:
                if record.seq != self.sequence + 1:
                    message = f"non-dense run.log sequence: {record.seq} after {self.sequence}"
                    raise ValueError(message)
                append_skeleton(self._skeleton, record)
                self.sequence = record.seq
                self.epoch = max(self.epoch, record.epoch)
            if self.sequence < committed:
                message = f"run.log lost committed records: head {committed}, log {self.sequence}"
                raise ValueError(message)
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
        """Write the next dense record; durability is acknowledged by ``commit``.

        A lifecycle or epoch header is an explicit commit boundary. Ordinary
        appends neither fsync nor publish projections, including at keyframe thresholds.
        """
        with self._mutex:
            self._check_open()
            if record.seq != self.sequence + 1:
                message = f"expected run.log seq {self.sequence + 1}, got {record.seq}"
                raise ValueError(message)
            frame = encode_frame(record)
            try:
                written = self._stream.write(frame)
                _check_write(written, len(frame))
                append_skeleton(self._skeleton, record)
                self._skeleton.log_offset = self._stream.tell()
                self.sequence = record.seq
                self.epoch = max(self.epoch, record.epoch)
                body = record.body
                if body and body.field == "epoch_started" and record.at is not None:
                    self._process = RunProcess(
                        pid=os.getpid(), epoch=record.epoch, started_at=record.at
                    )
                if body and body.field in {"lifecycle", "epoch_started"}:
                    self._commit(checkpoint=True)
                elif self.sequence == self._committed_seq + 1:
                    self._schedule_commit()
            except BaseException as error:
                self._abort(error)
                raise

    def commit(self) -> None:
        """Durably flush pending records, then publish head and any due keyframe.

        No pending records means no I/O. Failures close the writer and propagate;
        recovery fsyncs the complete surviving prefix before publishing it again.
        This is a log-only transaction, not a state.pb compare-and-swap.
        """
        with self._mutex:
            self._check_open()
            try:
                self._commit()
            except BaseException as error:
                self._abort(error)
                raise

    def _commit(self, *, checkpoint: bool = False) -> None:
        self._cancel_commit()
        if self.sequence == self._committed_seq:
            if checkpoint and self.sequence > self._checkpoint_seq:
                self._publish_keyframe()
            return
        os.fsync(self._stream.fileno())
        if checkpoint or self.sequence - self._checkpoint_seq >= CHECKPOINT_RECORDS:
            self._publish_keyframe()
        self._publish_head()
        self._committed_seq = self.sequence

    def _schedule_commit(self) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                return  # Offline producers commit explicitly and at close.
        due = self._clock.now() + COMMIT_INTERVAL
        self._loop.call_soon_threadsafe(self._start_timer, self._generation, due)

    def _start_timer(self, generation: int, due: float) -> None:
        with self._mutex:
            if generation == self._generation and not self._close.started:
                self._timer = asyncio.create_task(self._commit_after(generation, due))

    async def _commit_after(self, generation: int, due: float) -> None:
        await self._clock.sleep(max(0.0, due - self._clock.now()))
        with self._mutex:
            if generation != self._generation:
                return
            self._timer = None
            try:
                self.commit()
            except Exception:
                logging.getLogger(__name__).exception("Deferred run.log commit failed")

    def _cancel_commit(self) -> None:
        self._generation += 1
        if self._timer is not None:
            if self._loop is not None:
                # The owner may close after its loop has already shut down.
                # Generation fencing still prevents a stale timer from committing.
                with suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._timer.cancel)
            self._timer = None

    def _check_open(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._close.started:
            message = "run.log writer is closed"
            raise ValueError(message)

    def _abort(self, error: BaseException) -> None:
        self._failure = error
        self._cancel_commit()
        self._close.run(self._resources.close)

    def _publish_head(self) -> None:
        head = project_head(self._skeleton, self._process)
        if head is not None:
            validate(head)
            publish(self._path.parent / "run.head", head.to_binary())

    def _publish_keyframe(self) -> None:
        write_keyframe(self._path.parent / "keyframes", self._skeleton)
        self._checkpoint_seq = self.sequence

    def checkpoint(self) -> None:
        """Commit and checkpoint the current prefix, including a final conversion.

        Unlike periodic checkpoints this is explicit: callers can finish a batch
        at an arbitrary record and still open it without replaying its suffix.
        """
        with self._mutex:
            self._check_open()
            try:
                self._commit(checkpoint=True)
            except BaseException as error:
                self._abort(error)
                raise

    def close(self) -> None:
        """Commit the final suffix and release ownership, even when commit fails."""
        with self._mutex:
            if self._failure is not None:
                self._close.run(self._resources.close)
                raise self._failure
            if self._close.started:
                self._close.run(self._resources.close)
                return
            try:
                self._commit(checkpoint=True)
            finally:
                self._cancel_commit()
                self._close.run(self._resources.close)

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


def install_run_log(staged: Path, destination: Path) -> None:
    """Install a quiescent conversion with its head and keyframes.

    The staged log lives in a disposable sibling directory. The authoritative
    JSONL source is untouched; old derived keyframes must not survive a rewrite.
    """
    if staged.parent == destination.parent:
        message = "staged run.log needs a separate disposable directory"
        raise ValueError(message)
    lock = RunLock(destination.parent / "run.lock")
    try:
        keyframes = destination.parent / "keyframes"
        if keyframes.exists():
            keyframes.replace(staged.parent / "previous-keyframes")
        staged.replace(destination)
        staged_keyframes = staged.parent / "keyframes"
        if staged_keyframes.exists():
            staged_keyframes.replace(keyframes)
        head = destination.parent / "run.head"
        staged_head = staged.parent / "run.head"
        if staged_head.exists():
            staged_head.replace(head)
        else:
            head.unlink(missing_ok=True)
        sync_directory(destination.parent)
    finally:
        lock.close()


def _check_write(written: int | None, expected: int) -> None:
    if written != expected:
        message = "incomplete run.log append"
        raise OSError(message)


class FileRunContent:
    """Immutable blobs and replayable stream appends at caller-owned paths.

    Replaying an identical write succeeds. A hash mismatch, unsafe stream id,
    missing prefix or conflicting content raises rather than overwriting data.
    Every file handle is scoped to one operation.
    """

    def __init__(self, blobs: Path, streams: Path) -> None:
        """Use layout paths supplied by the project, without discovering a project."""
        self._blobs = blobs
        self._streams = streams

    def write_blob(self, digest: str, content: bytes) -> None:
        """Create immutable content only when its supplied hash matches its bytes."""
        if hashlib.sha256(content).hexdigest() != digest:
            message = "blob digest does not match content"
            raise ValueError(message)
        self._blobs.mkdir(parents=True, exist_ok=True)
        path = self._blobs / digest
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != content:
                message = f"blob {digest} conflicts with existing content"
                raise ValueError(message) from None
        sync_directory(self._blobs)
        sync_directory(self._blobs.parent)

    def append_stream(self, stream_id: str, offset: int, content: bytes) -> None:
        """Append once at an exact offset, or verify a previously written prefix."""
        if not _STREAM_ID.fullmatch(stream_id) or offset < 0:
            message = f"invalid stream id or offset: {stream_id!r}, {offset}"
            raise ValueError(message)
        self._streams.mkdir(parents=True, exist_ok=True)
        path = self._streams / stream_id
        with path.open("a+b") as stream:
            size = stream.tell()
            if size < offset:
                message = f"stream {stream_id}: missing prefix through {offset}"
                raise ValueError(message)
            stream.seek(offset)
            existing = stream.read(len(content))
            if existing != content[: len(existing)]:
                message = f"stream {stream_id}: conflicting content at {offset}"
                raise ValueError(message)
            stream.seek(0, 2)
            stream.write(content[len(existing) :])
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(self._streams)
        sync_directory(self._streams.parent)
