"""Single-writer file storage with explicit tail recovery."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import islice
from typing import TYPE_CHECKING, BinaryIO, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import TracebackType

    from vs_sim.api import SleepingClock, Threads
from vs_proto.api import RUN_LOG_MAGIC, Keyframe, Record, RunHead, RunProcess, validate
from vs_runlog._atomic import FileSync, OsFileSync, publish, sync_directory, write_keyframe
from vs_runlog._epoch import CurrentEpoch, current_epoch
from vs_runlog._fold import append_skeleton
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame, read_frame
from vs_runlog._head import project_head
from vs_runlog._lock import RunLock, hold_writer_identity
from vs_runlog._upgrade import InputUpgrade
from vs_sim.api import CloseOnce, ReleaseStack

CHECKPOINT_RECORDS = 256
COMMIT_INTERVAL = 0.1

_STREAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")


@dataclass(frozen=True)
class _WaitCommit:
    delay: float | None = None


@dataclass(frozen=True)
class _CommitBatch:
    checkpoint: bool


@dataclass(frozen=True)
class _CommitIntent:
    """Pure deadline and boundary policy; the shell owns buffers and I/O."""

    due: float | None = None
    urgent_seq: int = 0
    checkpoint_seq: int = 0
    closing: bool = False

    def queued(self, at: float) -> Self:
        return self if self.due is not None else replace(self, due=at + COMMIT_INTERVAL)

    def requested(self, target: int, *, checkpoint: bool, closing: bool) -> Self:
        return replace(
            self,
            urgent_seq=max(self.urgent_seq, target),
            checkpoint_seq=max(self.checkpoint_seq, target if checkpoint else 0),
            closing=self.closing or closing,
        )

    def taken(self) -> Self:
        return replace(self, due=None)

    def next(
        self, now: float, committed_seq: int, checkpoint_seq: int, *, failed: bool
    ) -> _WaitCommit | _CommitBatch | None:
        if failed:
            return None
        checkpoint = self.checkpoint_seq > checkpoint_seq
        if self.due is None:
            if checkpoint:
                return _CommitBatch(checkpoint=True)
            return None if self.closing else _WaitCommit()
        delay = max(0.0, self.due - now)
        if delay and not checkpoint and self.urgent_seq <= committed_seq:
            return _WaitCommit(delay)
        return _CommitBatch(checkpoint)


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


@contextmanager
def open_run_log(path: Path, offset: int = 0) -> Iterator[RunLogReader]:
    """Own the file handle while reading from a validated frame boundary."""
    with path.open("rb") as stream:
        yield RunLogReader(stream, offset)


class FileRunLogWriter:
    """Append dense sequences, recovering only an incomplete final frame on open.

    One process owns a file. Complete corruption is never repaired implicitly.
    Appends write immediately; a dedicated committer fsyncs and publishes the
    pending prefix outside the append lock. The first pending append sets a
    100 ms deadline independent of the caller's event loop. Storage and worker
    scheduling can delay completion. Lifecycle and epoch boundaries wait for
    durability. state.pb transactions are not wired yet.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: SleepingClock,
        threads: Threads,
        sync: FileSync | None = None,
        headline: Callable[[Keyframe, RunProcess | None], RunHead | None] = project_head,
    ) -> None:
        """Recover and own a committer with caller-selected headline projection.

        ``clock`` sets deadlines; condition timeouts use the injected ``threads``
        timeline. ``sync`` is the log durability operation (also used in recovery).
        """
        self._headline = headline
        self._condition = threads.condition()
        self._clock = clock
        self._sync = OsFileSync() if sync is None else sync
        self._pending: list[tuple[Record, int]] = []
        self._intent = _CommitIntent()
        self._done = False
        self._failure: BaseException | None = None
        self._committed_seq = 0
        self.sequence = 0
        self._epoch = CurrentEpoch()
        self._path = path
        # The writer folds every record of the log, recovered ones included.
        self._skeleton = Keyframe(failures_tracked=True, records_tracked=True)
        # The input fact a retained record leaves implicit, advanced once per record.
        self._upgrade = InputUpgrade()
        self._checkpoint_seq = 0
        self._process: RunProcess | None = None
        self._resources = ReleaseStack()
        self._close = CloseOnce()
        self._cleanup_lock = threads.lock()
        try:
            lock = RunLock(path.parent / "run.lock")
            self._resources.callback(lock.close)
            if not path.exists():
                with path.open("xb") as stream:
                    stream.write(RUN_LOG_MAGIC)
                    stream.flush()
                    self._sync(stream.fileno())
                sync_directory(path.parent, sync=self._sync)
            else:
                self._recover(path)
            self._stream = path.open("ab", buffering=0)
            self._resources.callback(self._stream.close)
            if self.sequence:
                self._publish_head()
                self._publish_keyframe()
                self._committed_seq = self.sequence
            self._worker = threads.spawn(self._run_committer, name="run-log-committer")
        except BaseException:
            self._resources.close()
            raise

    @property
    def epoch(self) -> int:
        """Current epoch derived from the writer's committed/accepted record prefix."""
        return self._epoch.number

    @property
    def current_epoch(self) -> CurrentEpoch:
        """Owned epoch facts; callers cannot mutate the writer's projection."""
        return current_epoch((), self._epoch)

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
                self._upgrade.observe(record)
                self.sequence = record.seq
                self._epoch = current_epoch((record,), self._epoch)
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
            self._sync(stream.fileno())
            self._skeleton.log_offset = reader.stop.offset or len(RUN_LOG_MAGIC)

    def append(self, record: Record) -> None:
        """Append without waiting for commit I/O; boundaries wait for durability."""
        frame = encode_frame(record)
        # The caller may reuse or mutate its protobuf after append returns.
        owned = Record.from_binary(record.to_binary())
        boundary = _is_boundary(owned)
        with self._condition:
            self._check_open()
            if owned.seq != self.sequence + 1:
                message = f"expected run.log seq {self.sequence + 1}, got {owned.seq}"
                raise ValueError(message)
            try:
                written = self._stream.write(frame)
                _check_write(written, len(frame))
                offset = self._stream.tell()
            except BaseException as error:
                self._failure = error
                self._condition.notify_all()
                raise
            self._intent = self._intent.queued(self._clock.now())
            self._pending.append((owned, offset))
            self.sequence = owned.seq
            self._epoch = current_epoch((owned,), self._epoch)
            if boundary:
                self._request_commit(owned.seq, checkpoint=True)
            self._condition.notify_all()
            if boundary:
                self._wait_committed(owned.seq, checkpoint=True)

    def commit(self) -> None:
        """Wait for the current prefix to become durable, including its published head.

        An empty commit does no I/O. Failures abort the writer and propagate;
        recovery fsyncs the surviving prefix before publishing it again.
        """
        with self._condition:
            self._check_open()
            target = self.sequence
            self._request_commit(target)
            self._wait_committed(target)

    def read(self) -> tuple[Record, ...]:
        """Copy the committed prefix under writer coordination for epoch recovery."""
        with self._condition:
            self._check_open()
            with self._path.open("rb") as stream:
                return tuple(islice(RunLogReader(stream), self._committed_seq))

    def _request_commit(
        self, target: int, *, checkpoint: bool = False, closing: bool = False
    ) -> None:
        self._intent = self._intent.requested(target, checkpoint=checkpoint, closing=closing)
        self._condition.notify_all()

    def _wait_committed(self, target: int, *, checkpoint: bool = False) -> None:
        while self._failure is None and (
            self._committed_seq < target or (checkpoint and self._checkpoint_seq < target)
        ):
            self._condition.wait()
        if self._failure is not None:
            while not self._done:
                self._condition.wait()
            raise self._failure

    def _take_batch(self) -> tuple[list[tuple[Record, int]], bool] | None:
        """Execute the pure policy's requests under the handoff lock."""
        with self._condition:
            while True:
                action = self._intent.next(
                    self._clock.now(),
                    self._committed_seq,
                    self._checkpoint_seq,
                    failed=self._failure is not None,
                )
                match action:
                    case None:
                        return None
                    case _WaitCommit(delay):
                        self._condition.wait(delay)
                    case _CommitBatch(checkpoint):
                        batch, self._pending = self._pending, []
                        self._intent = self._intent.taken()
                        return batch, checkpoint

    def _run_committer(self) -> None:
        try:
            with self._capture_failure():
                while (work := self._take_batch()) is not None:
                    batch, checkpoint = work
                    self._commit_batch(batch, checkpoint=checkpoint)
                    with self._condition:
                        self._committed_seq = self._skeleton.through_seq
                        self._condition.notify_all()
        finally:
            try:
                with self._capture_failure():
                    self._close.run(self._resources.close)
            finally:
                with self._condition:
                    self._done = True
                    self._condition.notify_all()

    @contextmanager
    def _capture_failure(self) -> Iterator[None]:
        try:
            yield
        except BaseException as error:
            with self._condition:
                first_failure = self._failure is None
                if first_failure:
                    self._failure = error
            if first_failure:
                logging.getLogger(__name__).exception("Deferred run.log commit failed")

    def _commit_batch(self, batch: list[tuple[Record, int]], *, checkpoint: bool) -> None:
        if batch:
            self._sync(self._stream.fileno())
        for record, offset in batch:
            append_skeleton(self._skeleton, record)
            self._upgrade.observe(record)
            self._skeleton.log_offset = offset
            if (
                record.body
                and record.body.field == "epoch_started"
                and record.at is not None
                and (self._process is None or record.epoch > self._process.epoch)
            ):
                self._process = RunProcess(
                    pid=os.getpid(), epoch=record.epoch, started_at=record.at
                )
                identity = hold_writer_identity(self._path, self._process)
                self._resources.callback(identity.close)
            if _is_boundary(record):
                self._publish_keyframe()
        through = self._skeleton.through_seq
        if (checkpoint and through > self._checkpoint_seq) or (
            through - self._checkpoint_seq >= CHECKPOINT_RECORDS
        ):
            self._publish_keyframe()
        if batch:
            self._publish_head()

    def _check_open(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._intent.closing:
            message = "run.log writer is closed"
            raise ValueError(message)

    def _publish_head(self) -> None:
        head = self._headline(self._skeleton, self._process)
        if head is not None:
            objective = head.objective
            if objective is not None and not objective.has_field("input_value"):
                measured = self._upgrade.measured
                if measured is not None:
                    objective.input_value = measured
            validate(head)
            publish(self._path.parent / "run.head", head.to_binary(), sync=self._sync)

    def _publish_keyframe(self) -> None:
        write_keyframe(self._path.parent / "keyframes", self._skeleton, sync=self._sync)
        self._checkpoint_seq = self._skeleton.through_seq

    def checkpoint(self) -> None:
        """Commit and checkpoint the current prefix, including a final conversion.

        Unlike periodic checkpoints this is explicit: callers can finish a batch
        at an arbitrary record and still open it without replaying its suffix.
        """
        with self._condition:
            self._check_open()
            target = self.sequence
            self._request_commit(target, checkpoint=True)
            self._wait_committed(target, checkpoint=True)

    def close(self) -> None:
        """Drain and join the committer, releasing ownership even after a failure."""
        with self._condition:
            self._request_commit(self.sequence, checkpoint=True, closing=True)
            while not self._done:
                self._condition.wait()
        self._worker.join()
        with self._cleanup_lock:
            self._close.run(self._resources.close)
        if self._failure is not None:
            raise self._failure

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


def install_run_log(
    staged: Path,
    destination: Path,
    *,
    prepare: Callable[[], None] | None = None,
    sync: FileSync | None = None,
) -> None:
    """Install a quiescent conversion with its head and keyframes.

    The staged log lives in a disposable sibling directory. The authoritative
    JSONL source is untouched; old derived keyframes must not survive a rewrite.
    When preparation writes shared content, pass it as ``prepare`` so the writer
    lock covers both content generation and installation.
    """
    if staged.parent == destination.parent:
        message = "staged run.log needs a separate disposable directory"
        raise ValueError(message)
    durable = OsFileSync() if sync is None else sync
    lock = RunLock(destination.parent / "run.lock")
    try:
        if prepare is not None:
            prepare()
        sync_directory(staged.parent, sync=durable)
        keyframes = destination.parent / "keyframes"
        if keyframes.exists():
            keyframes.replace(staged.parent / "previous-keyframes")
            sync_directory(destination.parent, sync=durable)
        staged.replace(destination)
        sync_directory(destination.parent, sync=durable)
        staged_keyframes = staged.parent / "keyframes"
        if staged_keyframes.exists():
            staged_keyframes.replace(keyframes)
            sync_directory(destination.parent, sync=durable)
        head = destination.parent / "run.head"
        staged_head = staged.parent / "run.head"
        if staged_head.exists():
            staged_head.replace(head)
        else:
            head.unlink(missing_ok=True)
        sync_directory(destination.parent, sync=durable)
    finally:
        lock.close()


def _is_boundary(record: Record) -> bool:
    return bool(
        record.body and record.body.field in {"lifecycle", "epoch_started", "declaration_amended"}
    )


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

    def __init__(self, blobs: Path, streams: Path, *, sync: FileSync | None = None) -> None:
        """Use layout paths supplied by the project, without discovering a project."""
        self._sync = OsFileSync() if sync is None else sync
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
                self._sync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != content:
                message = f"blob {digest} conflicts with existing content"
                raise ValueError(message) from None
        sync_directory(self._blobs, sync=self._sync)
        sync_directory(self._blobs.parent, sync=self._sync)

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
            self._sync(stream.fileno())
        sync_directory(self._streams, sync=self._sync)
        sync_directory(self._streams.parent, sync=self._sync)
