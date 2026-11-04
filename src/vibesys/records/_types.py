"""Values crossing the pure projection boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rpds import HashTrieMap, HashTrieSet

from vibesys.records._span_table import SpanTable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vibesys.records._header import EpochHeader
    from vs_proto.api import Record


def build_intents(pairs: Iterable[tuple[str, bytes]]) -> HashTrieMap[str, bytes]:
    """The state's intents: each control id with its encoded outcome."""
    return HashTrieMap(pairs)


@dataclass(frozen=True)
class MappingState:
    """Immutable replay cursor; every table is persistent, so an event pays only for what it changes.

    Spans are held encoded to prevent aliasing, and the epoch header is decoded only to amend it.
    """

    run_id: str
    epoch: int = 1
    vibesys_version: str = "unknown"
    sequence: int = 0
    header_written: bool = False
    epoch_header: EpochHeader | None = None
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
    span_table: SpanTable = field(default_factory=SpanTable)
    invocations: HashTrieMap[str, str] = field(default_factory=HashTrieMap)
    streams: HashTrieMap[str, int] = field(default_factory=HashTrieMap)
    waits: HashTrieMap[str, str] = field(default_factory=HashTrieMap)
    intents: HashTrieMap[str, bytes] = field(default_factory=HashTrieMap)
    primary_series: str | None = None
    objective_inferred: bool = False
    input_measurement: bytes | None = None
    input_inherited: bool = False
    metric_values: HashTrieMap[tuple[str, str | None], tuple[float, float]] = field(
        default_factory=HashTrieMap
    )
    # Journal sequences are immutable identities across all event kinds. Ordered input compacts
    # to one frontier; only deliveries ahead of a hole need retained identities. Thus memory is
    # bounded by the outstanding source-sequence gap, not by the history or delivery count.
    source_through: int = 0
    source_pending: HashTrieSet[int] = field(default_factory=HashTrieSet)
    ending: bytes | None = None
    pending: tuple[bytes, ...] = ()

    @property
    def spans(self) -> tuple[tuple[str, bytes], ...]:
        """Every span encoded, in first-seen order; a diagnostic view, linear in the run."""
        return self.span_table.encoded()


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
