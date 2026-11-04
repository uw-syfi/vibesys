"""The writer shell: open streams for owners, append their bytes, close them with an outcome."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

from vs_logs._core import (
    AppendBytes,
    DropSegment,
    ForgetPipesBefore,
    LogLimits,
    MarkPipe,
    StreamLayout,
    append,
    kept_marks,
)
from vs_logs._records import (
    INDEX_NAME,
    PIPES_NAME,
    ClosedRecord,
    CorruptLogError,
    OpenedRecord,
    StreamOutcome,
    complete_length,
    decode_index,
    decode_pipes,
    encode_index,
    encode_pipe,
    fold_index,
    read_all,
    segment_name,
    stream_id_for,
)
from vs_logs._volume import DirectoryVolume, MemoryVolume, Volume

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import TracebackType

    from pydantic import TypeAdapter

    from vs_sim.api import Lock, OutputStream, Threads


class LogStream:
    """One open stream: an :class:`~vs_sim.api.OutputSink` that closes with an outcome.

    Use it as a context manager: leaving normally closes it ``ENDED``, leaving by an
    exception closes it ``FAILED``, unless :meth:`close` already chose. Writes from two
    drain threads are safe; each call's bytes stay contiguous.

    Logs are for humans, so neither writing nor closing ever raises into the work being
    logged. When the store cannot write a chunk (a full disk), the stream is degraded:
    the store says so once through its ``warn``, drops every later byte of the stream,
    and records the stream's close as degraded. A write after close is dropped.
    """

    def __init__(self, store: _Writer, stream_id: str) -> None:
        """Write through ``store`` to ``stream_id``; made only by :meth:`LogScope.open`."""
        self._store = store
        self.stream_id = stream_id

    def write(self, stream: OutputStream, data: bytes) -> None:
        """Append ``data`` from pipe ``stream``; never raises."""
        self._store.write(self.stream_id, stream, data)

    def close(self, outcome: StreamOutcome) -> None:
        """Close with ``outcome``; closing again keeps the first outcome. Never raises."""
        self._store.close(self.stream_id, outcome)

    def __enter__(self) -> Self:
        """Return this stream."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close with ``FAILED`` when leaving by an exception, else ``ENDED``."""
        self.close(StreamOutcome.ENDED if exc_type is None else StreamOutcome.FAILED)


class LogScope[OwnerT](Protocol):
    """What a producer is handed: open a stream for an owner, and nothing else."""

    def open(self, owner: OwnerT, command: str) -> LogStream:
        """Open a new stream recording ``command``'s output for ``owner``."""
        ...


class _Writer(Protocol):
    def write(self, stream_id: str, pipe: OutputStream, data: bytes) -> None: ...

    def close(self, stream_id: str, outcome: StreamOutcome) -> None: ...


