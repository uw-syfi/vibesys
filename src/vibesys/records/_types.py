"""Values crossing the pure projection boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rpds import HashTrieSet

if TYPE_CHECKING:
    from vibesys.turn_activity import TurnActivity
    from vs_proto.api import Record


@dataclass(frozen=True)
class ActivityMark:
    """A running turn's activity, when it began, and the run clock its span last said one.

    ``held`` marks an activity its span does not say yet: it changed inside the write interval.
    """

    activity: TurnActivity
    since: float
    written_at: float | None = None
    held: bool = False


@dataclass(frozen=True)
class MappingState:
    """Immutable replay cursor; span versions are encoded to prevent aliasing."""

    run_id: str
    epoch: int = 1
    vibesys_version: str = "unknown"
    sequence: int = 0
    header_written: bool = False
    epoch_header: bytes | None = None
    origin: float | None = None
    clock: float = 0
    clock_offset: float = 0
    tree_hash: str = ""
    dirty: bool = False
    project_root: str = ""
    epoch_ended: bool = False
    native_intents: bool = False
    wall_span_clock: bool = False
    clock_epochs: tuple[tuple[float, float], ...] = ()
    spans: tuple[tuple[str, bytes], ...] = ()
    invocations: tuple[tuple[str, str], ...] = ()
    streams: tuple[tuple[str, int], ...] = ()
    waits: tuple[tuple[str, str], ...] = ()
    intents: tuple[tuple[str, bytes], ...] = ()
    primary_series: str | None = None
    objective_inferred: bool = False
    input_measurement: bytes | None = None
    input_inherited: bool = False
    metric_values: tuple[tuple[tuple[str, str | None], tuple[float, float]], ...] = ()
    # Journal sequences are immutable identities across all event kinds. Ordered input compacts
    # to one frontier; only deliveries ahead of a hole need retained identities. Thus memory is
    # bounded by the outstanding source-sequence gap, not by the history or delivery count.
    source_through: int = 0
    source_pending: HashTrieSet[int] = field(default_factory=HashTrieSet)
    ending: bytes | None = None
    pending: tuple[bytes, ...] = ()
    activities: tuple[tuple[str, ActivityMark], ...] = ()


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
