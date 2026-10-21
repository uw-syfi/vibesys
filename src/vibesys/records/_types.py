"""Values crossing the pure projection boundary."""

from dataclasses import dataclass

from vs_proto.api import Record


@dataclass(frozen=True)
class MappingState:
    """Immutable replay cursor; span versions are encoded to prevent aliasing."""

    run_id: str
    epoch: int = 1
    vibesys_version: str = "unknown"
    sequence: int = 0
    origin: float | None = None
    spans: tuple[tuple[str, bytes], ...] = ()
    invocations: tuple[tuple[str, str], ...] = ()
    streams: tuple[tuple[str, int], ...] = ()
    ending: bytes | None = None
    pending: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class BlobWrite:
    """Complete immutable bytes, addressed by their SHA-256."""

    sha256: str
    content: bytes


@dataclass(frozen=True)
class StreamWrite:
    """Append bytes to a stream at an exact replayable offset."""

    id: str
    offset: int
    content: bytes


@dataclass(frozen=True)
class MappedEvent:
    """One atomic projection result; content writes precede its record appends."""

    state: MappingState
    records: tuple[Record, ...]
    blobs: tuple[BlobWrite, ...] = ()
    streams: tuple[StreamWrite, ...] = ()
