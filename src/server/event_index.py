"""Disposable on-disk index for the authoritative JSONL event journal."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import BinaryIO

_FORMAT = "vibesys-event-index"
_VERSION = 1
_FINGERPRINT_BYTES = 64 * 1024
_RECORD_FIELDS = 7
_SHA256_HEX_LENGTH = 64


class _Digest(Protocol):
    def update(self, content: bytes, /) -> None:
        """Add bytes to the digest."""

    def hexdigest(self) -> str:
        """Return the lowercase hexadecimal digest."""


@dataclass(frozen=True, slots=True)
class EventIndexRecord:
    """Primitive fields needed to reconstruct one in-memory event record."""

    offset: int
    length: int
    raw_sequence: int
    sequence: int
    event_type: str
    execution_id: str | None
    chat_thread_id: str | None


@dataclass(frozen=True, slots=True)
class LoadedEventIndex:
    """A fully validated sidecar and its safe source boundary."""

    records: list[EventIndexRecord]
    boundary: int


@dataclass(frozen=True, slots=True)
class SourceStat:
    """Source metadata that must remain stable across one streaming scan."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _SourceIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    head_sha256: str
    boundary_sha256: str


def event_index_path(source: Path) -> Path:
    """Return the disposable sidecar path adjacent to ``source``."""
    return source.with_name(f"{source.name}.idx")


def load_event_index(source: Path) -> LoadedEventIndex | None:
    """Load a sidecar only when it exactly matches the current source file."""
    path = event_index_path(source)
    try:
        with path.open("rb") as stream:
            header_line = stream.readline()
            decoded_header = _decode_header(header_line)
            if decoded_header is None:
                return None
            boundary, count, expected_source = decoded_header
            current_source = _source_identity(source, boundary)
            if current_source != expected_source:
                return None

            digest = hashlib.sha256(usedforsecurity=False)
            digest.update(header_line)
            records = _load_records(stream, count, boundary, digest)
            if records is None or not _valid_footer(stream, digest):
                return None
            if _source_identity(source, boundary) != current_source:
                return None
    except (OSError, UnicodeError, ValueError):
        return None
    return LoadedEventIndex(records=records, boundary=boundary)


def _decode_header(line: bytes) -> tuple[int, int, _SourceIdentity] | None:
    header = _decode_object(line)
    expected_keys = {"format", "version", "source", "boundary", "count"}
    if header is None or set(header) != expected_keys:
        return None
    if header["format"] != _FORMAT or header["version"] != _VERSION:
        return None
    boundary = _plain_nonnegative_int(header["boundary"])
    count = _plain_nonnegative_int(header["count"])
    source = _decode_source_identity(header["source"])
    if boundary is None or count is None or source is None:
        return None
    return boundary, count, source


def _load_records(
    stream: BinaryIO, count: int, boundary: int, digest: _Digest
) -> list[EventIndexRecord] | None:
    records: list[EventIndexRecord] = []
    expected_offset = 0
    last_sequence = 0
    for _ in range(count):
        line = stream.readline()
        digest.update(line)
        record = _decode_record(line)
        if record is None or record.offset != expected_offset or record.length <= 0:
            return None
        repaired = record.raw_sequence if record.raw_sequence > last_sequence else last_sequence + 1
        if record.sequence != repaired:
            return None
        records.append(record)
        expected_offset += record.length
        last_sequence = record.sequence
    return records if expected_offset == boundary else None


def _valid_footer(stream: BinaryIO, digest: _Digest) -> bool:
    footer = _decode_object(stream.readline())
    checksum = None if footer is None else footer.get("sha256")
    return bool(
        footer is not None
        and set(footer) == {"sha256"}
        and isinstance(checksum, str)
        and _is_sha256(checksum)
        and hmac.compare_digest(checksum, digest.hexdigest())
        and not stream.read(1)
    )


