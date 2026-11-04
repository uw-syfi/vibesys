"""The spans a run keeps stay in the order they were first seen, and every lookup finds what a scan would.

The projection used to hold its spans as one ordered table it scanned; it holds them in an index
now, so each answer below is checked against the plain scan written here from the events alone.
The generators build journal events, never projection state, so nothing under test makes its own
oracle. Span names are drawn in an order unrelated to the order the spans are created in, so a
lookup that returned them sorted by name or by hash would be caught.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from vibesys.records.api import MappedEvent, MappingState, map_event

if TYPE_CHECKING:
    from collections.abc import Sequence

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_NAMES = ("m", "c", "x", "a", "q", "f")
_LATE = 1000


def _event(kind: e.CoreEventType, data: e.CoreEventData | None, **envelope: object) -> e.CoreEvent:
    return e.CoreEvent.model_validate(
        {"type": kind, "data": data, "timestamp": _AT, "run_id": "run-a", "sequence": 0} | envelope
    )


def _span_event(
    name: str, *, done: bool, kind: str | None = None, attrs: dict[str, object] | None = None
) -> e.CoreEvent:
    wire: dict[str, object] = {
        "id": f"op:{name}",
        "parent": None,
        "type": "operation",
        "name": "work",
        "status": "done" if done else "running",
        "started_at": 0,
        "ended_at": 1 if done else None,
    }
    if kind is not None:
        wire["label"] = {"kind": kind, "attrs_json": json.dumps(attrs or {})}
    if done:
        return _event(e.CoreEventType.SPAN_ENDED, e.SpanEndedData.model_validate({"span": wire}))
    return _event(e.CoreEventType.SPAN_STARTED, e.SpanStartedData.model_validate({"span": wire}))


def _feed(events: Sequence[e.CoreEvent]) -> tuple[MappingState, list[MappedEvent]]:
    state = MappingState("run-a")
    results: list[MappedEvent] = []
    for number, event in enumerate(events, 1):
        results.append(map_event(state, event.model_copy(update={"sequence": number})))
        state = results[-1].state
    return state, results


def _span_records(result: MappedEvent) -> list[p.Span]:
    return [
        r.body.value.span
        for r in result.records
        if r.body is not None and r.body.field == "span" and r.body.value.span is not None
    ]


@given(steps=st.lists(st.tuples(st.sampled_from(_NAMES), st.booleans()), min_size=1, max_size=30))
@example(steps=[("m", False), ("c", False), ("m", True), ("a", False), ("c", True)])
def test_spans_stay_in_first_seen_order_and_keep_their_latest_version(
    steps: list[tuple[str, bool]],
) -> None:
    """Whatever order spans are updated in, the state lists them as first seen, each as last written."""
    state = MappingState("run-a")
    seen: list[str] = []
    latest: dict[str, p.Span] = {}
    for number, (name, done) in enumerate(steps, 1):
        event = _span_event(name, done=done).model_copy(update={"sequence": number})
        result = map_event(state, event)
        state = result.state
        if f"op:{name}" not in seen:
            seen.append(f"op:{name}")
        for span in _span_records(result):
            latest[span.id] = span
        assert [key for key, _ in state.spans] == seen
        for key, wire in state.spans:
            decoded = p.Span.from_binary(wire)
            assert decoded == latest[key]
            assert decoded.to_binary() == wire


@dataclass(frozen=True)
class _Workstream:
    name: str
    round: int | None
    sequence: int | None
    revision: str | None

    def attrs(self) -> dict[str, object]:
        """The label attributes it sets: the ones it has."""
        return {
            name: value
            for name, value in (
                ("round", self.round),
                ("sequence", self.sequence),
                ("revision", self.revision),
            )
            if value is not None
        }


@st.composite
def _workstreams(draw: st.DrawFn) -> list[_Workstream]:
    """Workstreams created in a drawn order under names that do not follow it."""
    count = draw(st.integers(min_value=1, max_value=len(_NAMES)))
    names = draw(st.permutations(_NAMES))[:count]
    ordinal = st.one_of(st.none(), st.integers(min_value=0, max_value=3))
    revision = st.one_of(st.none(), st.sampled_from(["r0", "r1", "r2"]))
    return [_Workstream(name, draw(ordinal), draw(ordinal), draw(revision)) for name in names]


def _workstream_events(workstreams: Sequence[_Workstream]) -> list[e.CoreEvent]:
    """Each workstream started in turn, then every one updated again in reverse order."""
    started = [
        _span_event(w.name, done=False, kind="workstream", attrs=w.attrs()) for w in workstreams
    ]
    updated = [
        _span_event(w.name, done=True, kind="workstream", attrs=w.attrs())
        for w in reversed(workstreams)
    ]
    return started + updated


@given(workstreams=_workstreams(), wanted=st.integers(min_value=0, max_value=4))
@example(
    workstreams=[_Workstream("m", 1, 1, None), _Workstream("c", 1, None, None)],
    wanted=1,
)
def test_a_round_event_finds_the_first_workstream_that_a_scan_would(
    workstreams: list[_Workstream], wanted: int
) -> None:
    """``round_finished`` names a round; the first-seen workstream with that round or sequence owns it."""
    state, _ = _feed(_workstream_events(workstreams))
    owners = [f"op:{w.name}" for w in workstreams if wanted in {w.round, w.sequence}]
    finished = _event(
        e.CoreEventType.ROUND_FINISHED,
        e.RoundFinishedData(attempts=1, judge_verdict="pass", profile_skipped=True),
        round_label=f"round-{wanted}",
        sequence=_LATE,
    )
    result = map_event(state, finished)
    if owners:
        assert [span.id for span in _span_records(result)] == [owners[0]]
        assert not result.state.pending
    else:
        assert not _span_records(result)
        assert len(result.state.pending) == 1


@given(workstreams=_workstreams(), pair=st.tuples(*[st.sampled_from(["r0", "r1", "r2"])] * 2))
@example(
    workstreams=[_Workstream("m", None, None, "r1"), _Workstream("c", None, None, "r1")],
    pair=("r1", "r2"),
)
def test_a_comparison_links_every_workstream_of_a_revision_in_first_seen_order(
    workstreams: list[_Workstream], pair: tuple[str, str]
) -> None:
    """``measurement_compared`` links, for each of its revisions, the workstreams that made it."""
    state, _ = _feed(_workstream_events(workstreams))
    candidate, base = pair
    compared = _event(
        e.CoreEventType.MEASUREMENT_COMPARED,
        e.MeasurementComparedData(
            metric="m",
            direction="max",
            candidate_commit=candidate,
            base_commit=base,
            candidate_value=2.0,
            base_value=1.0,
            ratio=2.0,
            noise=0.1,
            verdict="better",
            candidate_samples=1,
            base_samples=1,
        ),
        sequence=_LATE,
    )
    comparison = _span_records(map_event(state, compared))[-1]
    expected = [
        (rel, f"op:{w.name}")
        for rel, revision in (("candidate", candidate), ("base", base))
        for w in workstreams
        if w.revision == revision
    ]
    assert comparison.label is not None
    assert [(link.rel, link.to) for link in comparison.label.links] == expected


@st.composite
def _adoptions(draw: st.DrawFn) -> list[tuple[str, str | None]]:
    """Adoptions, created in a drawn order under names that do not follow it, of a candidate or none."""
    count = draw(st.integers(min_value=1, max_value=len(_NAMES)))
    names = draw(st.permutations(_NAMES))[:count]
    candidate = st.one_of(st.none(), st.sampled_from(["r0", "r1", "r2"]))
    return [(name, draw(candidate)) for name in names]


@given(
    adoptions=_adoptions(),
    wanted=st.one_of(
        st.just(("baseline", "r0")), st.tuples(st.just("candidate"), st.sampled_from(["r0", "r1"]))
    ),
)
@example(adoptions=[("m", "r0"), ("c", "r0"), ("a", None)], wanted=("candidate", "r0"))
def test_a_selection_settles_the_last_matching_adoption_that_a_scan_would(
    adoptions: list[tuple[str, str | None]], wanted: tuple[str, str]
) -> None:
    """``winner_selected`` updates the last-seen adoption of its candidate, or of the baseline."""
    events = [
        _span_event(
            name,
            done=True,
            kind="adoption",
            attrs={} if candidate is None else {"candidate": candidate},
        )
        for name, candidate in adoptions
    ]
    state, _ = _feed(events)
    selection, revision = wanted
    chosen = _event(
        e.CoreEventType.WINNER_SELECTED,
        e.WinnerSelectedData.model_validate({"selection": selection, "revision": revision}),
        sequence=_LATE,
    )
    result = map_event(state, chosen)
    target = revision if selection == "candidate" else None
    matches = [f"op:{name}" for name, candidate in adoptions if candidate == target]
    if matches:
        assert [span.id for span in _span_records(result)] == [matches[-1]]
    else:
        assert not _span_records(result)


@given(
    done=st.lists(st.booleans(), min_size=1, max_size=len(_NAMES)),
    names=st.permutations(_NAMES),
)
@example(done=[False, True, False, False], names=list(_NAMES))
def test_a_stop_ends_exactly_the_running_spans_in_first_seen_order(
    done: list[bool], names: list[str]
) -> None:
    """The unfinished spans of any run are ended by a stop, oldest first, and no other."""
    events = [_span_event(name, done=finished) for name, finished in zip(names, done, strict=False)]
    state, _ = _feed(events)
    result = map_event(state, _event(e.CoreEventType.STOPPED, None, sequence=_LATE))
    ended = [span.id for span in _span_records(result)]
    assert ended == [f"op:{n}" for n, finished in zip(names, done, strict=False) if not finished]
    assert all(span.status == p.SpanStatus.STOPPED for span in _span_records(result))