class LogStore[OwnerT]:
    """A run's log streams, each under a typed owner, durably indexed.

    Opening a store cuts a torn last index line (a short write to a full disk leaves
    one), then closes, as ``ABANDONED``, every stream a previous writer left open, so a
    reader never waits on a stream nobody will write. Opening raises ``OSError`` or
    ``CorruptLogError`` when it cannot do that. Once open, the store never raises into a
    producer: a failed write degrades its stream, and a failed index append drops that
    record after cutting the torn line it left (a stream the index does not list is
    never read). Each failure is said once through ``warn``. One store instance writes a
    directory at a time; readers in any process may read it meanwhile.
    """

    def __init__(
        self,
        volume: Volume,
        owner: TypeAdapter[OwnerT],
        *,
        threads: Threads,
        limits: LogLimits,
        warn: Callable[[str], object],
    ) -> None:
        """Write streams to ``volume``, owners validated by ``owner``, locked by ``threads``.

        ``warn`` hears, once per stream and once for the index, that the store failed to
        write: the one place a log failure shows, since it never reaches the producer.
        """
        self._volume = volume
        self._owner = owner
        self._limits = limits
        self._warn = warn
        self._lock: Lock = threads.lock()
        held = read_all(_index_source(volume))
        self._index_size = complete_length(held)
        if self._index_size < len(held):
            volume.truncate(INDEX_NAME, self._index_size)
        entries = fold_index(decode_index(held[: self._index_size], where=INDEX_NAME))
        self._opened = max((int(stream_id) for stream_id in entries), default=0)
        self._layouts: dict[str, StreamLayout] = {}
        self._open: set[str] = set()
        self._degraded: set[str] = set()
        self._index_failed = False
        self._index_writable = True
        for stream_id, entry in entries.items():
            if entry.closed is None:
                line = encode_index(
                    ClosedRecord(stream_id=stream_id, outcome=StreamOutcome.ABANDONED)
                )
                volume.append(INDEX_NAME, line)
                self._index_size += len(line)

    @property
    def volume(self) -> Volume:
        """Where the files are; a reader over the same volume sees what this store wrote."""
        return self._volume

    def open(self, owner: OwnerT, command: str) -> LogStream:
        """Open a new stream for ``owner`` recording ``command``'s output.

        An invalid ``owner`` raises (a programming error). When the index cannot record
        the stream, the stream is returned anyway and drops what is written to it.
        """
        owner_json = self._owner.dump_python(self._owner.validate_python(owner), mode="json")
        with self._lock:
            self._opened += 1
            stream_id = stream_id_for(self._opened)
            opened = OpenedRecord(stream_id=stream_id, owner=owner_json, command=command)
            listed, warning = self._append_index(opened)
            if listed:
                self._open.add(stream_id)
                self._layouts[stream_id] = StreamLayout()
        self._say(warning)
        return LogStream(self, stream_id)

    def write(self, stream_id: str, pipe: OutputStream, data: bytes) -> None:
        """Append ``data`` to an open stream (through its :class:`LogStream`); never raises."""
        warning = None
        with self._lock:
            layout = self._layouts.get(stream_id)
            if layout is None:
                return  # closed, degraded, or never listed: the bytes have nowhere to go
            new_layout, requests = append(layout, self._limits, pipe, len(data))
            try:
                for request in requests:
                    self._execute(stream_id, request, data)
            except (OSError, CorruptLogError) as exc:
                del self._layouts[stream_id]
                self._degraded.add(stream_id)
                warning = (
                    f"vibesys: log stream {stream_id} could not be written "
                    f"({type(exc).__name__}: {exc}); the rest of its output is not kept.\n"
                )
            else:
                self._layouts[stream_id] = new_layout
        self._say(warning)

    def close(self, stream_id: str, outcome: StreamOutcome) -> None:
        """Close an open stream; closing again keeps the first outcome. Never raises."""
        with self._lock:
            if stream_id not in self._open:
                return
            self._open.discard(stream_id)
            self._layouts.pop(stream_id, None)
            degraded = stream_id in self._degraded
            self._degraded.discard(stream_id)
            closed = ClosedRecord(stream_id=stream_id, outcome=outcome, degraded=degraded)
            _, warning = self._append_index(closed)
        self._say(warning)

    def _say(self, warning: str | None) -> None:
        # Outside the lock: `warn` may log, and a log record may come back to this store.
        if warning is not None:
            self._warn(warning)

    def _append_index(self, record: OpenedRecord | ClosedRecord) -> tuple[bool, str | None]:
        """Append ``record``: whether it was, and the warning to give when it was not.

        A failed append cuts the torn line it left, so the next record starts a line.
        When that cut fails too, nothing more is appended, since a later record would
        start mid-line and corrupt the index; the next store to open it cuts the line.
        Only the index's first failure has a warning, so a full disk is said once.
        """
        if not self._index_writable:
            return False, None
        line = encode_index(record)
        try:
            self._volume.append(INDEX_NAME, line)
        except OSError as exc:
            try:
                self._volume.truncate(INDEX_NAME, self._index_size)
            except OSError:
                self._index_writable = False
            if self._index_failed:
                return False, None
            self._index_failed = True
            return False, (
                f"vibesys: the log index could not be written ({exc}); "
                "streams it cannot record are not kept.\n"
            )
        self._index_size += len(line)
        return True, None

    def _execute(
        self,
        stream_id: str,
        request: MarkPipe | AppendBytes | DropSegment | ForgetPipesBefore,
        data: bytes,
    ) -> None:
        match request:
            case MarkPipe():
                self._volume.append(f"{stream_id}/{PIPES_NAME}", encode_pipe(request))
            case AppendBytes(segment=segment, chunk_from=chunk_from, chunk_to=chunk_to):
                self._volume.append(segment_name(stream_id, segment), data[chunk_from:chunk_to])
            case DropSegment(segment=segment):
                self._volume.remove(segment_name(stream_id, segment))
            case ForgetPipesBefore(offset=offset):
                name = f"{stream_id}/{PIPES_NAME}"
                marks = decode_pipes(read_all(self._volume.source(name)), where=name)
                kept = kept_marks(marks, offset)
                self._volume.replace(name, b"".join(encode_pipe(mark) for mark in kept))


def _index_source(volume: Volume) -> _EmptyWhenMissing:
    return _EmptyWhenMissing(volume, INDEX_NAME)


class _EmptyWhenMissing:
    """A volume file read as empty until it exists."""

    def __init__(self, volume: Volume, name: str) -> None:
        self._source = volume.source(name)

    def size(self) -> int:
        try:
            return self._source.size()
        except FileNotFoundError:
            return 0

    def read(self, offset: int, max_bytes: int) -> bytes:
        try:
            return self._source.read(offset, max_bytes)
        except FileNotFoundError:
            return b""


class FileLogStore[OwnerT](LogStore[OwnerT]):
    """A log store in a directory on this host (a run's log directory)."""

    def __init__(
        self,
        directory: Path,
        owner: TypeAdapter[OwnerT],
        *,
        threads: Threads,
        warn: Callable[[str], object],
        limits: LogLimits | None = None,
    ) -> None:
        """Keep the streams under ``directory``; say through ``warn`` when a write fails."""
        super().__init__(
            DirectoryVolume(directory),
            owner,
            threads=threads,
            limits=limits or LogLimits(),
            warn=warn,
        )


class MemoryLogStore[OwnerT](LogStore[OwnerT]):
    """The Fake: the same store over memory, held to the same contract suite."""

    def __init__(
        self,
        owner: TypeAdapter[OwnerT],
        *,
        threads: Threads,
        warn: Callable[[str], object],
        limits: LogLimits | None = None,
        volume: MemoryVolume | None = None,
    ) -> None:
        """Keep the streams in memory: in ``volume`` when given, to reopen or fill it."""
        self._memory = volume or MemoryVolume()
        super().__init__(
            self._memory, owner, threads=threads, limits=limits or LogLimits(), warn=warn
        )

    @property
    def memory_volume(self) -> MemoryVolume:
        """The memory this store keeps its files in, to open another store over it."""
        return self._memory
