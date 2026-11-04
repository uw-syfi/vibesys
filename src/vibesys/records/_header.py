"""An epoch declaration held encoded, with the few facts events read of it kept beside it.

The declaration carries every descriptor the attrs need, about 95 KB, so decoding it on every
event cost more than the event. It is decoded only to amend it; what a mapper asks of it comes
from :class:`Declared`, computed once for each declaration.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rpds import HashTrieMap

import vs_proto.api as p


@dataclass(frozen=True)
class SeriesFacts:
    """One declared metric series, as a mapper compares it."""

    unit: str
    direction: p.Direction
    role: p.SeriesRole


@dataclass(frozen=True)
class Declared:
    """What events read of a declaration: its span kinds, series, objectives and whether it has a run."""

    span_kinds: frozenset[str]
    series: HashTrieMap[str, SeriesFacts]
    objectives: tuple[tuple[str, p.Direction], ...]
    has_run: bool

    @classmethod
    def of(cls, message: p.EpochStarted) -> Declared:
        """The facts of ``message``; a series named twice keeps its first row."""
        series: dict[str, SeriesFacts] = {}
        for row in message.series:
            series.setdefault(row.name, SeriesFacts(row.unit, row.direction, row.role))
        run = message.run
        return cls(
            span_kinds=frozenset(kind.kind for kind in message.span_kinds),
            series=HashTrieMap(series),
            objectives=()
            if run is None
            else tuple((o.series, o.direction) for o in run.objectives),
            has_run=run is not None,
        )


@dataclass(frozen=True)
class EpochHeader:
    """A declaration as its wire form; it is decoded to amend it, never to read it."""

    wire: bytes
    declared: Declared = field(compare=False, repr=False)

    @classmethod
    def of(cls, message: p.EpochStarted, wire: bytes) -> EpochHeader:
        """The header of ``message``, whose encoding is ``wire``."""
        return cls(wire, Declared.of(message))

    def decode(self) -> p.EpochStarted:
        """A copy of the declaration to amend; the header itself is never changed."""
        return p.EpochStarted.from_binary(self.wire)
