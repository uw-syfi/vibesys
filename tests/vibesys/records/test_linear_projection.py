"""An event costs what it touches, not what the run has accumulated.

Projecting an event used to decode every span of the run so far and encode them all again, and
the epoch header with them: with a 600 idea run's 1,500 spans, a late event cost 4 times what an
early one did, and the run's whole projection the square of its length (40 s of CPU where a
linear projection takes 2). The cost is counted, not timed: the protobuf decodes and encodes one
event makes on a state grown by ``n`` entries of one kind and by ``4 * n``, so the check is exact
and never flaky. Each kind of state the projection keeps is grown in turn, because a table that
is decoded or copied whole on every event is the same defect wherever it sits.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.support.codec_work import CodecWork, codec_work

import vibesys.events as e
from vibesys.api import SteerDelivery, SteerDeliveryState, SteerMode
from vibesys.records.api import MappingState, map_event

if TYPE_CHECKING:
    from collections.abc import Callable

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_PROBE = "probe"
_PROBE_ROUND = 999
_RUN = "run-a"


def _event(
    kind: e.CoreEventType, data: e.CoreEventData | None, sequence: int, **envelope: object
) -> e.CoreEvent:
    return e.CoreEvent.model_validate(
        {"type": kind, "data": data, "timestamp": _AT, "run_id": _RUN, "sequence": sequence}
        | envelope
    )


def _span(
    identity: str,
    *,
    label: tuple[str, dict[str, object]] | None = None,
    invocation: str | None = None,
    done: bool = True,
) -> e.CoreEvent:
    """A span event: ``identity``, done or running, with a label of ``(kind, attrs)``."""
    wire: dict[str, object] = {
        "id": identity,
        "parent": None,
        "type": "operation",
        "name": "work",
        "status": "done" if done else "running",
        "started_at": 0,
        "ended_at": 1 if done else None,
        "invocation": invocation,
    }
    if label is not None:
        wire["label"] = {"kind": label[0], "attrs_json": json.dumps(label[1])}
    if done:
        return _event(e.CoreEventType.SPAN_ENDED, e.SpanEndedData.model_validate({"span": wire}), 0)
    return _event(e.CoreEventType.SPAN_STARTED, e.SpanStartedData.model_validate({"span": wire}), 0)


def _gate(identity: str, series: str, value: float) -> e.CoreEvent:
    row = {"name": series, "value": value, "direction": "max"}
    attrs: dict[str, object] = {"stage": "benchmark", "metrics": [row]}
    return _span(identity, label=("gate", attrs))


def _quota(kind: e.CoreEventType, provider: str, **envelope: object) -> e.CoreEvent:
    data: e.CoreEventData
    if kind is e.CoreEventType.QUOTA_PAUSED:
        data = e.QuotaPausedData(provider=provider, condition="quota_exhausted", detail="wait")
    else:
        data = e.QuotaResumedData(provider=provider)
    return _event(kind, data, 0, **envelope)


def _steer(control: str) -> e.CoreEvent:
    delivery = SteerDelivery(
        state=SteerDeliveryState.QUEUED, control_id=control, mode=SteerMode.NEXT_TURN
    )
    return _event(e.CoreEventType.STEER_QUEUED, e.SteerDeliveryData(delivery=delivery), 0)


def _chunk(execution: str) -> e.CoreEvent:
    data = e.AgentOutputChunkData(channel="assistant", content="text")
    return _event(e.CoreEventType.AGENT_OUTPUT_CHUNK, data, 0, execution_id=execution)


def _finish(execution: str) -> e.CoreEvent:
    """The end of the turn a transcript chunk opened, so that no span is left running."""
    data = e.AgentExecutionFinishedData(result="done")
    return _event(e.CoreEventType.AGENT_EXECUTION_FINISHED, data, 0, execution_id=execution)


def _rate_limit(provider: str, used: float) -> e.CoreEvent:
    data = e.RateLimitUpdateData(provider=provider, window="w", used_fraction=used, exhausted=False)
    return _event(e.CoreEventType.RATE_LIMIT_UPDATE, data, 0)


def _workstream(index: int, name: str) -> e.CoreEvent:
    attrs: dict[str, object] = {"round": index, "revision": f"rev-{name}"}
    return _span(f"op:ws-{name}", label=("workstream", attrs))


def _adoption(name: str) -> e.CoreEvent:
    return _span(f"op:adopt-{name}", label=("adoption", {"candidate": f"rev-{name}"}))


def _turn(name: str) -> e.CoreEvent:
    attrs: dict[str, object] = {"agent": "agent-a", "role": "implementer"}
    return _span(f"op:turn-{name}", label=("agent_turn", attrs), invocation=f"turn-{name}")


def _comparison(candidate: str) -> e.CoreEvent:
    data = e.MeasurementComparedData(
        metric="m",
        direction="max",
        candidate_commit=f"rev-{candidate}",
        base_commit="rev-base",
        candidate_value=2.0,
        base_value=1.0,
        ratio=2.0,
        noise=0.1,
        verdict="better",
        candidate_samples=1,
        base_samples=1,
    )
    return _event(e.CoreEventType.MEASUREMENT_COMPARED, data, 0)


def _round_finished() -> e.CoreEvent:
    data = e.RoundFinishedData(attempts=1, judge_verdict="pass", profile_skipped=True)
    return _event(e.CoreEventType.ROUND_FINISHED, data, 0, round_label=f"round-{_PROBE_ROUND}")


def _winner() -> e.CoreEvent:
    data = e.WinnerSelectedData(selection="candidate", revision=f"rev-{_PROBE}")
    return _event(e.CoreEventType.WINNER_SELECTED, data, 0)


def _start() -> list[e.CoreEvent]:
    """What every run has, and what the events measured below act on."""
    started = e.RunStartedData(outer_loop="test", input="input")
    return [
        _event(e.CoreEventType.RUN_STARTED, started, 0),
        _workstream(_PROBE_ROUND, _PROBE),
        _adoption(_PROBE),
        _turn(_PROBE),
        _gate("op:gate-probe", "m", 1.0),
        _rate_limit(_PROBE, 0.5),
        _quota(e.CoreEventType.QUOTA_PAUSED, "prov", execution_id=f"turn-{_PROBE}"),
        _quota(e.CoreEventType.QUOTA_RESUMED, "prov", execution_id=f"turn-{_PROBE}"),
    ]


@dataclass(frozen=True)
class _Table:
    """One kind of state the projection keeps, and the events that grow it by one entry."""

    name: str
    grow: Callable[[int], list[e.CoreEvent]]


_TABLES = (
    _Table("spans", lambda i: [_span(f"op:plain-{i}")]),
    _Table("workstreams", lambda i: [_workstream(i, str(i))]),
    _Table("adoptions", lambda i: [_adoption(str(i))]),
    _Table("turns", lambda i: [_turn(str(i))]),
    _Table("readings", lambda i: [_gate(f"op:gate-{i}", "m", float(i))]),
    _Table("streams", lambda i: [_chunk(f"stream-{i}"), _finish(f"stream-{i}")]),
    _Table(
        "waits",
        lambda i: [
            _quota(e.CoreEventType.QUOTA_PAUSED, f"prov-{i}"),
            _quota(e.CoreEventType.QUOTA_RESUMED, f"prov-{i}"),
        ],
    ),
    _Table("intents", lambda i: [_steer(f"control-{i}")]),
    _Table("series", lambda i: [_rate_limit(f"provider-{i}", 0.25)]),
)


def _todo() -> e.CoreEvent:
    return _event(e.CoreEventType.TODO_UPDATE, e.TodoUpdateData(), 0)


# Every kind of event a mapper handles differently, each acting on what ``_start`` made.
_EVENTS: dict[str, Callable[[], e.CoreEvent]] = {
    "dropped": _todo,
    "transcript": lambda: _chunk(f"turn-{_PROBE}"),
    "new transcript": lambda: _chunk("fresh"),
    "span update": lambda: _workstream(_PROBE_ROUND, _PROBE),
    "new span": lambda: _span("op:fresh"),
    "reading": lambda: _gate("op:gate-fresh", "m", 7.0),
    "reading of a declared series": lambda: _rate_limit(_PROBE, 0.75),
    "new series": lambda: _rate_limit("fresh", 0.75),
    "round finished": _round_finished,
    "winner selected": _winner,
    "measurement compared": lambda: _comparison(_PROBE),
    "quota resumed": lambda: _quota(
        e.CoreEventType.QUOTA_RESUMED, "prov", execution_id=f"turn-{_PROBE}"
    ),
    "steer": lambda: _steer("fresh"),
    "run stopped": lambda: _event(e.CoreEventType.STOPPED, None, 0),
}


# A new series amends the epoch declaration, and the amendment is a record that carries the whole
# declaration, one row longer for every series: its bytes are the output's size, so only its calls
# are held to the run's length.
_AMENDS = frozenset({"new series"})


def _fold(state: MappingState, events: list[e.CoreEvent]) -> MappingState:
    """``state`` after ``events``, numbered in order after the sequences it has seen."""
    for event in events:
        state = map_event(
            state, event.model_copy(update={"sequence": state.source_through + 1})
        ).state
    return state


def _cost(state: MappingState, event: e.CoreEvent) -> CodecWork:
    """The codec work of projecting ``event`` as the next of ``state``'s journal."""
    numbered = event.model_copy(update={"sequence": state.source_through + 1})
    _, work = codec_work(lambda: map_event(state, numbered))
    return work


