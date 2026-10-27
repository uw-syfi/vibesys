"""Decode intra-kernel timelines and summarize them per event and per block.

Two inputs carry the same model, a set of per-block tracks of named intervals:

* The ``uint64`` buffer FlashInfer's ``include/flashinfer/profiler.cuh`` macros write
  (``PROFILER_INIT``, ``PROFILER_EVENT_START``, ``PROFILER_EVENT_END``,
  ``PROFILER_EVENT_INSTANT``), saved raw, e.g. ``buffer.cpu().numpy().tofile(path)``.
  Word 0 holds ``num_blocks`` and ``num_groups`` as two little-endian ``int32``; each
  later word holds a 32-bit tag in its low half and ``%globaltimer_lo`` nanoseconds in its
  high half. Tag bits 0-1 are the event type (begin, end, instant), bits 2-11 the event
  index into the caller's name list, bits 24-31 the SM id. Each (block, group) writes
  every ``num_blocks * num_groups`` words starting at ``1 + block * num_groups + group``,
  so the word's position, not its tag, names the writer.
* A Chrome trace-event JSON file (what ``ui.perfetto.dev`` opens), with complete ``X``
  events or ``B``/``E`` pairs; each ``(pid, tid)`` is a track, named by ``thread_name``
  metadata when present.

Everything here is pure: callers pass bytes or parsed JSON, and invalid input raises
:class:`TraceError`.
"""

from __future__ import annotations

import json
import math
import statistics
import struct
import sys
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_EVENT_BEGIN, _EVENT_END, _EVENT_INSTANT = 0, 1, 2
_WORD_BYTES = 8
_MIN_BUFFER_BYTES = 2 * _WORD_BYTES  # the header word and one event word
_INSTANT_PHASES = frozenset({"i", "I", "n"})
_MAX_US = 1e15  # bounds Chrome ``ts`` and ``dur`` so sums stay finite
_MAX_EVENT_INDEX = 0x3FF
_MAX_SLOTS = 1 << 20
_TIMER_WRAP = 1 << 32
_HALF_WRAP = 1 << 31


class TraceError(ValueError):
    """The input is not a decodable intra-kernel timeline."""


@dataclass(frozen=True)
class Interval:
    """One closed event on one track, in nanoseconds from the trace's first event."""

    track: str
    name: str
    begin_ns: float
    end_ns: float

    @property
    def duration_ns(self) -> float:
        """Return the interval length."""
        return self.end_ns - self.begin_ns


@dataclass(frozen=True)
class Timeline:
    """Decoded intervals plus what the decoder could not pair."""

    intervals: tuple[Interval, ...]
    instants: int = 0
    unmatched: int = 0

    def select(self, track_filter: str) -> Timeline:
        """Keep tracks whose name contains ``track_filter`` (case-insensitive)."""
        if not track_filter:
            return self
        needle = track_filter.lower()
        kept = tuple(item for item in self.intervals if needle in item.track.lower())
        if not kept:
            message = f"no track name contains {track_filter!r}"
            raise TraceError(message)
        return Timeline(kept, self.instants, self.unmatched)


def _event_name(index: int, names: Sequence[str]) -> str:
    if not names:
        return f"event_{index}"
    if index >= len(names):
        message = f"event index {index} has no name; {len(names)} event names were given"
        raise TraceError(message)
    return names[index]


def _buffer_words(data: bytes) -> tuple[int, int, array[int]]:
    """Return ``num_blocks``, ``num_groups`` and the event words after the header."""
    if len(data) < _MIN_BUFFER_BYTES or len(data) % _WORD_BYTES:
        message = (
            f"a FlashInfer profiler buffer is a whole number of uint64 words with a header "
            f"and at least one event; got {len(data)} bytes"
        )
        raise TraceError(message)
    num_blocks, num_groups = struct.unpack_from("<ii", data)
    if num_blocks <= 0 or num_groups <= 0 or num_blocks * num_groups > _MAX_SLOTS:
        message = (
            f"header holds num_blocks={num_blocks}, num_groups={num_groups}; the buffer was "
            "not written by PROFILER_INIT, or was not saved as raw uint64 words"
        )
        raise TraceError(message)
    words = array("Q")
    words.frombytes(data[_WORD_BYTES:])
    if sys.byteorder == "big":
        words.byteswap()
    return num_blocks, num_groups, words


