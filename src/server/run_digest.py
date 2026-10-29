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

Public surface: ``digest_journal``, ``run_attention``, ``digest_store``.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Final, NamedTuple

from pydantic import JsonValue, TypeAdapter, ValidationError

from server.host_api import (
    RunClosing,
    RunDigest,
    RunEndingFact,
    RunEndOutcome,
    RunPhase,
    RunStatus,
)
from server.host_run_dashboard import (
    DASHBOARD_MAX_POINTS,
    REPEATED_FAILURE_AFTER,
    STALLED_AFTER_S,
    BestReading,
    IdeaPoint,
    IdeaVerdict,
    RunAttention,
    RunDashboard,
)
from server.host_run_summary import TraceFormat, trace_format
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vs_proto.api import Keyframe, RunHead, Span

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


def _span_time(span: dict[str, JsonValue]) -> float:
    """When a span ended, else when it began, else 0: the run page's ``ended_at ?? started_at``.

    A time of exactly 0 is a time (the run clock's first instant), so it is never skipped as falsy.
    """
    ended = _number(span.get("ended_at"))
    if ended is not None:
        return ended
    began = _number(span.get("started_at"))
    return 0.0 if began is None else began


class _Gate(NamedTuple):
    """A gate span's verdict, measured value, ratio to the input, and when it ended (or began)."""

    verdict: str | None
    value: float | None
    ratio: float | None
    at: float
    wall: float | None
    """When the journal event that last wrote the gate was recorded (epoch seconds)."""


class _Measure(NamedTuple):
    value: float | None
    ratio: float | None
    at: float
    wall: float | None


class _Attempt(NamedTuple):
    """A non-probe workstream: its idea, its outcome (``passed``, ``failed`` or ``running``) and when it ended (or began)."""

    idea: str
    outcome: str
    at: float
    wall: float | None


class _Idea(NamedTuple):
    """An idea as the run page's candidate: its outcome, its shown measurement and when it last moved."""

    outcome: str
    measure: _Measure | None
    at: float
    wall: float | None


class _Plotted(NamedTuple):
    """One point of the chart: the idea's verdict and measurement, and when its position landed."""

    verdict: IdeaVerdict
    measure: _Measure | None
    wall: float | None
    """The journal time of the reading that plots it, else of the idea's last move (epoch seconds)."""


def _outcome(label: dict[str, JsonValue], span: dict[str, JsonValue]) -> str:
    """The run page's outcome of one attempt: a verdict of passed passes, any other verdict or an ended span fails."""
    verdict = label.get("verdict")
    if verdict == "passed":
        return "passed"
    return "failed" if verdict is not None or span.get("status") in _ENDED else "running"


def _last_measure(gates: list[_Gate]) -> _Measure | None:
    shown = [gate for gate in gates if gate.value is not None or gate.verdict is not None]
    return (
        None
        if not shown
        else _Measure(shown[-1].value, shown[-1].ratio, shown[-1].at, shown[-1].wall)
    )


