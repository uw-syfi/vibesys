"""The spans of a run, kept encoded and indexed so that an event costs what it touches.

A run holds a span for every unit of work, so decoding and encoding all of them on every event
made a run cost the square of its length. The table is persistent: an event reads the spans it
names through a :class:`SpanDraft`, which decodes a span on first use, and the successor table
shares every span the event left alone. Mappers that look for spans they cannot name ask for a
:class:`SpanKey`, answered from an index kept in step with the spans, never by scanning them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from rpds import HashTrieMap, HashTrieSet

import vs_proto.api as p

if TYPE_CHECKING:
    from collections.abc import Iterable


class Facet(StrEnum):
    """What a mapper asks of spans it does not name."""

    KIND = "kind"
    OPEN = "open"
    ROUND = "round"
    REVISION = "revision"
    ADOPTION = "adoption"
    CAPACITY_PROVIDER = "capacity_provider"


class SpanKey(NamedTuple):
    """One question the index answers: the spans whose ``facet`` is ``value``."""

    facet: Facet
    value: str | None = None

    @classmethod
    def of_kind(cls, kind: str) -> SpanKey:
        """Spans labelled ``kind``."""
        return cls(Facet.KIND, kind)

    @classmethod
    def unfinished(cls) -> SpanKey:
        """Spans still running, waiting or pending."""
        return cls(Facet.OPEN)

    @classmethod
    def round_named(cls, label: str) -> SpanKey:
        """Workstreams a journal event labelled ``label`` (``round-3``) names."""
        return cls(Facet.ROUND, label)

    @classmethod
    def revision_of(cls, revision: str) -> SpanKey:
        """Workstreams that produced ``revision``."""
        return cls(Facet.REVISION, revision)

    @classmethod
    def adoption_of(cls, candidate: str | None) -> SpanKey:
        """Adoptions of ``candidate``; None is the baseline."""
        return cls(Facet.ADOPTION, candidate)

    @classmethod
    def capacity_on(cls, provider: str) -> SpanKey:
        """Running capacity waits on ``provider``."""
        return cls(Facet.CAPACITY_PROVIDER, provider)


_OPEN = frozenset({p.SpanStatus.RUNNING, p.SpanStatus.WAITING, p.SpanStatus.PENDING})


def keys_of(span: p.Span) -> frozenset[SpanKey]:
    """Every question ``span`` answers, as the index holds them."""
    keys = {SpanKey.unfinished()} if span.status in _OPEN else set()
    label = span.label
    if label is None:
        return frozenset(keys)
    keys.add(SpanKey.of_kind(label.kind))
    attrs = label.attrs
    if attrs is None:
        return frozenset(keys)
    if label.kind == "workstream":
        workstream = p.WorkstreamAttrs.from_binary(attrs.value)
        keys.add(SpanKey.revision_of(workstream.revision))
        keys.update(
            SpanKey.round_named(f"round-{getattr(workstream, name)}")
            for name in ("round", "sequence")
            if workstream.has_field(name)
        )
    elif label.kind == "adoption":
        adoption = p.AdoptionAttrs.from_binary(attrs.value)
        keys.add(
            SpanKey.adoption_of(adoption.candidate if adoption.has_field("candidate") else None)
        )
    elif label.kind == "capacity_wait" and span.status == p.SpanStatus.RUNNING:
        keys.add(SpanKey.capacity_on(p.CapacityWaitAttrs.from_binary(attrs.value).provider))
    return frozenset(keys)


@dataclass(frozen=True)
class _Entry:
    ordinal: int
    wire: bytes
    keys: frozenset[SpanKey]


@dataclass(frozen=True)
class SpanTable:
    """Every span of a run encoded, in first-seen order, with the index that finds them by key."""

    entries: HashTrieMap[str, _Entry] = field(default_factory=HashTrieMap)
    index: HashTrieMap[SpanKey, HashTrieSet[str]] = field(default_factory=HashTrieMap)

    def __len__(self) -> int:
        return len(self.entries)

    def encoded(self) -> tuple[tuple[str, bytes], ...]:
        """Every span encoded, in first-seen order; a diagnostic view, linear in the run."""
        ordered = sorted(self.entries.items(), key=lambda item: item[1].ordinal)
        return tuple((identity, entry.wire) for identity, entry in ordered)

    def put(self, identity: str, wire: bytes, keys: frozenset[SpanKey]) -> SpanTable:
        """The table with ``identity`` holding ``wire``; a new span is the last seen."""
        previous = self.entries.get(identity)
        if previous is not None and previous.wire == wire and previous.keys == keys:
            return self
        ordinal = len(self.entries) if previous is None else previous.ordinal
        index = self.index
        held = frozenset() if previous is None else previous.keys
        for dropped in held - keys:
            group = index[dropped].remove(identity)
            index = index.insert(dropped, group) if group else index.remove(dropped)
        for added in keys - held:
            index = index.insert(added, index.get(added, HashTrieSet()).insert(identity))
        return SpanTable(self.entries.insert(identity, _Entry(ordinal, wire, keys)), index)


def build_span_table(spans: Iterable[tuple[str, p.Span]]) -> SpanTable:
    """The table of ``spans`` in the order they were first seen."""
    table = SpanTable()
    for identity, span in spans:
        table = table.put(identity, span.to_binary(), keys_of(span))
    return table


class SpanDraft:
    """One event's view of the spans: a span is decoded when first read, encoded when committed.

    A span the event read or wrote is encoded into the successor table, so a change to a span
    handed out here is never lost; every other span is shared with the table unchanged.
    """

    def __init__(self, table: SpanTable) -> None:
        """Start from ``table``; nothing is decoded yet."""
        self._table = table
        self._touched: dict[str, p.Span] = {}

    def get(self, identity: str) -> p.Span | None:
        """The span named ``identity``, or None."""
        span = self._touched.get(identity)
        if span is None:
            entry = self._table.entries.get(identity)
            if entry is None:
                return None
            span = self._touched[identity] = p.Span.from_binary(entry.wire)
        return span

    def __contains__(self, identity: str) -> bool:
        return identity in self._touched or identity in self._table.entries

    def __setitem__(self, identity: str, span: p.Span) -> None:
        self._touched[identity] = span

    def matching(self, key: SpanKey) -> list[p.Span]:
        """The spans ``key`` names as this event now sees them, in first-seen order."""
        entries = self._table.entries
        stored = self._table.index.get(key)
        names = [] if stored is None else [name for name in stored if name not in self._touched]
        names += [name for name, span in self._touched.items() if key in keys_of(span)]
        new = [name for name in self._touched if name not in entries]
        position = {name: len(entries) + order for order, name in enumerate(new)}
        names.sort(key=lambda name: position[name] if name in position else entries[name].ordinal)
        return [span for name in names if (span := self.get(name)) is not None]

    def has(self, key: SpanKey) -> bool:
        """Whether any span answers ``key``, without reading the spans that do."""
        stored = self._table.index.get(key)
        if stored is not None and any(name not in self._touched for name in stored):
            return True
        return any(key in keys_of(span) for span in self._touched.values())

    def commit(self) -> SpanTable:
        """The table after the event: the spans it read or wrote encoded again, the rest shared."""
        table = self._table
        for identity, span in self._touched.items():
            table = table.put(identity, span.to_binary(), keys_of(span))
        return table
