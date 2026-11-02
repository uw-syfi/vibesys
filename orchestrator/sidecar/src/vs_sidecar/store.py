"""The sidecar's memory: an offset-numbered event log, a raw frame log and the effect ids seen.

Two implementations satisfy ``Store``: ``MemoryStore`` for tests and ``FileStore`` for a session.
Both pass the contract tests in ``tests/test_store.py``. A crash can tear the last record of a file,
so the readers keep the whole records and stop at the first torn one; the next append continues
from there.

Every method is safe to call from several threads. ``FileStore`` reaches the disk through
``Files``, so tests give it a file that fails or tears at a chosen write.
"""

from __future__ import annotations

import json
import os
import struct
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from google.protobuf.message import DecodeError

from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_LENGTH = struct.Struct(">I")
#: A record longer than this is a torn or corrupt length, not a record.
MAX_RECORD = 64 * 1024 * 1024


class FrameDirection(StrEnum):
    """Which way a raw frame crossed the provider's pipe."""

    TO_PROVIDER = "to_provider"
    FROM_PROVIDER = "from_provider"


@dataclass(frozen=True)
class RawRecord:
    """One raw provider frame, tied to the event log by the offset it followed."""

    after_offset: int
    direction: FrameDirection
    text: str


class Store(Protocol):
    """What a session remembers across a restart of its sidecar."""

    def append(self, event: sc.SidecarEvent) -> sc.SidecarEvent:
        """Number *event* with the next offset and keep it. Returns the numbered event."""
        ...

    def events_after(self, offset: int) -> list[sc.SidecarEvent]:
        """The kept events with an offset above *offset*, in order."""
        ...

    def last_offset(self) -> int:
        """The offset of the last kept event, or 0."""
        ...

    def append_raw(self, direction: FrameDirection, text: str) -> None:
        """Keep one raw frame, after the last event."""
        ...

    def raw_frames(self) -> list[RawRecord]:
        """Every kept raw frame, in order."""
        ...

    def remember_effect(self, effect_id: str) -> bool:
        """Note an effect id. ``True`` when it is new, ``False`` when it was seen before."""
        ...


class StoreError(OSError):
    """The store can no longer promise that what it holds is what it reported.

    Raised when a write failed and the file could not be put back as it was. Every later append
    raises it too; the process should end and let a restart repair the file.
    """


class LogFile(Protocol):
    """One append-only file."""

    def read(self) -> bytes:
        """Everything in the file (empty when it does not exist yet)."""
        ...

    def append(self, data: bytes, *, durable: bool) -> None:
        """Add *data* at the end; with *durable*, not return before it is on disk."""
        ...

    def truncate(self, size: int) -> None:
        """Cut the file to *size* bytes."""
        ...


class Files(Protocol):
    """The files of one session's store, by name."""

    def open(self, name: str) -> LogFile:
        """The file called *name*, created empty on first use."""
        ...


class DiskLog:
    """A ``LogFile`` on disk."""

    def __init__(self, path: Path) -> None:
        """Use the file at *path*."""
        self._path = path

    def read(self) -> bytes:
        """See ``LogFile.read``."""
        return self._path.read_bytes() if self._path.exists() else b""

    def append(self, data: bytes, *, durable: bool) -> None:
        """See ``LogFile.append``."""
        with self._path.open("ab") as handle:
            handle.write(data)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())

    def truncate(self, size: int) -> None:
        """See ``LogFile.truncate``."""
        if self._path.exists() and self._path.stat().st_size != size:
            os.truncate(self._path, size)


class DiskFiles:
    """``Files`` in a directory."""

    def __init__(self, directory: Path) -> None:
        """Keep the files in *directory*, creating it."""
        directory.mkdir(parents=True, exist_ok=True)
        self._directory = directory

    def open(self, name: str) -> LogFile:
        """See ``Files.open``."""
        return DiskLog(self._directory / name)


def encode_events(events: Iterable[sc.SidecarEvent]) -> bytes:
    """The event log format: each event as a 4-byte big-endian length, then its bytes."""
    out = bytearray()
    for event in events:
        body = event.SerializeToString(deterministic=True)
        out += _LENGTH.pack(len(body)) + body
    return bytes(out)