class _Fold:
    """The figures read so far."""

    def __init__(self) -> None:
        self.first: float | None = None
        self.last: float | None = None
        self.minimize = False
        self.metric: str | None = None
        self.unit: str | None = None
        self.workstreams: dict[str, tuple[str, str | None] | None] = {}
        """Each workstream span's idea and revision, by span id, as its latest event left it; ``None`` for a probe."""
        self.adoptions: dict[str, tuple[str | None, dict[str, JsonValue]]] = {}
        """Each adoption span's verdict and attributes, by span id, in the order they started."""
        self.parents: dict[str, str | None] = {}
        """Each span's parent, by span id, in the order the spans first appeared (the run page's order)."""
        self.attempts: dict[str, _Attempt] = {}
        """Each non-probe workstream, by span id."""
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
            self._span(_object(data.get("span")), _seconds(event))
        elif kind == "run_started":
            objective = _object(_object(data.get("overview")).get("objective"))
            self.minimize = objective.get("direction") == "min"
            self.metric = _text(objective.get("metric"))
            self.unit = _text(objective.get("unit"))
        elif kind == "run_ending":
            self._ending(data)
        elif isinstance(kind, str) and kind in _CLOSINGS:
            self.closing = _CLOSINGS[kind]

    def _span(self, span: dict[str, JsonValue], wall: float | None) -> None:
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
                self.attempts[span_id] = _Attempt(
                    idea,
                    _outcome(label, span),
                    _span_time(span),
                    wall,
                )
        elif label.get("kind") == "gate":
            verdict = label.get("verdict")
            self.gates[span_id] = _Gate(
                verdict if isinstance(verdict, str) else None,
                _number(attrs.get("metric_value")),
                _number(attrs.get("vs_baseline")),
                _span_time(span),
                wall,
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

    def _candidates(self) -> list[_Idea]:
        """The run page's candidates, in the order they began.

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
        for span_id, attempt in self.attempts.items():
            ideas.setdefault(attempt.idea, []).append(span_id)
        found: list[_Idea] = []
        for attempt_ids in ideas.values():
            outcomes = [self.attempts[each].outcome for each in attempt_ids]
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
                if outcome != "passed" or self.attempts[each].outcome == "passed"
                if (measure := _last_measure(gates_of.get(each, []))) is not None
            ]
            moved = max(self.attempts[each].at for each in attempt_ids)
            walls = [wall for each in attempt_ids if (wall := self.attempts[each].wall) is not None]
            found.append(
                _Idea(outcome, shown[-1] if shown else None, moved, max(walls, default=None))
            )
        return found

    def _chart(self) -> list[_Plotted]:
        """The run page's chart (``performanceFold``): each idea's verdict and measurement, in plot order.

        The ideas whose gate read a value are plotted in the order the readings ended (ties in the
        order the ideas began); the others take the place of when they last moved. The input stands
        as the best until a passed idea beats it (the input's value is any measurement's value over
        its ratio to the input): that idea is ``kept`` and becomes the best. A passed idea that does
        not beat the best is ``not_better``; the workstream's own verdict decides passed or failed,
        never a number alone.
        """
        ideas = self._candidates()
        standing: float | None = None
        for idea in ideas:
            measure = idea.measure
            if measure and measure.value is not None and measure.ratio:
                standing = measure.value / measure.ratio
                break

        def plotted_at(idea: _Idea) -> float:
            measure = idea.measure
            return measure.at if measure and measure.value is not None else idea.at

        chart: list[_Plotted] = []
        for _, idea in sorted(enumerate(ideas), key=lambda each: (plotted_at(each[1]), each[0])):
            measure, value = idea.measure, idea.measure.value if idea.measure else None
            if idea.outcome == "running":
                verdict = IdeaVerdict.RUNNING
            elif idea.outcome == "failed":
                verdict = IdeaVerdict.FAILED
            elif value is not None and (
                standing is None or (value < standing if self.minimize else value > standing)
            ):
                verdict, standing = IdeaVerdict.KEPT, value
            else:
                verdict = IdeaVerdict.NOT_BETTER
            landed = measure.wall if measure and measure.value is not None else idea.wall
            chart.append(_Plotted(verdict, measure, landed))
        return chart

    def _improvement(self, measure: _Measure | None) -> float | None:
        """``measure``'s ratio to the input, worded as an improvement (the digest's wording)."""
        if measure is None or measure.ratio is None:
            return None
        return 1 / measure.ratio if self.minimize and measure.ratio != 0 else measure.ratio

    def _best_so_far_ratio(self, chart: list[_Plotted]) -> float | None:
        """What the run page headlines while the run works: the best idea so far, as a ratio.

        Before the run opens an adoption, the page's best is the last ``kept`` idea of the ``chart``.
        Nothing once the run has opened an adoption: from then the adoption's result is the
        headline, or none.
        """
        if self.adoptions:
            return None
        kept = [each.measure for each in chart if each.verdict is IdeaVerdict.KEPT]
        return self._improvement(kept[-1]) if kept else None

    def _best_reading(self, chart: list[_Plotted]) -> BestReading | None:
        """The reading of the last ``kept`` idea of the ``chart``: the best so far, in the metric's own terms."""
        kept = [each.measure for each in chart if each.verdict is IdeaVerdict.KEPT]
        measure = kept[-1] if kept else None
        if measure is None or measure.value is None:
            return None
        return BestReading(value=measure.value, metric=self.metric, unit=self.unit)

    def _dashboard(self, chart: list[_Plotted]) -> RunDashboard:
        points = [
            IdeaPoint(
                ratio=self._improvement(each.measure),
                verdict=each.verdict,
                at=None if each.wall is None else datetime.fromtimestamp(each.wall, UTC),
            )
            for each in chart
        ]
        last = None if self.last is None else datetime.fromtimestamp(self.last, UTC)
        return _dashboard_points(points, last, self._best_reading(chart))

    def _ending(self, data: dict[str, JsonValue]) -> None:
        try:
            self.ending = RunEndingFact.model_validate(
                {
                    "outcome": data.get("reason"),
                    "summary": data.get("summary", ""),
                    "resumable": data.get("resumable"),
                    "rounds_finished": data.get("rounds_finished"),
                    "uncompared": data.get("uncompared", False),
                }
            )
        except ValidationError:
            self.ending = None

    def digest(self) -> RunDigest:
        duration = None if self.first is None or self.last is None else self.last - self.first
        recorded = trace_format(spans=self.spans, rounds=self.rounds) is TraceFormat.RECORDED
        chart = self._chart()
        return RunDigest(
            ideas=len({made[0] for made in self.workstreams.values() if made})
            if recorded
            else None,
            adopted_ratio=self._adopted_ratio(),
            best_so_far_ratio=self._best_so_far_ratio(chart),
            duration_s=duration,
            ending=self.ending,
            closing=self.closing,
            dashboard=self._dashboard(chart),
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


def run_attention(digest: RunDigest, *, phase: RunPhase | None, now: float) -> RunAttention:
    """Why the run behind ``digest`` needs its user, as of ``now`` (epoch seconds, given as data).

    A pure function of the journal's facts (``digest``), the phase of the server that drives the run
    (``None``: no server here) and the time: nothing here reads a clock, so a test names any time
    it likes. In order: ``failed`` when the run ended in failure (its terminal event, its ending or
    its server's phase says so); nothing once it ended any other way; ``stalled`` when its server
    works (``running``) and the journal's last event is older than ``STALLED_AFTER_S``;
    ``repeated_failure`` when it works and its last ``REPEATED_FAILURE_AFTER`` ideas failed. A
    paused run is silent by design, so it is neither stalled nor failing.
    """
    ended = digest.closing is not None or digest.ending is not None
    if (
        digest.closing is RunClosing.FAILED
        or (digest.ending is not None and digest.ending.outcome is RunEndOutcome.FAILED)
        or (not ended and phase is not None and phase.status is RunStatus.FAILED)
    ):
        return RunAttention.FAILED
    if ended or phase is None or phase.status is not RunStatus.RUNNING:
        return RunAttention.NONE
    dashboard = digest.dashboard
    if dashboard is None:
        return RunAttention.NONE
    if (
        dashboard.last_event_at is not None
        and now - dashboard.last_event_at.timestamp() > STALLED_AFTER_S
    ):
        return RunAttention.STALLED
    if dashboard.failed_streak >= REPEATED_FAILURE_AFTER:
        return RunAttention.REPEATED_FAILURE
    return RunAttention.NONE


def _dashboard_points(
    points: list[IdeaPoint], last: datetime | None, best: BestReading | None
) -> RunDashboard:
    streak = 0
    for point in reversed(points):
        if point.verdict is IdeaVerdict.RUNNING:
            continue
        if point.verdict is not IdeaVerdict.FAILED:
            break
        streak += 1
    # Over the cap, the oldest points that are not kept go first, then the oldest kept ones.
    order = sorted(range(len(points)), key=lambda at: (points[at].verdict is IdeaVerdict.KEPT, at))
    gone = set(order[: max(0, len(points) - DASHBOARD_MAX_POINTS)])
    shown = tuple(point for at, point in enumerate(points) if at not in gone)
    return RunDashboard(
        points=shown,
        points_omitted=len(gone),
        failed_streak=streak,
        last_event_at=last,
        best=best,
    )


def digest_store(
    head: RunHead, skeleton: Keyframe, *, last_event_at: datetime | None = None
) -> RunDigest:
    """Project the writer's committed facts; a journal never overrides a typed run.

    Headline and outcome come from the same head the page observes. Idea measurements join
    the skeleton by ownership, and timing comes from that prefix's recorded run clock.
    """
    wire = import_module("vs_proto.api")
    objective = head.objective
    best = (
        None if objective is None or not objective.has_field("best_value") else objective.best_value
    )
    input_value = (
        None
        if objective is None or not objective.has_field("input_value")
        else objective.input_value
    )
    series = None if objective is None else objective.series
    minimize = series is not None and series.direction == wire.Direction.MIN
    ratio = _store_ratio(best, input_value, minimize=minimize)
    spans = {
        key: history.versions[-1].span
        for key, history in skeleton.spans.items()
        if history.versions
    }
    spans = {key: span for key, span in spans.items() if span is not None}
    ideas = _store_ideas(spans)
    points = _store_points(head)
    times = [span.started_at for span in spans.values()]
    times += [span.ended_at for span in spans.values() if span.ended_at is not None]
    times += [
        version.point.clock
        for history in skeleton.metrics
        for version in history.points
        if version.point is not None
    ]
    ending, closing = _store_ending(head)
    return RunDigest(
        ideas=len(set(ideas.values())),
        adopted_ratio=ratio,
        best_so_far_ratio=None,
        duration_s=None if not times else max(0, *times),
        ending=ending,
        closing=closing,
        dashboard=_dashboard_points(
            points,
            last_event_at,
            None
            if best is None
            else BestReading(
                value=best,
                metric=None if series is None else series.name,
                unit=None if series is None else series.unit or None,
            ),
        ),
    )


def _store_ratio(value: float | None, input_value: float | None, *, minimize: bool) -> float | None:
    if value is None or input_value is None or input_value == 0 or (minimize and value == 0):
        return None
    return input_value / value if minimize else value / input_value


def _store_ideas(spans: dict[str, Span]) -> dict[str, str]:
    wire = import_module("vs_proto.api")
    ideas = {}
    for key, span in spans.items():
        label = span.label
        if label is None or label.kind != "workstream" or label.attrs is None:
            continue
        attrs = wire.WorkstreamAttrs.from_binary(label.attrs.value)
        if not attrs.probe:
            ideas[key] = (
                "hypothesis:" + attrs.hypothesis_id if attrs.hypothesis_id else "span:" + key
            )
    return ideas


def _store_points(head: RunHead) -> list[IdeaPoint]:
    """The card renders exactly the writer's idea decisions, never reselects a best."""
    wire = import_module("vs_proto.api")
    objective = head.objective
    if objective is None or objective.series is None:
        return []
    series = next((item for item in head.performance if item.series == objective.series.name), None)
    if series is None:
        return []
    verdicts = {
        wire.IdeaOutcome.PENDING: IdeaVerdict.RUNNING,
        wire.IdeaOutcome.FAILED: IdeaVerdict.FAILED,
        wire.IdeaOutcome.KEPT: IdeaVerdict.KEPT,
        wire.IdeaOutcome.NOT_KEPT: IdeaVerdict.NOT_BETTER,
        wire.IdeaOutcome.WITHIN_NOISE: IdeaVerdict.NOT_BETTER,
        wire.IdeaOutcome.WORSE_THAN_INPUT: IdeaVerdict.NOT_BETTER,
    }
    readings = {point.measurement_seq: point.point for point in series.points}
    return [
        IdeaPoint(
            ratio=_store_ratio(
                None if (reading := readings.get(idea.measurement_seq)) is None else reading.value,
                series.input_value if series.has_field("input_value") else None,
                minimize=objective.series.direction == wire.Direction.MIN,
            ),
            verdict=verdicts[idea.outcome],
        )
        for idea in series.ideas
        if idea.outcome in verdicts
    ]


def _store_ending(head: RunHead) -> tuple[RunEndingFact | None, RunClosing | None]:
    state = None if head.lifecycle is None else head.lifecycle.state
    if state is None or state.field != "ended":
        return None, None
    ended = state.value
    presentation = ended.presentation
    if presentation is None:
        message = "ended.presentation: core outcome is absent"
        raise ValueError(message)
    match presentation.status:
        case "completed":
            outcome, closing = None, RunClosing.FINISHED
        case "failed":
            outcome, closing = RunEndOutcome.FAILED, RunClosing.FAILED
        case "stopped":
            outcome, closing = RunEndOutcome.STOPPED, RunClosing.INTERRUPTED
        case _:
            message = f"ended.presentation.status: unknown outcome {presentation.status}"
            raise ValueError(message)
    return RunEndingFact(
        outcome=outcome, summary=ended.summary, resumable=ended.resumable, rounds_finished=0
    ), closing