def write_event_index(
    source: Path,
    records: Iterable[EventIndexRecord],
    record_count: int,
    boundary: int,
    expected_source: SourceStat,
) -> bool:
    """Atomically publish an index, returning false when caching is unavailable."""
    identity = _source_identity(source, boundary)
    if identity is None or _identity_stat(identity) != expected_source:
        return False
    header = {
        "format": _FORMAT,
        "version": _VERSION,
        "source": asdict(identity),
        "boundary": boundary,
        "count": record_count,
    }
    path = event_index_path(source)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        temporary.chmod(0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            digest = hashlib.sha256(usedforsecurity=False)
            header_line = _encode(header)
            stream.write(header_line)
            digest.update(header_line)
            written_records = 0
            for record in records:
                written_records += 1
                line = _encode(
                    [
                        record.offset,
                        record.length,
                        record.raw_sequence,
                        record.sequence,
                        record.event_type,
                        record.execution_id,
                        record.chat_thread_id,
                    ]
                )
                stream.write(line)
                digest.update(line)
            stream.write(_encode({"sha256": digest.hexdigest()}))
            stream.flush()
            os.fsync(stream.fileno())
        if written_records != record_count:
            return False
        # A source change only makes this cache stale, but avoid publishing work
        # already known to be unusable.
        if _source_identity(source, boundary) != identity:
            return False
        os.replace(temporary, path)  # noqa: PTH105  # atomic publication
        _fsync_directory(path.parent)
        temporary = None
    except OSError:
        return False
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return True


def source_stat(source: Path) -> SourceStat | None:
    """Return the exact metadata used to reject changes during a source scan."""
    try:
        value = source.stat()
    except OSError:
        return None
    return SourceStat(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _source_identity(source: Path, boundary: int) -> _SourceIdentity | None:
    """Capture stable stat fields plus bounded source-content fingerprints."""
    try:
        with source.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if boundary < 0 or boundary > before.st_size:
                return None
            head = stream.read(min(_FINGERPRINT_BYTES, before.st_size))
            boundary_start = max(0, boundary - _FINGERPRINT_BYTES)
            stream.seek(boundary_start)
            boundary_bytes = stream.read(boundary - boundary_start)
            after = os.fstat(stream.fileno())
        path_stat = source.stat()
    except OSError:
        return None
    before_fields = _stat_fields(before)
    if before_fields != _stat_fields(after) or before_fields != _stat_fields(path_stat):
        return None
    return _SourceIdentity(
        device=before.st_dev,
        inode=before.st_ino,
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        ctime_ns=before.st_ctime_ns,
        head_sha256=_content_digest(head),
        boundary_sha256=_content_digest(boundary_bytes),
    )


def _stat_fields(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _identity_stat(identity: _SourceIdentity) -> SourceStat:
    return SourceStat(
        device=identity.device,
        inode=identity.inode,
        size=identity.size,
        mtime_ns=identity.mtime_ns,
        ctime_ns=identity.ctime_ns,
    )


def _content_digest(content: bytes) -> str:
    return hashlib.sha256(content, usedforsecurity=False).hexdigest()


def _encode(value: object) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _decode_object(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _decode_source_identity(value: object) -> _SourceIdentity | None:
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "size",
        "mtime_ns",
        "ctime_ns",
        "head_sha256",
        "boundary_sha256",
    }:
        return None
    integers = [value[name] for name in ("device", "inode", "size", "mtime_ns", "ctime_ns")]
    if any(_plain_nonnegative_int(item) is None for item in integers):
        return None
    head = value["head_sha256"]
    boundary = value["boundary_sha256"]
    if not _is_sha256(head) or not _is_sha256(boundary):
        return None
    return _SourceIdentity(
        device=integers[0],
        inode=integers[1],
        size=integers[2],
        mtime_ns=integers[3],
        ctime_ns=integers[4],
        head_sha256=head,
        boundary_sha256=boundary,
    )


def _decode_record(line: bytes) -> EventIndexRecord | None:
    try:
        value = json.loads(line)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, list) or len(value) != _RECORD_FIELDS:
        return None
    offset, length, raw_sequence, sequence, event_type, execution_id, chat_thread_id = value
    if any(
        _plain_nonnegative_int(item) is None for item in (offset, length, raw_sequence, sequence)
    ):
        return None
    if not isinstance(event_type, str):
        return None
    if not _is_optional_str(execution_id) or not _is_optional_str(chat_thread_id):
        return None
    return EventIndexRecord(
        offset=offset,
        length=length,
        raw_sequence=raw_sequence,
        sequence=sequence,
        event_type=event_type,
        execution_id=execution_id,
        chat_thread_id=chat_thread_id,
    )


def _plain_nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _is_optional_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LENGTH or value != value.lower():
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
