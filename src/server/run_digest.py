"""A run's digest, folded from its event journal in one pass without replaying the run.

A list row needs a few figures of a run (its ideas, its best ratio, how long it took, how it
ended), and the journal is megabytes of output chunks and tool calls around the few events
that carry them. ``digest_journal`` skips every line that cannot be one of those events by a
substring test and decodes only the rest, so its cost is the file's read, not its parse.

The fold reads the vocabulary the hypothesis search labels its spans with (``workstream`` and
``gate``, ``vibesys.search_spans``) and the run's own ``run_ending`` and terminal events; it
imports none of them, so a host command that lists runs stays light
(``tests/architecture/test_light_entrypoints.py``).

Public surface: ``digest_journal``.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Final

from pydantic import JsonValue, TypeAdapter, ValidationError

from server.host_api import RunClosing, RunDigest, RunEndingFact
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Iterable

_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
_LINE_MAX_BYTES: Final = 16 * 1024 * 1024
_WANTED: Final = (
    b"span_ended",
    b"run_started",
    b"run_ending",
    b"run_finished",
    b"run_failed",
    b"run_interrupted",
)
"""A line holding none of these is not an event the fold reads."""
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
        self.ideas: set[str] = set()
        self.best: tuple[float, float | None] | None = None
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
        if kind == "span_ended":
            self._span(_object(data.get("span")))
        elif kind == "run_started":
            self.minimize = _object(data.get("overview")).get("direction") == "min"
        elif kind == "run_ending":
            self._ending(data)
        elif isinstance(kind, str) and kind in _CLOSINGS:
            self.closing = _CLOSINGS[kind]

    def _span(self, span: dict[str, JsonValue]) -> None:
        label = _object(span.get("label"))
        attrs = _attrs(label)
        span_id = span.get("id")
        if label.get("kind") == "workstream" and isinstance(span_id, str):
            if attrs.get("probe") is not True:
                self.ideas.add(span_id)
        elif label.get("kind") == "gate" and label.get("verdict") == "passed":
            value = _number(attrs.get("metric_value"))
            if value is not None and self._beats(value):
                self.best = (value, _number(attrs.get("vs_baseline")))

    def _beats(self, value: float) -> bool:
        if self.best is None:
            return True
        return value < self.best[0] if self.minimize else value > self.best[0]

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
        return RunDigest(
            ideas=len(self.ideas),
            best_ratio=None if self.best is None else self.best[1],
            duration_s=duration,
            ending=self.ending,
            closing=self.closing,
        )


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
        if not first and not any(word in line for word in _WANTED):
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
