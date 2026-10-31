"""A per-model split the ``.proto`` refuses is rejected before the journal line is written.

The projector validates every record it writes against ``agent_attrs.proto``, and one refused record
disables the run log's tee for the rest of the process. A journal line is durable before it is
projected, so a line that carries rows the proto refuses would be written first and fail the
projection after. The rules live in the proto alone: the sink asks it (through the records
module) before it builds the line, drops a refused split whole, keeps the rest of the turn, and
says why on the diagnostic channel.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_proto.api as p
from vibesys.events import (
    AgentOutputChunkData,
    CoreEvent,
    CoreEventType,
    TurnUsageData,
)
from vibesys.records.api import MappingState, map_event
from vibesys.run import CoreAgentEventSink
from vs_agent.api import AgentModelUsage, AgentUsage, CostBasis, UsageSpeed
from vs_sim.api.testing import ManualClock, examples

_VALID = AgentModelUsage(
    model="claude-opus-5-5",
    input_tokens=100,
    cache_creation_input_tokens=10,
    cache_creation_1h_input_tokens=5,
    cache_read_input_tokens=20,
    output_tokens=30,
    reasoning_output_tokens=4,
    turns=3,
    recorded_usd=0.25,
    speed=None,
    primary=True,
)


def _row(**changes: object) -> AgentModelUsage:
    """A valid primary row, with ``changes`` applied."""
    return replace(_VALID, **changes)


def _turn(rows: tuple[AgentModelUsage, ...]) -> AgentUsage:
    return AgentUsage(
        input_tokens=100,
        output_tokens=30,
        total_cost_usd=0.25,
        cost_basis=CostBasis.PRICE_TABLE,
        by_model=rows,
    )


def _record(usage: AgentUsage) -> list[CoreEvent]:
    events: list[CoreEvent] = []
    sink = CoreAgentEventSink(events.append, clock=ManualClock())
    sink.turn_usage_recorded(
        usage,
        agent_kind="judge",
        round_label="round 1",
        invocation_id="inv-1",
        provider_session_id="session-1",
    )
    return events


def _usage_event(events: list[CoreEvent]) -> TurnUsageData:
    (event,) = (e for e in events if e.type is CoreEventType.TURN_USAGE_RECORDED)
    assert isinstance(event.data, TurnUsageData)
    return event.data


def _diagnostics(events: list[CoreEvent]) -> list[str]:
    return [
        e.data.content
        for e in events
        if isinstance(e.data, AgentOutputChunkData) and e.data.channel == "diagnostic"
    ]


def _projects(events: list[CoreEvent]) -> None:
    """What the run log's tee does with the line: map it and validate every record."""
    state = MappingState("run-1")
    for index, event in enumerate(events, start=1):
        mapped = map_event(state, event.model_copy(update={"run_id": "run-1", "sequence": index}))
        for record in mapped.records:
            p.validate(record)
        state = mapped.state


REFUSED: list[tuple[str, tuple[AgentModelUsage, ...], str]] = [
    ("two primary rows", (_row(), _row(model="b", turns=0)), "exactly one primary"),
    ("no primary row", (_row(primary=False, turns=0),), "exactly one primary"),
    (
        "requests on a row that is not the primary",
        (_row(), _row(model="b", primary=False, turns=2)),
        "by_model[1]: only the primary row counts",
    ),
    (
        "cache reads and writes beyond the input",
        (_row(input_tokens=10, cache_read_input_tokens=8, cache_creation_input_tokens=8),),
        "by_model[0]: cache reads and writes are part of the input",
    ),
    (
        "one-hour writes beyond the writes",
        (_row(cache_creation_input_tokens=2, cache_creation_1h_input_tokens=3),),
        "by_model[0]: one-hour cache writes are part of the cache writes",
    ),
    (
        "reasoning beyond the output",
        (_row(output_tokens=2, reasoning_output_tokens=3),),
        "by_model[0]: reasoning is part of the output",
    ),
    (
        "a cost that is infinite",
        (_row(recorded_usd=math.inf),),
        "by_model[0].recorded_usd",
    ),
    ("a cost that is not a number", (_row(recorded_usd=math.nan),), "by_model"),
    ("a negative count", (_row(input_tokens=-1),), "by_model"),
    ("no model name", (_row(model=""),), "by_model"),
]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [pytest.param(rows, reason, id=name) for name, rows, reason in REFUSED],
)
def test_a_split_the_proto_refuses_is_not_written_and_the_rest_of_the_turn_is(
    rows: tuple[AgentModelUsage, ...], reason: str
) -> None:
    events = _record(_turn(rows))

    data = _usage_event(events)
    assert data.by_model == ()
    # The rest of the turn is still true: its tokens, cost, basis and conversation stay.
    assert (data.input_tokens, data.output_tokens, data.total_cost_usd) == (100, 30, 0.25)
    assert data.cost_basis is CostBasis.PRICE_TABLE
    assert data.provider_session_id == "session-1"
    (diagnostic,) = _diagnostics(events)
    assert "was refused and is not recorded" in diagnostic
    assert reason in diagnostic
    _projects(events)


