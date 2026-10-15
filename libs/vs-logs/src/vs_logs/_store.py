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
    OpenedRecord,
    StreamOutcome,
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
    from pathlib import Path
    from types import TracebackType

    from pydantic import TypeAdapter

    from vs_sim.api import Lock, OutputStream, Threads


class LogStream:
    """One open stream: an :class:`~vs_sim.api.OutputSink` that closes with an outcome.

    Use it as a context manager: leaving normally closes it ``ENDED``, leaving by an
    exception closes it ``FAILED``, unless :meth:`close` already chose. Writes from two
    drain threads are safe; each call's bytes stay contiguous. Writing after close is a
    programming error and raises ``RuntimeError``.
    """

    def __init__(self, store: _Writer, stream_id: str) -> None:
        """Write through ``store`` to ``stream_id``; made only by :meth:`LogScope.open`."""
        self._store = store
        self.stream_id = stream_id

    def write(self, stream: OutputStream, data: bytes) -> None:
        """Append ``data`` from pipe ``stream``."""
        self._store.write(self.stream_id, stream, data)

    def close(self, outcome: StreamOutcome) -> None:
        """Close with ``outcome``; closing again keeps the first outcome."""
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


class AdoptedLog:
    """A file someone else writes, listed in the store; closing records how it ended."""

    def __init__(self, store: _Writer, stream_id: str) -> None:
        """Close ``stream_id`` through ``store``; made only by :meth:`LogStore.adopt`."""
        self._store = store
        self.stream_id = stream_id

    def close(self, outcome: StreamOutcome) -> None:
        """Close with ``outcome``; closing again keeps the first outcome."""
        self._store.close(self.stream_id, outcome)

    def __enter__(self) -> Self:
        """Return this log."""
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

    Opening a store closes, as ``ABANDONED``, every stream a previous writer left open, so a
    reader never waits on a stream nobody will write. One store instance writes a
    directory at a time; readers in any process may read it meanwhile.
    """

    def __init__(
        self,
        volume: Volume,
        owner: TypeAdapter[OwnerT],
        *,
        threads: Threads,
        limits: LogLimits,
    ) -> None:
        """Write streams to ``volume``, owners validated by ``owner``, locked by ``threads``."""
        self._volume = volume
        self._owner = owner
        self._limits = limits
        self._lock: Lock = threads.lock()
        entries = fold_index(decode_index(read_all(_index_source(volume)), where=INDEX_NAME))
        self._opened = len(entries)
        self._layouts: dict[str, StreamLayout] = {}
        self._open: set[str] = set()
        for stream_id, entry in entries.items():
            if entry.closed is None:
                self._append_index(
                    ClosedRecord(stream_id=stream_id, outcome=StreamOutcome.ABANDONED)
                )

    @property
    def volume(self) -> Volume:
        """Where the files are; a reader over the same volume sees what this store wrote."""
        return self._volume

    def open(self, owner: OwnerT, command: str) -> LogStream:
        """Open a new stream for ``owner`` recording ``command``'s output."""
        stream_id = self._register(owner, command, adopted_path=None)
        with self._lock:
            self._layouts[stream_id] = StreamLayout()
        return LogStream(self, stream_id)

    def adopt(self, path: str, owner: OwnerT, command: str) -> AdoptedLog:
        """List the file at ``path``, which someone else writes, as a stream read as-is."""
        return AdoptedLog(self, self._register(owner, command, adopted_path=path))

    def write(self, stream_id: str, pipe: OutputStream, data: bytes) -> None:
        """Append ``data`` to an open stream (through its :class:`LogStream`)."""
        with self._lock:
            layout = self._layouts.get(stream_id)
            if stream_id not in self._open or layout is None:
                message = f"log stream {stream_id} is not open for writing"
                raise RuntimeError(message)
            new_layout, requests = append(layout, self._limits, pipe, len(data))
            for request in requests:
                self._execute(stream_id, request, data)
            self._layouts[stream_id] = new_layout

    def close(self, stream_id: str, outcome: StreamOutcome) -> None:
        """Close an open stream; closing again keeps the first outcome."""
        with self._lock:
            if stream_id not in self._open:
                return
            self._open.discard(stream_id)
            self._layouts.pop(stream_id, None)
            self._append_index(ClosedRecord(stream_id=stream_id, outcome=outcome))

    def _register(self, owner: OwnerT, command: str, *, adopted_path: str | None) -> str:
        owner_json = self._owner.dump_python(self._owner.validate_python(owner), mode="json")
        with self._lock:
            self._opened += 1
            stream_id = stream_id_for(self._opened)
            self._append_index(
                OpenedRecord(
                    stream_id=stream_id,
                    owner=owner_json,
                    command=command,
                    adopted_path=adopted_path,
                )
            )
            self._open.add(stream_id)
            return stream_id

    def _append_index(self, record: OpenedRecord | ClosedRecord) -> None:
        self._volume.append(INDEX_NAME, encode_index(record))

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
        limits: LogLimits | None = None,
    ) -> None:
        """Keep the streams under ``directory``."""
        super().__init__(
            DirectoryVolume(directory), owner, threads=threads, limits=limits or LogLimits()
        )


class MemoryLogStore[OwnerT](LogStore[OwnerT]):
    """The Fake: the same store over memory, held to the same contract suite."""

    def __init__(
        self,
        owner: TypeAdapter[OwnerT],
        *,
        threads: Threads,
        limits: LogLimits | None = None,
        volume: MemoryVolume | None = None,
    ) -> None:
        """Keep the streams in memory (in ``volume`` when given, to reopen it)."""
        self._memory = volume or MemoryVolume()
        super().__init__(self._memory, owner, threads=threads, limits=limits or LogLimits())

    @property
    def memory_volume(self) -> MemoryVolume:
        """The memory this store keeps its files in, to open another store over it."""
        return self._memory

    def append_external(self, path: str, data: bytes) -> None:
        """Append to the memory file ``path``, as another writer appends to an adopted log."""
        self._memory.append(path, data)