@cache
def _started() -> MappingState:
    """The state every run starts from; opening it writes the epoch declaration, the dear part."""
    return _fold(MappingState(_RUN), _start())


@cache
def _grown(table: _Table, entries: int) -> tuple[MappingState, MappingState]:
    """The state after ``entries`` and after ``4 * entries`` entries of ``table`` (a prefix).

    States never change, so each is built once however many examples ask for it.
    """
    small = _fold(_started(), [event for i in range(entries) for event in table.grow(i)])
    rest = [event for i in range(entries, 4 * entries) for event in table.grow(i)]
    return small, _fold(small, rest)


@cache
def _costs(table: _Table, entries: int) -> dict[str, tuple[CodecWork, CodecWork]]:
    """What each kind of event costs on the short run and on the run four times as long."""
    small, long = _grown(table, entries)
    return {name: (_cost(small, build()), _cost(long, build())) for name, build in _EVENTS.items()}


@pytest.mark.regression
@pytest.mark.parametrize("table", _TABLES, ids=lambda table: table.name)
@example(entries=3)
@given(entries=st.integers(min_value=2, max_value=4))
def test_an_event_decodes_and_encodes_as_much_on_a_long_run_as_on_a_short_one(
    table: _Table, entries: int
) -> None:
    """Each kind of event, on a run grown by ``n`` and by ``4 * n`` entries of one kind of state.

    The calls must be equal, and the bytes they move may differ only by the few a longer
    sequence number or clock takes; a table decoded whole moves at least a span's worth per entry.
    """
    for name, (short, big) in _costs(table, entries).items():
        assert big.calls == short.calls, (name, short, big)
        if name not in _AMENDS:
            assert big.size <= short.size + 4 * short.calls, (name, short, big)