def decode_events(data: bytes) -> tuple[list[sc.SidecarEvent], int]:
    """The whole events in *data* and the number of bytes they take.

    Stops at the first torn record (a short length or body, an oversized length, or bytes that
    are not an event) without raising, so a crash mid-write loses only the record it tore. Total
    over any input: the fuzz target.
    """
    events: list[sc.SidecarEvent] = []
    at = 0
    while at + _LENGTH.size <= len(data):
        (size,) = _LENGTH.unpack_from(data, at)
        end = at + _LENGTH.size + size
        if size > MAX_RECORD or end > len(data):
            break
        event = sc.SidecarEvent()
        try:
            event.ParseFromString(data[at + _LENGTH.size : end])
        except DecodeError:
            break
        events.append(event)
        at = end
    return events, at


def decode_lines(data: bytes) -> tuple[list[str], int]:
    """The complete (newline-ended) UTF-8 lines in *data*, and the bytes they take."""
    lines: list[str] = []
    at = 0
    while (end := data.find(b"\n", at)) != -1:
        try:
            lines.append(data[at:end].decode())
        except UnicodeDecodeError:
            break
        at = end + 1
    return lines, at


def _raw_line(record: RawRecord) -> str:
    return json.dumps(
        {"after": record.after_offset, "dir": record.direction.value, "text": record.text},
        separators=(",", ":"),
    )


def _parse_raw(line: str) -> RawRecord | None:
    try:
        value = json.loads(line)
        return RawRecord(int(value["after"]), FrameDirection(value["dir"]), str(value["text"]))
    except (ValueError, KeyError, TypeError):
        return None


def _parse_effect(line: str) -> str | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


class MemoryStore:
    """A ``Store`` in memory."""

    def __init__(
        self,
        events: Iterable[sc.SidecarEvent] = (),
        raw: Iterable[RawRecord] = (),
        effects: Iterable[str] = (),
    ) -> None:
        """Start with the given history (empty by default)."""
        self._lock = threading.Lock()
        self._events = list(events)
        self._raw = list(raw)
        self._effects = set(effects)

    def _last(self) -> int:
        return self._events[-1].offset if self._events else 0

    def numbered(self, event: sc.SidecarEvent) -> sc.SidecarEvent:
        """A copy of *event* with the offset it would take next. Keeps nothing."""
        with self._lock:
            numbered = sc.SidecarEvent()
            numbered.CopyFrom(event)
            numbered.offset = self._last() + 1
            return numbered

    def keep(self, numbered: sc.SidecarEvent) -> None:
        """Keep an event ``numbered`` gave out; it must still be the next offset."""
        with self._lock:
            if numbered.offset != self._last() + 1:
                msg = f"offset {numbered.offset} is not the next offset {self._last() + 1}"
                raise ValueError(msg)
            self._events.append(numbered)

    def append(self, event: sc.SidecarEvent) -> sc.SidecarEvent:
        """See ``Store.append``."""
        with self._lock:
            numbered = sc.SidecarEvent()
            numbered.CopyFrom(event)
            numbered.offset = self._last() + 1
            self._events.append(numbered)
            return numbered

    def events_after(self, offset: int) -> list[sc.SidecarEvent]:
        """See ``Store.events_after``."""
        with self._lock:
            return [event for event in self._events if event.offset > offset]

    def last_offset(self) -> int:
        """See ``Store.last_offset``."""
        with self._lock:
            return self._last()

    def append_raw(self, direction: FrameDirection, text: str) -> None:
        """See ``Store.append_raw``."""
        self.keep_raw(RawRecord(self.last_offset(), direction, text))

    def keep_raw(self, record: RawRecord) -> None:
        """Keep a raw record."""
        with self._lock:
            self._raw.append(record)

    def raw_frames(self) -> list[RawRecord]:
        """See ``Store.raw_frames``."""
        with self._lock:
            return list(self._raw)

    def remember_effect(self, effect_id: str) -> bool:
        """See ``Store.remember_effect``."""
        with self._lock:
            if effect_id in self._effects:
                return False
            self._effects.add(effect_id)
            return True

    def forget_effect(self, effect_id: str) -> None:
        """Take back an effect id ``remember_effect`` just accepted."""
        with self._lock:
            self._effects.discard(effect_id)


