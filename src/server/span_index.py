"""Where each event of a run's journal sits, by sequence and by span: the journal's span index.

The journal (``run-events.jsonl``) is the truth; this index is a disposable read model of it,
kept beside it as ``run-events.jsonl.spans``. It answers two questions without reading the
journal from its start: the newest events before a sequence (a tail page), and the events of
one span, with the spans beneath it (one execution's transcript, tool calls and output).

An event belongs to the span it names in ``span_id``; an event that names none belongs to the
span whose ``invocation`` is its ``execution_id`` (an event recorded before its span was). The
spans' parents and invocations come from the span events themselves, so the index folds the
same facts a replay does and needs no other source.

The journal's indexed part is the longest run of lines that are JSON objects with integer
sequences, each at most ``HELPER_LINE_MAX_BYTES`` long. The first line that is not (an
unfinished tail, damage) ends it, as it ends ``runs.read_events``. An entry's ``sequence`` is
the cursor a reader sees, as the event store repairs it: a journal a resumed run appended to
under an older store restarts its sequences, and an event whose sequence does not pass its
predecessor's is read as the one after it. The events a page returns carry that sequence.

The sidecar is an append-only log: a header line, then one line per event,
``[offset, length, crc, sequence, span, execution, link, kind]``, where ``offset`` and ``length``
are the event's bytes in the journal without its newline, ``crc`` is their CRC-32,
``sequence`` is its repaired cursor, ``link`` is ``[span, parent, invocation]`` for a span event
and ``null`` otherwise, and ``kind`` is the event's ``type`` (``null`` when it names none), which
is how a read selects events by kind without reading the journal. A sidecar of another version
(the version is in its header) is not trusted: the reader scans the journal, and the next run
writer to open it rewrites it. The
run's writer (``SpanIndexLog``) appends a line right after it appends the event, so the
sidecar is never ahead of the journal except by an unflushed journal. A reader trusts the
longest valid prefix of the sidecar whose first and last entries still match the journal
bytes they name. A small source stamp records the journal's file identity and stat snapshot
after the writer's last sidecar update: a same-size rewrite or replaced file invalidates the
sidecar, while an append-only extension needs only a suffix scan. A missing or damaged stamp
causes a full scan.

Public surface: ``SpanLink``, ``Entry``, ``entry_from_line``, ``JournalIndex``,
``load_journal_index``, ``span_page``, ``kind_page``, ``read_documents``, ``StaleIndexError``,
``SpanIndexLog`` and ``sidecar_path``.
"""

from __future__ import annotations

import json
import sys
import zlib
from bisect import bisect_left
from dataclasses import dataclass, field, replace
from heapq import merge
from typing import TYPE_CHECKING, Final

from server.host_api import HELPER_LINE_MAX_BYTES

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from pydantic import JsonValue

SIDECAR_SUFFIX: Final = ".spans"
_IDENTITY_SUFFIX: Final = ".source"
_FORMAT: Final = "vibesys-span-index"
_VERSION: Final = 2
_HEADER: Final = (json.dumps([_FORMAT, _VERSION], separators=(",", ":")) + "\n").encode()
_ENTRY_FIELDS: Final = 8
_LINK_FIELDS: Final = 3
_SPAN_EVENT_KINDS: Final = frozenset({"span_started", "span_updated", "span_ended"})
_NEWLINE: Final = b"\n"
_SIDECAR_BATCH_BYTES: Final = 1 << 20
_SOURCE_STAMP_MAX_BYTES: Final = 256
_SOURCE_STAT_FIELDS: Final = 5
_TIMESTAMP_TICK_NS: Final = 2_000_000_000
"""The coarsest file timestamp granularity assumed (ext3 and FAT keep seconds, FAT two): an edit
made this soon after another can keep the same ``mtime_ns`` and ``ctime_ns``."""


def sidecar_path(journal: Path) -> Path:
    """The span index kept beside ``journal``."""
    return journal.with_name(journal.name + SIDECAR_SUFFIX)


def _identity_path(journal: Path) -> Path:
    """The small source-stat stamp written after the span sidecar."""
    return journal.with_name(journal.name + SIDECAR_SUFFIX + _IDENTITY_SUFFIX)


