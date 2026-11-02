"""The sidecar's memory: an offset-numbered event log, a raw frame log and the effect ids seen.

Two implementations satisfy ``Store``: ``MemoryStore`` for tests and ``FileStore`` for a session.
Both pass ``tests/store_contract.py``. A crash can tear the last record of a file, so the readers
keep the whole records and stop at the first torn one; the next append continues from there.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

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
        except Exception:  # noqa: BLE001 - protobuf raises DecodeError and subclasses of Exception alike; any parse failure is a torn record
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
        self._events = list(events)
        self._raw = list(raw)
        self._effects = set(effects)

    def append(self, event: sc.SidecarEvent) -> sc.SidecarEvent:
        """See ``Store.append``."""
        numbered = sc.SidecarEvent()
        numbered.CopyFrom(event)
        numbered.offset = self.last_offset() + 1
        self._events.append(numbered)
        return numbered

    def events_after(self, offset: int) -> list[sc.SidecarEvent]:
        """See ``Store.events_after``."""
        return [event for event in self._events if event.offset > offset]

    def last_offset(self) -> int:
        """See ``Store.last_offset``."""
        return self._events[-1].offset if self._events else 0

    def append_raw(self, direction: FrameDirection, text: str) -> None:
        """See ``Store.append_raw``."""
        self._raw.append(RawRecord(self.last_offset(), direction, text))

    def raw_frames(self) -> list[RawRecord]:
        """See ``Store.raw_frames``."""
        return list(self._raw)

    def remember_effect(self, effect_id: str) -> bool:
        """See ``Store.remember_effect``."""
        if effect_id in self._effects:
            return False
        self._effects.add(effect_id)
        return True


class FileStore:
    """A ``Store`` in a directory: ``events.log``, ``raw.log`` and ``effects.log``.

    Opening it reads what is there and cuts a torn tail off each file, so a crash costs at most
    the record being written. Every append is flushed and synced before it returns, so an event
    the daemon has read is an event a restart still has.
    """

    def __init__(self, directory: Path) -> None:
        """Open the store in *directory*, creating it, and repair a torn tail."""
        directory.mkdir(parents=True, exist_ok=True)
        self._events_path = directory / "events.log"
        self._raw_path = directory / "raw.log"
        self._effects_path = directory / "effects.log"
        events, size = decode_events(self._read(self._events_path))
        self._cut(self._events_path, size)
        raw_lines, size = decode_lines(self._read(self._raw_path))
        self._cut(self._raw_path, size)
        effect_lines, size = decode_lines(self._read(self._effects_path))
        self._cut(self._effects_path, size)
        self._memory = MemoryStore(
            events,
            [r for line in raw_lines if (r := _parse_raw(line)) is not None],
            [e for line in effect_lines if (e := _parse_effect(line)) is not None],
        )

    @staticmethod
    def _read(path: Path) -> bytes:
        return path.read_bytes() if path.exists() else b""

    @staticmethod
    def _cut(path: Path, size: int) -> None:
        if path.exists() and path.stat().st_size != size:
            os.truncate(path, size)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        with path.open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, event: sc.SidecarEvent) -> sc.SidecarEvent:
        """See ``Store.append``."""
        numbered = self._memory.append(event)
        self._write(self._events_path, encode_events([numbered]))
        return numbered

    def events_after(self, offset: int) -> list[sc.SidecarEvent]:
        """See ``Store.events_after``."""
        return self._memory.events_after(offset)

    def last_offset(self) -> int:
        """See ``Store.last_offset``."""
        return self._memory.last_offset()

    def append_raw(self, direction: FrameDirection, text: str) -> None:
        """See ``Store.append_raw``."""
        self._memory.append_raw(direction, text)
        self._write(self._raw_path, (_raw_line(self._memory.raw_frames()[-1]) + "\n").encode())

    def raw_frames(self) -> list[RawRecord]:
        """See ``Store.raw_frames``."""
        return self._memory.raw_frames()

    def remember_effect(self, effect_id: str) -> bool:
        """See ``Store.remember_effect``."""
        if not self._memory.remember_effect(effect_id):
            return False
        self._write(self._effects_path, (json.dumps(effect_id) + "\n").encode())
        return True
