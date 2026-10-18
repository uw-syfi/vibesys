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
While the run works it has opened no adoption, so the page headlines its best idea so far
(``best_so_far_ratio``): the latest idea that passed and beat the input, read from the gates under
each workstream. The page's fold is ``CandidateFold`` in ``@vibesys/run-state``;
``tests/conformance/fixtures/run-digest`` holds the journals both must fold to the same figures.

A journal with finished rounds and no span event predates the run trace
(``server.host_run_summary.trace_format``): its ideas are not recorded, so the digest says so
(``ideas`` is ``None``) instead of counting none. A round and its workstream span end together, so a
journal with a finished round and no ended span has no spans at all.

Public surface: ``digest_journal``.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, NamedTuple

from pydantic import JsonValue, TypeAdapter, ValidationError

from server.host_api import RunClosing, RunDigest, RunEndingFact
from server.host_run_summary import TraceFormat, trace_format
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Iterable

_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
_LINE_MAX_BYTES: Final = 16 * 1024 * 1024
_WANTED: Final = (
    b"span_started",
    b"span_ended",
    b"round_finished",
    b"run_started",
    b"run_ending",
    b"run_finished",
    b"run_failed",
    b"run_interrupted",
)
"""A line holding none of these, and no update of a span of a kind the fold reads, is not an event it reads."""
_OPEN_SPANS: Final = (b"span_updated",)
_OPEN_KINDS: Final = (b"workstream", b"adoption", b"gate")
_SPAN_EVENTS: Final = frozenset({"span_started", "span_updated", "span_ended"})
_ENDED: Final = frozenset({"done", "failed", "stopped"})
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
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    # A stamp with no offset is UTC, as the journal writes it; ``timestamp()`` would read it
    # as the host's local time.
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).timestamp()


class _Gate(NamedTuple):
    """A gate span's verdict, measured value, ratio to the input, and when it ended (or began)."""

    verdict: str | None
    value: float | None
    ratio: float | None
    at: float


class _Measure(NamedTuple):
    value: float | None
    ratio: float | None
    at: float


def _outcome(label: dict[str, JsonValue], span: dict[str, JsonValue]) -> str:
    """The run page's outcome of one attempt: a verdict of passed passes, any other verdict or an ended span fails."""
    verdict = label.get("verdict")
    if verdict == "passed":
        return "passed"
    return "failed" if verdict is not None or span.get("status") in _ENDED else "running"


def _last_measure(gates: list[_Gate]) -> _Measure | None:
    shown = [gate for gate in gates if gate.value is not None or gate.verdict is not None]
    return None if not shown else _Measure(shown[-1].value, shown[-1].ratio, shown[-1].at)


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
        self.parents: dict[str, str | None] = {}
        """Each span's parent, by span id, in the order the spans first appeared (the run page's order)."""
        self.attempts: dict[str, tuple[str, str]] = {}
        """Each non-probe workstream's idea and outcome (``passed``, ``failed`` or ``running``), by span id."""
        self.gates: dict[str, _Gate] = {}
        """Each gate span, by span id, as its latest event left it."""
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
        parent = span.get("parent")
        self.parents[span_id] = parent if isinstance(parent, str) else None
        if label.get("kind") == "workstream":
            hypothesis = _text(attrs.get("hypothesis_id"))
            idea = f"span:{span_id}" if hypothesis is None else f"hypothesis:{hypothesis}"
            probe = attrs.get("probe") is True
            self.workstreams[span_id] = None if probe else (idea, _text(attrs.get("revision")))
            if probe:
                self.attempts.pop(span_id, None)
            else:
                self.attempts[span_id] = (idea, _outcome(label, span))
        elif label.get("kind") == "gate":
            verdict = label.get("verdict")
            self.gates[span_id] = _Gate(
                verdict if isinstance(verdict, str) else None,
                _number(attrs.get("metric_value")),
                _number(attrs.get("vs_baseline")),
                _number(span.get("ended_at")) or _number(span.get("started_at")) or 0.0,
            )
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

    def _owner(self, span_id: str) -> str | None:
        """The workstream a span belongs to: the nearest one above it, ``None`` for a probe or none."""
        seen: set[str] = set()
        above = self.parents.get(span_id)
        while above is not None and above not in seen:
            if above in self.attempts:
                return above
            seen.add(above)
            above = self.parents.get(above)
        return None

    def _candidates(self) -> list[tuple[bool, _Measure | None]]:
        """The run page's candidates: whether each idea passed, and its shown measurement, in the order they began.

        An attempt's measurement is its last gate that has a value or a verdict; an idea shows the
        last such attempt's among its passed attempts (all of them when none passed), and is running
        while any attempt runs.
        """
        gates_of: dict[str, list[_Gate]] = {}
        for gate_id, gate in self.gates.items():
            owner = self._owner(gate_id)
            if owner is not None:
                gates_of.setdefault(owner, []).append(gate)
        ideas: dict[str, list[str]] = {}
        for span_id, (idea, _) in self.attempts.items():
            ideas.setdefault(idea, []).append(span_id)
        found: list[tuple[bool, _Measure | None]] = []
        for attempt_ids in ideas.values():
            outcomes = [self.attempts[each][1] for each in attempt_ids]
            outcome = (
                "running"
                if "running" in outcomes
                else "passed"
                if "passed" in outcomes
                else "failed"
            )
            shown = [
                measure
                for each in attempt_ids
                if outcome != "passed" or self.attempts[each][1] == "passed"
                if (measure := _last_measure(gates_of.get(each, []))) is not None
            ]
            found.append((outcome == "passed", shown[-1] if shown else None))
        return found

    def _best_so_far_ratio(self) -> float | None:
        """What the run page headlines while the run works: the best idea so far, as a ratio.

        Before the run opens an adoption, the page's best is the latest idea that passed and beat the
        input and every passed idea before it, in the order their measurements ended (the chart's
        step line); the input's value is any measurement's value over its ratio to the input. Worded
        as an improvement like ``_adopted_ratio``. Nothing once the run has opened an adoption:
        from then the adoption's result is the headline, or none.
        """
        if self.adoptions:
            return None
        candidates = self._candidates()
        standing: float | None = None
        for _, measure in candidates:
            if measure and measure.value is not None and measure.ratio:
                standing = measure.value / measure.ratio
                break
        measured = sorted(
            (
                (passed, measure)
                for passed, measure in candidates
                if measure and measure.value is not None
            ),
            key=lambda each: each[1].at if each[1] else 0.0,
        )
        leader: _Measure | None = None
        for passed, measure in measured:
            value = measure.value if measure else None
            if not passed or measure is None or value is None:
                continue
            if standing is None or (value < standing if self.minimize else value > standing):
                leader, standing = measure, value
        if leader is None or leader.ratio is None:
            return None
        return 1 / leader.ratio if self.minimize and leader.ratio != 0 else leader.ratio

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
            best_so_far_ratio=self._best_so_far_ratio(),
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