@dataclass(frozen=True, slots=True)
class _SourceStat:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _source_stat(path: Path) -> _SourceStat | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _SourceStat(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _same_source(path: Path, expected: _SourceStat | None) -> bool:
    """Accept an append-only extension, but reject replacement or same-size edits."""
    current = _source_stat(path)
    if expected is None or current is None:
        return False
    if (current.device, current.inode) != (expected.device, expected.inode):
        return False
    if current.size < expected.size:
        return False
    return current.size > expected.size or (
        current.mtime_ns == expected.mtime_ns and current.ctime_ns == expected.ctime_ns
    )


def _is_racy(journal: Path, source: _SourceStat | None) -> bool:
    """Whether a rewrite of ``journal`` after ``source`` was taken could keep ``source``'s stat.

    The file system's own clock is the stamp's modification time (the stamp is written after
    the journal event it describes). When the journal was modified within one timestamp tick
    of it, or after it, an in-place edit of the same size can leave ``mtime_ns`` and
    ``ctime_ns`` unchanged, so the stat alone cannot say the journal is unchanged. A missing
    stamp is racy.
    """
    if source is None:
        return True
    try:
        taken_ns = _identity_path(journal).stat().st_mtime_ns
    except OSError:
        return True
    return taken_ns - source.mtime_ns < _TIMESTAMP_TICK_NS


def _lines_intact(journal: Path, entries: Sequence[Entry]) -> bool:
    """Whether every entry's line is still in ``journal`` with the checksum it was indexed with."""
    try:
        with journal.open("rb") as stream:
            for entry in entries:
                stream.seek(entry.offset)
                line = stream.read(entry.length)
                if len(line) != entry.length or zlib.crc32(line) != entry.crc:
                    return False
    except OSError:
        return False
    return True


def _read_source_stamp(path: Path) -> _SourceStat | None:
    """The writer's bounded-size snapshot of the journal after the indexed prefix."""
    try:
        with path.open("rb") as stream:
            content = stream.read(257)
        if len(content) > _SOURCE_STAMP_MAX_BYTES:
            return None
        value = json.loads(content)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != _SOURCE_STAT_FIELDS
        or any(type(item) is not int for item in value)
    ):
        return None
    if min(value) < 0:
        return None
    return _SourceStat(*value)


def _write_source_stamp(journal: Path) -> None:
    """Record the journal identity after the corresponding sidecar write."""
    source = _source_stat(journal)
    if source is None:
        return
    stamp = _identity_path(journal)
    try:
        stamp.write_text(
            json.dumps(
                [source.device, source.inode, source.size, source.mtime_ns, source.ctime_ns],
                separators=(",", ":"),
            ),
            encoding="ascii",
        )
    except OSError:
        return


@dataclass(frozen=True, slots=True)
class SpanLink:
    """What a span event says about its span: its parent and the invocation it ran as."""

    span: str
    parent: str | None
    invocation: str | None


@dataclass(frozen=True, slots=True)
class Entry:
    """One journal event: its bytes (without the newline), identity and span facts."""

    offset: int
    length: int
    crc: int
    sequence: int
    """The event's cursor: the sequence it records, or the one after its predecessor's when it
    does not pass it."""
    span: str | None
    execution: str | None
    link: SpanLink | None
    kind: str | None
    """The event's ``type``, or ``None`` when it names none."""