def test_a_split_the_proto_accepts_is_written_whole_and_without_a_diagnostic() -> None:
    rows = (_row(speed=UsageSpeed.FAST), _row(model="b", primary=False, turns=0, recorded_usd=None))

    events = _record(_turn(rows))

    data = _usage_event(events)
    assert [(row.model, row.primary, row.speed) for row in data.by_model] == [
        ("claude-opus-5-5", True, UsageSpeed.FAST),
        ("b", False, None),
    ]
    assert _diagnostics(events) == []
    _projects(events)


def _any_row() -> st.SearchStrategy[AgentModelUsage]:
    count = st.integers(-2, 60)
    return st.builds(
        AgentModelUsage,
        model=st.sampled_from(["a", "b", "c", ""]),
        input_tokens=count,
        cache_creation_input_tokens=count,
        cache_creation_1h_input_tokens=count,
        cache_read_input_tokens=count,
        output_tokens=count,
        reasoning_output_tokens=count,
        turns=st.integers(-1, 3),
        recorded_usd=st.none() | st.floats(allow_nan=True, allow_infinity=True),
        speed=st.none() | st.sampled_from(UsageSpeed),
        primary=st.booleans(),
    )


@settings(max_examples=examples(pr=150, nightly=3000))
@given(rows=st.lists(_any_row(), max_size=4).map(tuple))
@example(rows=())
@example(rows=(_VALID,))
def test_whatever_split_a_producer_hands_over_the_line_written_projects_and_a_good_split_is_kept(
    rows: tuple[AgentModelUsage, ...],
) -> None:
    """Writing the line never fails and never leaves a line the projector would refuse.

    This is the class property behind the cases above: for any rows at all, valid or not, the
    sink does not raise, the journal line projects through the tee's own path, and the rows are
    kept exactly when the proto accepts them (so no refusal is a guess of this test's own).
    """
    events = _record(_turn(rows))

    data = _usage_event(events)
    _projects(events)
    refused = bool(_diagnostics(events))
    assert refused == (not _proto_accepts(rows))
    assert data.by_model == () if refused else len(data.by_model) == len(rows)


def _proto_accepts(rows: tuple[AgentModelUsage, ...]) -> bool:
    """Whether the ``.proto`` takes these rows, asked directly and with this test's own mapping.

    Counts and costs the unsigned and finite proto fields cannot hold are refused before the
    message exists; the rest is protovalidate's answer.
    """
    for row in rows:
        counts = (
            row.input_tokens,
            row.cache_creation_input_tokens,
            row.cache_creation_1h_input_tokens,
            row.cache_read_input_tokens,
            row.output_tokens,
            row.reasoning_output_tokens,
            row.turns,
        )
        if min(counts) < 0 or not row.model:
            return False
        if row.recorded_usd is not None and not (row.recorded_usd >= 0):
            return False
    detail = p.TurnUsageDetail(
        by_model=[
            p.ModelTurnUsage(
                model=row.model,
                input=row.input_tokens,
                cache_read=row.cache_read_input_tokens,
                cache_creation=row.cache_creation_input_tokens,
                cache_creation_1h=row.cache_creation_1h_input_tokens,
                output=row.output_tokens,
                reasoning=row.reasoning_output_tokens,
                turns=row.turns,
                recorded_usd=row.recorded_usd,
                speed=None if row.speed is None else p.UsageSpeed[row.speed.name],
                primary=row.primary,
            )
            for row in rows
        ]
    )
    try:
        p.validate(detail)
    except p.ValidationError:
        return False
    return True


@pytest.mark.parametrize("rows", [(), (_row(),)], ids=["no split", "one primary"])
def test_a_usage_with_no_basis_records_not_reported_never_null(
    rows: tuple[AgentModelUsage, ...],
) -> None:
    """Null on a stored line means only that it was written before the field existed."""
    events = _record(AgentUsage(input_tokens=5, by_model=rows))

    assert _usage_event(events).cost_basis is CostBasis.NOT_REPORTED