@pytest.mark.regression
@pytest.mark.parametrize("table", _TABLES, ids=lambda table: table.name)
@example(entries=3)
@given(entries=st.integers(min_value=1, max_value=4))
def test_an_event_nothing_maps_decodes_nothing_and_shares_every_table(
    table: _Table, entries: int
) -> None:
    """A dropped event leaves the state's tables the very objects they were, at any length."""
    state, _ = _grown(table, entries)
    event = _todo().model_copy(update={"sequence": state.source_through + 1})
    result, work = codec_work(lambda: map_event(state, event))
    assert work == CodecWork(0, 0)
    assert result.records == ()
    for field in dataclasses.fields(MappingState):
        if field.name not in {"sequence", "source_through", "source_pending", "pending"}:
            assert getattr(result.state, field.name) is getattr(state, field.name), field.name


@pytest.mark.regression
@pytest.mark.parametrize("table", _TABLES, ids=lambda table: table.name)
@example(entries=3)
@given(entries=st.integers(min_value=1, max_value=4))
def test_a_transcript_event_shares_every_table_but_the_two_it_changes(
    table: _Table, entries: int
) -> None:
    """A transcript chunk writes a stream and its turn's span; no other table is copied."""
    state, _ = _grown(table, entries)
    event = _chunk(f"turn-{_PROBE}").model_copy(update={"sequence": state.source_through + 1})
    result = map_event(state, event)
    written = {"sequence", "source_through", "source_pending", "pending", "streams", "span_table"}
    for field in dataclasses.fields(MappingState):
        if field.name not in written:
            assert getattr(result.state, field.name) is getattr(state, field.name), field.name
    assert result.state.streams != state.streams