def entry_from_line(offset: int, line: bytes) -> Entry | None:
    """The entry for ``line`` (a journal line without its newline) at ``offset``.

    ``None`` when the line is not a journal event: not a JSON object, or without an integer
    ``sequence``, or longer than a helper line. The one place that reads an event's span
    facts, so the writer and every reader agree on them. The entry's ``sequence`` is the
    one the line records: ``JournalIndex.add`` repairs it against the previous entry's.
    """
    if not line or len(line) > HELPER_LINE_MAX_BYTES:
        return None
    try:
        document = json.loads(line)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    sequence = document.get("sequence")
    if type(sequence) is not int or sequence < 0:
        return None
    execution = _text(document.get("execution_id")) or _text(document.get("invocation_id"))
    return Entry(
        offset=offset,
        length=len(line),
        crc=zlib.crc32(line),
        sequence=sequence,
        span=_text(document.get("span_id")),
        execution=execution,
        link=_span_link(document.get("data")),
        kind=_kind(document.get("type")),
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _kind(value: object) -> str | None:
    """The event type, shared between entries: a journal has a few dozen types and millions of events."""
    return sys.intern(value) if isinstance(value, str) else None


def _span_link(data: object) -> SpanLink | None:
    if not isinstance(data, dict) or data.get("kind") not in _SPAN_EVENT_KINDS:
        return None
    span = data.get("span")
    if not isinstance(span, dict):
        return None
    identity = _text(span.get("id"))
    if identity is None:
        return None
    return SpanLink(identity, _text(span.get("parent")), _text(span.get("invocation")))


@dataclass(slots=True)
class _Tables:
    """What a ``JournalIndex`` knows: its entries, and the positions and spans they name."""

    entries: list[Entry] = field(default_factory=list)
    by_span: dict[str, list[int]] = field(default_factory=dict)
    """The positions of the entries that name a span, by span."""
    by_execution: dict[str, list[int]] = field(default_factory=dict)
    """The positions of the entries that name an execution and no span, by execution."""
    parent: dict[str, str | None] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    invocation: dict[str, str] = field(default_factory=dict)

    def copy(self) -> _Tables:
        """The same tables, to extend without changing these."""
        return _Tables(
            self.entries[:],
            {span: found[:] for span, found in self.by_span.items()},
            {execution: found[:] for execution, found in self.by_execution.items()},
            dict(self.parent),
            {span: found[:] for span, found in self.children.items()},
            dict(self.invocation),
        )


class JournalIndex:
    """The entries of a journal's indexed part, and the questions they answer.

    Built by ``add`` in journal order; ``load_journal_index`` is how a reader gets one.
    """

    def __init__(self, tables: _Tables | None = None) -> None:
        """Start with no entries, or with the entries ``tables`` holds."""
        self._tables = _Tables() if tables is None else tables
        self._source: _SourceStat | None = None
        self._racy = True

    @property
    def entries(self) -> list[Entry]:
        """Every indexed entry, in journal order; not to be changed by the caller."""
        return self._tables.entries

    @property
    def last_sequence(self) -> int:
        """The newest indexed event's sequence, or 0 for no events."""
        return self.entries[-1].sequence if self.entries else 0

    @property
    def next_offset(self) -> int:
        """Where the next entry's line must start in the journal."""
        if not self.entries:
            return 0
        last = self.entries[-1]
        return last.offset + last.length + 1

    def add(self, entry: Entry) -> bool:
        """Append ``entry`` when it is the journal's next line; otherwise leave the index as it is."""
        if entry.offset != self.next_offset:
            return False
        entry = _repaired(entry, self.last_sequence)
        position = len(self.entries)
        self.entries.append(entry)
        if entry.span is not None:
            self._tables.by_span.setdefault(entry.span, []).append(position)
        elif entry.execution is not None:
            self._tables.by_execution.setdefault(entry.execution, []).append(position)
        link = entry.link
        if link is not None:
            if link.span not in self._tables.parent:
                self._tables.children.setdefault(link.parent or "", []).append(link.span)
            self._tables.parent.setdefault(link.span, link.parent)
            if link.invocation is not None:
                self._tables.invocation[link.span] = link.invocation
        return True

    def copy(self) -> JournalIndex:
        """An index of the same entries that ``add`` can extend without changing this one."""
        copied = JournalIndex(self._tables.copy())
        copied._source = self._source
        copied._racy = self._racy
        return copied

    def matches_source(self, journal: Path) -> bool:
        """Whether ``journal`` is unchanged or an append-only extension since this index.

        A stat that equals the snapshot's proves nothing while the snapshot is racy (see
        ``_is_racy``), so every indexed line is then checked against its checksum.
        """
        if not _same_source(journal, self._source):
            return False
        if self._racy and _source_stat(journal) == self._source:
            return _lines_intact(journal, self._tables.entries)
        return True

    def record_source(self, journal: Path) -> None:
        """Remember the journal's file identity and stat after its indexed prefix was read."""
        self._source = _source_stat(journal)
        self._racy = _is_racy(journal, self._source)

    def set_source_snapshot(self, journal: Path, source: _SourceStat | None) -> None:
        """Use the source snapshot validated with a sidecar."""
        self._source = source
        self._racy = _is_racy(journal, source)

    def tail(self, before: int | None, count: int, max_bytes: int) -> tuple[list[Entry], bool]:
        """The newest entries below sequence ``before`` (all when ``None``) one page carries.

        At most ``count`` and ``max_bytes`` of journal, but always one when there is one,
        oldest first, and whether older entries remain.
        """
        high = (
            len(self.entries)
            if before is None
            else bisect_left(self.entries, before, key=_sequence_of)
        )
        low = high
        size = 0
        while low > 0 and high - low < count:
            entry = self.entries[low - 1]
            if low < high and size + entry.length + 1 > max_bytes:
                break
            size += entry.length + 1
            low -= 1
        return self.entries[low:high], low > 0

    def spans_under(self, span: str) -> list[str]:
        """``span`` and every span beneath it, as the span events recorded their parents."""
        found = [span]
        seen = {span}
        for member in found:
            for child in self._tables.children.get(member, ()):
                if child not in seen:
                    seen.add(child)
                    found.append(child)
        return found

    def span_entries(self, span: str) -> list[Entry]:
        """The entries of ``span`` and of every span beneath it, in journal order."""
        members = self.spans_under(span)
        runs = [self._tables.by_span.get(member, ()) for member in members]
        invocations = {
            self._tables.invocation[member]
            for member in members
            if member in self._tables.invocation
        }
        runs.extend(
            self._tables.by_execution.get(invocation, ()) for invocation in sorted(invocations)
        )
        return [self.entries[position] for position in merge(*runs)]


def _repaired(entry: Entry, last_sequence: int) -> Entry:
    """``entry`` with the cursor the event store gives it after an event numbered ``last_sequence``."""
    if entry.sequence > last_sequence:
        return entry
    return replace(entry, sequence=last_sequence + 1)


def _sequence_of(entry: Entry) -> int:
    return entry.sequence


def span_page(
    index: JournalIndex, span: str, from_sequence: int, count: int, max_bytes: int
) -> tuple[list[Entry], int, bool]:
    """One page of ``span``'s entries from ``from_sequence``, with the span's entry count and whether the page ends it.

    A page holds at most ``count`` entries and ``max_bytes`` of journal, but always one when
    there is one, so every read makes progress.
    """
    entries = index.span_entries(span)
    first = bisect_left(entries, from_sequence, key=_sequence_of)
    size = 0
    stop = first
    while stop < len(entries) and stop - first < count:
        length = entries[stop].length + 1
        if stop > first and size + length > max_bytes:
            break
        size += length
        stop += 1
    return entries[first:stop], len(entries), stop == len(entries)


def kind_page(
    index: JournalIndex, kinds: Collection[str], from_sequence: int, count: int, max_bytes: int
) -> tuple[list[Entry], int, int, bool, int]:
    """One page of the entries whose event type is in ``kinds``, from ``from_sequence``.

    Returns the page's entries, how many entries of those kinds the journal holds and their bytes
    (every page, so a reader can show progress), whether the page reaches the journal's end, and
    the ``from_sequence`` of the next page.

    A page holds at most ``count`` entries and ``max_bytes`` of journal, but always one when
    there is one, so every read makes progress. A page that fills stops after its last entry;
    one that does not has scanned to the journal's end, so its next page starts after the last
    entry the index holds (a stretch of other kinds is never read twice, nor stalls a reader).
    """
    selected = [entry for entry in index.entries if entry.kind in kinds]
    first = bisect_left(selected, from_sequence, key=_sequence_of)
    size = 0
    stop = first
    while stop < len(selected) and stop - first < count:
        length = selected[stop].length + 1
        if stop > first and size + length > max_bytes:
            break
        size += length
        stop += 1
    page = selected[first:stop]
    complete = stop == len(selected)
    next_sequence = (
        max(from_sequence, index.last_sequence + 1) if complete else page[-1].sequence + 1
    )
    return page, len(selected), sum(entry.length + 1 for entry in selected), complete, next_sequence


class StaleIndexError(Exception):
    """The journal no longer holds the bytes an index entry names."""


def read_documents(journal: Path, entries: Sequence[Entry]) -> list[dict[str, JsonValue]]:
    """The events ``entries`` name, read from ``journal``.

    Raises ``StaleIndexError`` when any line differs from its entry: the journal was rewritten
    since the index was read, and the caller reads the index afresh.
    """
    documents: list[dict[str, JsonValue]] = []
    if not entries:
        return documents
    try:
        with journal.open("rb") as stream:
            for entry in entries:
                stream.seek(entry.offset)
                line = stream.read(entry.length)
                if len(line) != entry.length or zlib.crc32(line) != entry.crc:
                    raise StaleIndexError(journal)
                document = json.loads(line)
                if not isinstance(document, dict):
                    raise StaleIndexError(journal)
                document["sequence"] = entry.sequence
                documents.append(document)
    except (OSError, ValueError) as exc:
        raise StaleIndexError(journal) from exc
    return documents


def load_journal_index(journal: Path, previous: JournalIndex | None = None) -> JournalIndex:
    """The index of ``journal``'s indexed part, from its sidecar and a scan of what the sidecar lacks.

    ``previous`` is an index of the same journal read earlier; it is reused, and extended
    when the journal has grown, as long as its ends still match the journal. An absent
    journal has an empty index. Read-only: the sidecar is never written here.
    """
    if previous is not None and previous.entries and previous.matches_source(journal):
        if not _matches_journal(journal, previous):
            index = _checked_sidecar(journal)[0]
        elif not _grown(journal, previous):
            return previous
        else:
            index = previous.copy()
    elif previous is not None and not previous.entries and previous.matches_source(journal):
        if not _grown(journal, previous):
            return previous
        index = previous.copy()
    else:
        index = _checked_sidecar(journal)[0]
    _extend_from_journal(journal, index)
    index.record_source(journal)
    return index


def _grown(journal: Path, index: JournalIndex) -> bool:
    """Whether ``journal`` holds bytes past the index's last line other than its newline."""
    try:
        return journal.stat().st_size > index.next_offset
    except OSError:
        return False


def _matches_journal(journal: Path, index: JournalIndex) -> bool:
    """Whether the first and last entries still name the journal bytes they were read from."""
    if not index.entries:
        return True
    try:
        read_documents(journal, (index.entries[0], index.entries[-1]))
    except StaleIndexError:
        return False
    return True


def _checked_sidecar(journal: Path) -> tuple[JournalIndex, int]:
    """The sidecar's valid prefix as an index, and the bytes it takes; empty when it is not ``journal``'s."""
    entries, valid = _read_sidecar(sidecar_path(journal))
    index = JournalIndex()
    for entry in entries:
        index.add(entry)
    source = _read_source_stamp(_identity_path(journal))
    if not _same_source(journal, source) or not _matches_journal(journal, index):
        return JournalIndex(), 0
    index.set_source_snapshot(journal, source)
    if index.entries and not index.matches_source(journal):
        return JournalIndex(), 0
    return index, valid


def _read_sidecar(path: Path) -> tuple[list[Entry], int]:
    """The sidecar's valid entries and the bytes they take, header included.

    Stops at the first line that is torn, malformed or out of order, and takes nothing from
    a file whose header is not this format. Read in batches of lines, never whole.
    """
    try:
        stream = path.open("rb")
    except OSError:
        return [], 0
    entries: list[Entry] = []
    with stream:
        header = stream.readline()
        if header != _HEADER:
            return [], 0
        valid = len(header)
        next_offset = 0
        last_sequence = 0
        while batch := stream.readlines(_SIDECAR_BATCH_BYTES):
            whole = [line for line in batch if line.endswith(_NEWLINE)]
            rows = _json_rows([line[:-1] for line in whole])
            for line, fields in zip(whole, rows, strict=False):
                entry = _decode_entry(fields)
                if entry is None or entry.offset != next_offset or entry.sequence <= last_sequence:
                    return entries, valid
                entries.append(entry)
                valid += len(line)
                next_offset = entry.offset + entry.length + 1
                last_sequence = entry.sequence
            if len(rows) < len(batch):
                break
    return entries, valid


def _json_rows(lines: list[bytes]) -> list[object]:
    """The JSON values of ``lines`` up to the first that is not JSON.

    One decode of all of them when they all are, which is how a sidecar is read at its
    size; a line at a time to find where a damaged one is.
    """
    try:
        rows: list[object] = json.loads(b"[" + b",".join(lines) + b"]")
    except ValueError:
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                break
    return rows


def _encode_entry(entry: Entry) -> bytes:
    link = (
        None if entry.link is None else [entry.link.span, entry.link.parent, entry.link.invocation]
    )
    fields = [
        entry.offset,
        entry.length,
        entry.crc,
        entry.sequence,
        entry.span,
        entry.execution,
        link,
        entry.kind,
    ]
    return json.dumps(fields, separators=(",", ":")).encode() + _NEWLINE


def _decode_entry(fields: object) -> Entry | None:
    if not isinstance(fields, list) or len(fields) != _ENTRY_FIELDS:
        return None
    offset, length, crc, sequence, span, execution, link, kind = fields
    if (
        type(offset) is not int
        or type(length) is not int
        or type(crc) is not int
        or type(sequence) is not int
        or min(offset, length, crc, sequence) < 0
        or length == 0
        or not (span is None or isinstance(span, str))
        or not (execution is None or isinstance(execution, str))
        or not (kind is None or isinstance(kind, str))
    ):
        return None
    kind = _kind(kind)
    if link is None:
        return Entry(offset, length, crc, sequence, span, execution, None, kind)
    decoded = _decode_link(link)
    if decoded is None:
        return None
    return Entry(offset, length, crc, sequence, span, execution, decoded, kind)


def _decode_link(link: object) -> SpanLink | None:
    if not isinstance(link, list) or len(link) != _LINK_FIELDS or not isinstance(link[0], str):
        return None
    if not all(item is None or isinstance(item, str) for item in link[1:]):
        return None
    return SpanLink(link[0], link[1], link[2])


def _extend_from_journal(journal: Path, index: JournalIndex) -> None:
    """Add the entries of the journal lines after the index's last, until the indexed part ends."""
    try:
        stream = journal.open("rb")
    except OSError:
        return
    with stream:
        offset = index.next_offset
        if index.entries:
            stream.seek(offset - 1)
            if stream.read(1) != _NEWLINE:
                return
        for raw in stream:
            entry = entry_from_line(offset, raw.removesuffix(_NEWLINE))
            if entry is None or not index.add(entry) or not raw.endswith(_NEWLINE):
                return
            offset += len(raw)


class SpanIndexLog:
    """The run's writer of the sidecar: it appends one line per journal event, as it is appended.

    Opening it brings the sidecar level with the journal (trimming whatever is not valid
    and adding the events it lacks); ``append`` then keeps it level. A sidecar line that
    cannot be written is skipped, which leaves a gap the next reader stops at and scans
    past: never a wrong answer, only a slower one.
    """

    def __init__(self, journal: Path) -> None:
        """Open ``journal``'s sidecar, repairing it against the journal as it is now."""
        self._journal = journal
        self._path = sidecar_path(journal)
        index, valid = _checked_sidecar(journal)
        kept = len(index.entries)
        _extend_from_journal(journal, index)
        self._next_offset = index.next_offset
        self._last_sequence = index.last_sequence
        try:
            self._repair(valid, index.entries, kept)
        except OSError:
            return
        _write_source_stamp(journal)

    def _repair(self, valid: int, entries: list[Entry], kept: int) -> None:
        """Make the sidecar hold exactly ``entries``: its first ``kept`` lines are already in it."""
        if not self._path.exists() and not entries:
            return
        if valid == 0:
            body = b"".join(_encode_entry(entry) for entry in entries)
            self._path.write_bytes(_HEADER + body if entries else b"")
            return
        with self._path.open("r+b") as stream:
            stream.truncate(valid)
            stream.seek(valid)
            stream.write(b"".join(_encode_entry(entry) for entry in entries[kept:]))

    def append(self, offset: int, line: bytes) -> None:
        """Record the event ``line`` just appended to the journal at ``offset``."""
        recorded = entry_from_line(offset, line)
        if recorded is None or recorded.offset != self._next_offset:
            return
        entry = _repaired(recorded, self._last_sequence)
        self._next_offset = entry.offset + entry.length + 1
        self._last_sequence = entry.sequence
        try:
            with self._path.open("ab") as stream:
                if stream.tell() == 0:
                    stream.write(_HEADER)
                stream.write(_encode_entry(entry))
        except OSError:
            return
        _write_source_stamp(self._journal)