class FileStore:
    """A ``Store`` over three files: ``events.log``, ``raw.log`` and ``effects.log``.

    Opening it reads what is there and cuts a torn tail off each file, so a crash costs at most
    the record being written. An event is written and synced first and kept in memory second, so
    what ``events_after`` returns is always on disk, and a write that fails is cut off the file
    again before the error reaches the caller: the log never holds what a reader was not shown,
    and an offset is never given to two different events. Raw frames are written without a sync
    (they are diagnostics, and a torn tail is cut on open), so a frame never waits on the disk.
    """

    def __init__(self, files: Files) -> None:
        """Open the store in *files*, repairing a torn tail."""
        self._events = _Log(files.open("events.log"))
        self._raw = _Log(files.open("raw.log"))
        self._effects = _Log(files.open("effects.log"))
        events, size = decode_events(self._events.file.read())
        self._events.settle(size)
        raw_lines, size = decode_lines(self._raw.file.read())
        self._raw.settle(size)
        effect_lines, size = decode_lines(self._effects.file.read())
        self._effects.settle(size)
        self._memory = MemoryStore(
            events,
            [r for line in raw_lines if (r := _parse_raw(line)) is not None],
            [e for line in effect_lines if (e := _parse_effect(line)) is not None],
        )

    @classmethod
    def at(cls, directory: Path) -> FileStore:
        """The store in *directory* on disk."""
        return cls(DiskFiles(directory))

    def append(self, event: sc.SidecarEvent) -> sc.SidecarEvent:
        """See ``Store.append``. Raises the write's error, with the file as it was."""
        with self._events.lock:
            numbered = self._memory.numbered(event)
            self._events.write(encode_events([numbered]), durable=True)
            self._memory.keep(numbered)
            return numbered

    def events_after(self, offset: int) -> list[sc.SidecarEvent]:
        """See ``Store.events_after``."""
        return self._memory.events_after(offset)

    def last_offset(self) -> int:
        """See ``Store.last_offset``."""
        return self._memory.last_offset()

    def append_raw(self, direction: FrameDirection, text: str) -> None:
        """See ``Store.append_raw``."""
        with self._raw.lock:
            record = RawRecord(self._memory.last_offset(), direction, text)
            self._raw.write((_raw_line(record) + "\n").encode(), durable=False)
            self._memory.keep_raw(record)

    def raw_frames(self) -> list[RawRecord]:
        """See ``Store.raw_frames``."""
        return self._memory.raw_frames()

    def remember_effect(self, effect_id: str) -> bool:
        """See ``Store.remember_effect``."""
        with self._effects.lock:
            if not self._memory.remember_effect(effect_id):
                return False
            try:
                self._effects.write((json.dumps(effect_id) + "\n").encode(), durable=True)
            except BaseException:
                self._memory.forget_effect(effect_id)
                raise
            return True


class _Log:
    """One file of a ``FileStore`` and the size it is known to have."""

    def __init__(self, file: LogFile) -> None:
        self.file = file
        self.lock = threading.Lock()
        self._size = 0
        self._broken = False

    def settle(self, size: int) -> None:
        """Cut the file to *size*, the length of its whole records."""
        self.file.truncate(size)
        self._size = size

    def write(self, data: bytes, *, durable: bool) -> None:
        """Add *data*, or leave the file as it was and raise."""
        if self._broken:
            msg = "an earlier write could not be undone"
            raise StoreError(msg)
        try:
            self.file.append(data, durable=durable)
        except OSError:
            try:
                self.file.truncate(self._size)
            except OSError as undo:
                self._broken = True
                msg = "a write failed and could not be undone"
                raise StoreError(msg) from undo
            raise
        self._size += len(data)