@dataclass
class _Pairing:
    """Begin/end pairing state of one buffer, one open-event stack per writer slot."""

    stride: int
    open_events: dict[int, list[tuple[int, int]]] = field(default_factory=lambda: defaultdict(list))
    closed: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    instants: int = 0
    unmatched: int = 0

    def add(self, position: int, tag: int, time: int) -> None:
        kind, index = tag & 0x3, (tag >> 2) & _MAX_EVENT_INDEX
        slot = position % self.stride
        if kind == _EVENT_INSTANT:
            self.instants += 1
        elif kind == _EVENT_BEGIN:
            self.open_events[slot].append((index, time))
        elif kind == _EVENT_END:
            self._close(slot, (tag >> 24) & 0xFF, index, time)
        else:
            message = f"word {position + 1} has event type {kind}, which the profiler never writes"
            raise TraceError(message)

    def _close(self, slot: int, sm: int, index: int, time: int) -> None:
        stack = self.open_events[slot]
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] == index:
                _, begin = stack.pop(depth)
                self.closed.append((slot, sm, index, begin, time))
                return
        self.unmatched += 1


def decode_buffer(data: bytes, event_names: Sequence[str] = ()) -> Timeline:
    """Decode a raw FlashInfer profiler buffer.

    Timestamps are the low 32 bits of the global nanosecond timer. They are unwrapped
    against the first recorded event, so a capture must span less than about 2.1 s.
    """
    num_blocks, num_groups, words = _buffer_words(data)
    pairing = _Pairing(num_blocks * num_groups)
    reference: int | None = None
    for position, word in enumerate(words):
        if word == 0:
            continue
        stamp = word >> 32
        if reference is None:
            reference = stamp
        time = (stamp - reference + _HALF_WRAP) % _TIMER_WRAP - _HALF_WRAP
        pairing.add(position, word & 0xFFFFFFFF, time)
    if not pairing.closed:
        message = (
            "the buffer holds no closed events; build the kernel with "
            "FLASHINFER_ENABLE_PROFILER and pass the buffer to the launch"
        )
        raise TraceError(message)
    origin = min(begin for *_, begin, _ in pairing.closed)
    intervals = tuple(
        Interval(
            f"blk{slot // num_groups}_g{slot % num_groups} sm{sm}",
            _event_name(index, event_names),
            float(begin - origin),
            float(end - origin),
        )
        for slot, sm, index, begin, end in pairing.closed
    )
    unmatched = pairing.unmatched + sum(len(stack) for stack in pairing.open_events.values())
    return Timeline(intervals, pairing.instants, unmatched)


def _chrome_events(document: object) -> list[dict[str, object]]:
    events = document.get("traceEvents") if isinstance(document, dict) else document
    if not isinstance(events, list):
        message = "a Chrome trace is a JSON list of events or an object with 'traceEvents'"
        raise TraceError(message)
    return [event for event in events if isinstance(event, dict)]


def _number(event: dict[str, object], key: str) -> float:
    value = event.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not abs(value) <= _MAX_US:
        message = f"event {event.get('name')!r} has no {key!r} within {_MAX_US:.0e} us"
        raise TraceError(message)
    return float(value)


def _track_key(event: dict[str, object]) -> tuple[str, str]:
    """Return a hashable ``(pid, tid)``; ids may be any JSON value."""
    return str(event.get("pid")), str(event.get("tid"))


def _thread_labels(events: list[dict[str, object]]) -> dict[tuple[str, str], str]:
    labels = {}
    for event in events:
        args = event.get("args")
        is_label = event.get("ph") == "M" and event.get("name") == "thread_name"
        if is_label and isinstance(args, dict) and "name" in args:
            labels[_track_key(event)] = str(args["name"])
    return labels


