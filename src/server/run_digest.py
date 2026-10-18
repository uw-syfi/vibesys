"""A run's digest, folded from its event journal in one pass without replaying the run.

A list row needs a few figures of a run (its ideas, its adopted ratio, how long it took, how it
ended), and the journal is megabytes of output chunks and tool calls around the few events
that carry them. ``digest_journal`` skips every line that cannot be one of those events by a
substring test and decodes only the rest, so its cost is the file's read, not its parse.

The figures are the run page's, by one definition each. The page counts an idea once per
hypothesis, however many attempts it took (a workstream with no hypothesis is an idea of its own),
and counts no probe; its headline is what the run's ``adoption`` span names, never the best raw
reading of a gate, which may belong to an idea that later failed. The fold reads the vocabulary the
hypothesis search labels its spans with (``workstream`` and ``adoption``,
``vibesys.search_spans``) and the run's own ``run_ending`` and terminal events; it imports none of
them, so a host command that lists runs stays light (``tests/architecture/test_light_entrypoints.py``).
The page's fold is ``CandidateFold`` in ``@vibesys/run-state``;
``tests/conformance/fixtures/run-digest`` holds the journals both must fold to the same figures.

A journal with finished rounds and no span event predates the run trace
(``server.host_run_summary.trace_format``): its ideas are not recorded, so the digest says so
(``ideas`` is ``None``) instead of counting none. A round and its workstream span end together, so a
journal with a finished round and no ended span has no spans at all.

Public surface: ``digest_journal``.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue, TypeAdapter, ValidationError

from server.host_api import RunClosing, RunDigest, RunEndingFact
from server.host_run_summary import TraceFormat, trace_format
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Iterable

_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
_LINE_MAX_BYTES: Final = 16 * 1024 * 1024
_WANTED: Final = (
    b"span_ended",
    b"round_finished",
    b"run_started",
    b"run_ending",
    b"run_finished",
    b"run_failed",
    b"run_interrupted",
)
"""A line holding none of these, and no open span of a kind the fold reads, is not an event it reads."""
_OPEN_SPANS: Final = (b"span_started", b"span_updated")
_OPEN_KINDS: Final = (b"workstream", b"adoption")
_SPAN_EVENTS: Final = frozenset({"span_started", "span_updated", "span_ended"})
_CLOSINGS: Final = {
    "run_finished": RunClosing.FINISHED,
    "run_failed": RunClosing.FAILED,
    "run_interrupted": RunClosing.INTERRUPTED,
}


def _object(value: JsonValue | None) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _number(value: JsonValue | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _text(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _attrs(label: dict[str, JsonValue]) -> dict[str, JsonValue]:
    text = label.get("attrs_json")
    if not isinstance(text, str) or not text:
        return {}
    decoded = decode_json(text, _OBJECT, source="span attributes", max_bytes=_LINE_MAX_BYTES)
    return {} if isinstance(decoded, Unreadable) else decoded


def _seconds(event: dict[str, JsonValue]) -> float | None:
    stamp = event.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


class _Fold:
    """The figures read so far."""

    def __init__(self) -> None:
        self.first: float | None = None
        self.last: float | None = None
        self.minimize = False
        self.workstreams: dict[str, tuple[str, str | None] | None] = {}
        """Each workstream span's idea and revision, by span id, as its latest event left it; ``None`` for a probe."""
        self.adoptions: dict[str, tuple[str | None, dict[str, JsonValue]]] = {}
        """Each adoption span's verdict and attributes, by span id, in the order they started."""
        self.spans = 0
        self.rounds = 0
        self.ending: RunEndingFact | None = None
        self.closing: RunClosing | None = None

    def stamp(self, event: dict[str, JsonValue]) -> None:
        """Count ``event``'s time in the run's span, whatever it says."""
        at = _seconds(event)
        if at is not None:
            self.first = at if self.first is None else min(self.first, at)
            self.last = at if self.last is None else max(self.last, at)

    def see(self, event: dict[str, JsonValue]) -> None:
        """Read the figures ``event`` carries."""
        self.stamp(event)
        kind = event.get("type")
        data = _object(event.get("data"))
        if kind == "round_finished":
            self.rounds += 1
        elif kind in _SPAN_EVENTS:
            self.spans += 1
            self._span(_object(data.get("span")))
        elif kind == "run_started":
            objective = _object(_object(data.get("overview")).get("objective"))
            self.minimize = objective.get("direction") == "min"
        elif kind == "run_ending":
            self._ending(data)
        elif isinstance(kind, str) and kind in _CLOSINGS:
            self.closing = _CLOSINGS[kind]

    def _span(self, span: dict[str, JsonValue]) -> None:
        label = _object(span.get("label"))
        attrs = _attrs(label)
        span_id = span.get("id")
        if not isinstance(span_id, str):
            return
        if label.get("kind") == "workstream":
            hypothesis = _text(attrs.get("hypothesis_id"))
            idea = f"span:{span_id}" if hypothesis is None else f"hypothesis:{hypothesis}"
            probe = attrs.get("probe") is True
            self.workstreams[span_id] = None if probe else (idea, _text(attrs.get("revision")))
        elif label.get("kind") == "adoption":
            verdict = label.get("verdict")
            self.adoptions[span_id] = (verdict if isinstance(verdict, str) else None, attrs)

    def _adopted_ratio(self) -> float | None:
        """What the run page headlines: the ratio of the result the latest adoption span names.

        Nothing before the search begins an adoption, once the adoption says ``not_adopted`` or
        keeps the input, and when no counted idea made the adopted revision. ``min`` objectives read
        as an improvement (the input's value over the result's), as the page words them.
        """
        if not self.adoptions:
            return None
        verdict, attrs = next(reversed(self.adoptions.values()))
        revision = _text(attrs.get("candidate"))
        ratio = _number(attrs.get("vs_baseline"))
        if verdict == "not_adopted" or revision is None or ratio is None:
            return None
        if not any(made is not None and made[1] == revision for made in self.workstreams.values()):
            return None
        return 1 / ratio if self.minimize and ratio != 0 else ratio

    def _ending(self, data: dict[str, JsonValue]) -> None:
        try:
            self.ending = RunEndingFact.model_validate(
                {
                    "outcome": data.get("reason"),
                    "summary": data.get("summary", ""),
                    "resumable": data.get("resumable"),
                    "rounds_finished": data.get("rounds_finished"),
                }
            )
        except ValidationError:
            self.ending = None

    def digest(self) -> RunDigest:
        duration = None if self.first is None or self.last is None else self.last - self.first
        recorded = trace_format(spans=self.spans, rounds=self.rounds) is TraceFormat.RECORDED
        return RunDigest(
            ideas=len({made[0] for made in self.workstreams.values() if made})
            if recorded
            else None,
            adopted_ratio=self._adopted_ratio(),
            duration_s=duration,
            ending=self.ending,
            closing=self.closing,
        )


def _is_read(line: bytes) -> bool:
    if any(word in line for word in _WANTED):
        return True
    return any(word in line for word in _OPEN_SPANS) and any(word in line for word in _OPEN_KINDS)


def digest_journal(lines: Iterable[bytes]) -> RunDigest:
    """The digest of the journal ``lines``, oldest first.

    A line that is not a JSON object (the tail a live writer has not finished, or damage) is
    skipped: the figures are those of the events before it and after it that decode. The
    first and the last line are always decoded, for the times the run began and was last heard.
    """
    fold = _Fold()
    first = True
    unread: deque[bytes] = deque(maxlen=2)
    for line in lines:
        if not line.strip():
            continue
        if not first and not _is_read(line):
            unread.append(line)
            continue
        first = False
        unread.clear()
        event = decode_json(line, _OBJECT, source="run event", max_bytes=_LINE_MAX_BYTES)
        if not isinstance(event, Unreadable):
            fold.see(event)
    for line in reversed(unread):  # the last line may be torn: then the one before it
        last = decode_json(line, _OBJECT, source="run event", max_bytes=_LINE_MAX_BYTES)
        if not isinstance(last, Unreadable):
            fold.stamp(last)
            break
    return fold.digest()