@dataclass
class _ChromeSpans:
    """Complete and paired events of one Chrome trace, keyed by ``(pid, tid)``."""

    spans: list[tuple[tuple[str, str], str, float, float]] = field(default_factory=list)
    open_events: dict[tuple[str, str], list[tuple[str, float]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    instants: int = 0
    unmatched: int = 0

    def add(self, event: dict[str, object]) -> None:
        phase, key = event.get("ph"), _track_key(event)
        name = str(event.get("name", ""))
        if phase == "X":
            begin = _number(event, "ts")
            self.spans.append((key, name, begin, begin + _number(event, "dur")))
        elif phase == "B":
            self.open_events[key].append((name, _number(event, "ts")))
        elif phase == "E":
            if not self.open_events[key]:
                self.unmatched += 1
                return
            opened, begin = self.open_events[key].pop()
            self.spans.append((key, name or opened, begin, _number(event, "ts")))
        elif phase in _INSTANT_PHASES:
            self.instants += 1


def decode_chrome_trace(text: str | bytes) -> Timeline:
    """Decode a Chrome trace-event JSON document; ``ts`` and ``dur`` are microseconds."""
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as error:
        message = f"not a JSON document: {error}"
        raise TraceError(message) from error
    events = _chrome_events(document)
    labels = _thread_labels(events)
    collected = _ChromeSpans()
    for event in events:
        collected.add(event)
    if not collected.spans:
        message = "the trace holds no complete ('X') or paired ('B'/'E') events"
        raise TraceError(message)
    origin = min(begin for *_, begin, _ in collected.spans)
    intervals = tuple(
        Interval(
            labels.get(key, f"{key[0]}:{key[1]}"),
            name,
            (begin - origin) * 1e3,
            (end - origin) * 1e3,
        )
        for key, name, begin, end in collected.spans
    )
    unmatched = collected.unmatched + sum(len(stack) for stack in collected.open_events.values())
    return Timeline(intervals, collected.instants, unmatched)


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def _union_ns(intervals: Iterable[Interval]) -> float:
    total, cursor = 0.0, -math.inf
    for item in sorted(intervals, key=lambda value: value.begin_ns):
        begin = max(item.begin_ns, cursor)
        if item.end_ns > begin:
            total += item.end_ns - begin
        cursor = max(cursor, item.end_ns)
    return total


def summarize(timeline: Timeline, top: int) -> dict[str, object]:
    """Per event name: count, tracks, total and distribution of durations."""
    span = max(item.end_ns for item in timeline.intervals) - min(
        item.begin_ns for item in timeline.intervals
    )
    by_name: dict[str, list[Interval]] = defaultdict(list)
    for item in timeline.intervals:
        by_name[item.name].append(item)
    rows = []
    for name, items in by_name.items():
        durations = [item.duration_ns for item in items]
        rows.append(
            {
                "event": name,
                "count": len(items),
                "tracks": len({item.track for item in items}),
                "total_us": round(sum(durations) / 1e3, 3),
                "mean_us": round(statistics.fmean(durations) / 1e3, 3),
                "p50_us": round(_percentile(durations, 0.5) / 1e3, 3),
                "p95_us": round(_percentile(durations, 0.95) / 1e3, 3),
                "max_us": round(max(durations) / 1e3, 3),
            }
        )
    rows.sort(key=lambda row: -float(row["total_us"]))
    return {
        "span_us": round(span / 1e3, 3),
        "tracks": len({item.track for item in timeline.intervals}),
        "intervals": len(timeline.intervals),
        "instants": timeline.instants,
        "unmatched": timeline.unmatched,
        "events": rows[:top],
        "events_truncated": len(rows) > top,
    }


def tracks(timeline: Timeline, top: int) -> dict[str, object]:
    """Per track busy and idle time, latest-finishing tracks first."""
    by_track: dict[str, list[Interval]] = defaultdict(list)
    for item in timeline.intervals:
        by_track[item.track].append(item)
    start = min(item.begin_ns for item in timeline.intervals)
    rows = []
    for track, items in by_track.items():
        first = min(item.begin_ns for item in items)
        last = max(item.end_ns for item in items)
        busy = _union_ns(items)
        rows.append(
            {
                "track": track,
                "intervals": len(items),
                "first_begin_us": round((first - start) / 1e3, 3),
                "last_end_us": round((last - start) / 1e3, 3),
                "busy_us": round(busy / 1e3, 3),
                "idle_us": round((last - first - busy) / 1e3, 3),
            }
        )
    rows.sort(key=lambda row: (-float(row["last_end_us"]), str(row["track"])))
    busy_values = [float(row["busy_us"]) for row in rows]
    return {
        "tracks": len(rows),
        "busy_us_mean": round(statistics.fmean(busy_values), 3),
        "busy_us_max": max(busy_values),
        "busy_us_min": min(busy_values),
        "latest_finishing": rows[:top],
        "truncated": len(rows) > top,
    }


def event_skew(timeline: Timeline, event: str, top: int) -> dict[str, object]:
    """Align the k-th occurrence of ``event`` on every track and measure its spread.

    For each occurrence: ``wall`` runs from the earliest begin to the latest end,
    ``begin_skew`` is the spread of begins, ``imbalance`` is the slowest track's duration
    minus the mean. The track that ends last is the one the next grid-wide dependency
    waits for, so the tally of last finishers shows whether one block is persistently
    critical or the cost moves around.
    """
    per_track: dict[str, list[Interval]] = defaultdict(list)
    for item in sorted(timeline.intervals, key=lambda value: value.begin_ns):
        if item.name == event:
            per_track[item.track].append(item)
    if not per_track:
        names = sorted({item.name for item in timeline.intervals})
        message = f"no event named {event!r}; events: {', '.join(names[:40])}"
        raise TraceError(message)
    occurrences = max(len(items) for items in per_track.values())
    rows, last_finishers = [], Counter()
    for k in range(occurrences):
        items = [(track, values[k]) for track, values in per_track.items() if k < len(values)]
        durations = [item.duration_ns for _, item in items]
        last_track, last = max(items, key=lambda pair: pair[1].end_ns)
        slowest_track, slowest = max(items, key=lambda pair: pair[1].duration_ns)
        last_finishers[last_track] += 1
        rows.append(
            {
                "occurrence": k,
                "tracks": len(items),
                "wall_us": (last.end_ns - min(item.begin_ns for _, item in items)) / 1e3,
                "begin_skew_us": (
                    max(item.begin_ns for _, item in items)
                    - min(item.begin_ns for _, item in items)
                )
                / 1e3,
                "slowest_us": slowest.duration_ns / 1e3,
                "mean_us": statistics.fmean(durations) / 1e3,
                "imbalance_us": (slowest.duration_ns - statistics.fmean(durations)) / 1e3,
                "slowest_track": slowest_track,
                "last_track": last_track,
            }
        )
    totals = {
        key: round(sum(float(row[key]) for row in rows), 3)
        for key in ("wall_us", "begin_skew_us", "slowest_us", "mean_us", "imbalance_us")
    }
    worst = sorted(rows, key=lambda row: -float(row["wall_us"]))[:top]
    for row in worst:
        for key in totals:
            row[key] = round(float(row[key]), 3)
    return {
        "event": event,
        "occurrences": occurrences,
        "tracks": len(per_track),
        "totals": totals,
        "last_finishers": [
            {"track": track, "times_last": count}
            for track, count in last_finishers.most_common(top)
        ],
        "worst_occurrences": worst,
    }


def window(
    timeline: Timeline, track: str, start_us: float, end_us: float, limit: int
) -> dict[str, object]:
    """List one track's intervals that overlap ``[start_us, end_us]``."""
    start_ns, end_ns = start_us * 1e3, end_us * 1e3
    items = sorted(
        (
            item
            for item in timeline.intervals
            if item.track == track and item.end_ns >= start_ns and item.begin_ns <= end_ns
        ),
        key=lambda value: value.begin_ns,
    )
    if not items and track not in {item.track for item in timeline.intervals}:
        message = f"no track named {track!r}; call tracks() for the names"
        raise TraceError(message)
    return {
        "track": track,
        "intervals": [
            {
                "event": item.name,
                "begin_us": round(item.begin_ns / 1e3, 3),
                "duration_us": round(item.duration_ns / 1e3, 3),
            }
            for item in items[:limit]
        ],
        "matching": len(items),
        "truncated": len(items) > limit,
    }


def to_chrome_trace(timeline: Timeline) -> dict[str, object]:
    """Render a timeline as Chrome trace-event JSON, one thread per track."""
    names = sorted({item.track for item in timeline.intervals})
    tids = {name: tid for tid, name in enumerate(names)}
    events: list[dict[str, object]] = [
        {"ph": "M", "pid": 0, "name": "process_name", "args": {"name": "blocks"}}
    ]
    events += [
        {"ph": "M", "pid": 0, "tid": tid, "name": "thread_name", "args": {"name": name}}
        for name, tid in tids.items()
    ]
    events += [
        {
            "ph": "X",
            "pid": 0,
            "tid": tids[item.track],
            "name": item.name,
            "ts": item.begin_ns / 1e3,
            "dur": item.duration_ns / 1e3,
        }
        for item in timeline.intervals
    ]
    return {"traceEvents": events, "displayTimeUnit": "ns"}
