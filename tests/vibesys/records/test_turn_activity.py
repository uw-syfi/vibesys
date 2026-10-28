"""A running turn's activity on its span: what it is doing, when that began, written sparsely."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from vibesys.api import TurnActivity
from vibesys.records.api import ACTIVITY_INTERVAL, MappingState, map_event

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_TURN = "turn:turn-a"
_TOOLS = ("Read", "Edit", "Bash")

_STEP = st.tuples(
    st.integers(min_value=0, max_value=20).map(lambda quarters: quarters / 4),
    st.one_of(
        st.tuples(st.just("call"), st.sampled_from(_TOOLS)),
        st.tuples(st.just("result"), st.sampled_from(_TOOLS)),
        st.tuples(st.just("todo"), st.sampled_from((None, "Profile the queue", "Pad the ring"))),
    ),
)

Shown = tuple[str | None, str | None]


def _event(
    kind: e.CoreEventType, data: e.CoreEventData | None, at: float, sequence: int
) -> e.CoreEvent:
    return e.CoreEvent.model_validate(
        {
            "type": kind,
            "data": data,
            "timestamp": _AT + timedelta(seconds=at),
            "run_id": "run-a",
            "sequence": sequence,
            "execution_id": "turn-a",
            "agent_kind": "implementer",
        }
    )


def _payload(action: tuple[str, str | None]) -> tuple[e.CoreEventType, e.CoreEventData]:
    verb, value = action
    if verb == "call":
        return e.CoreEventType.TOOL_CALL, e.ToolCallData(tool=value or "", call_id=None)
    if verb == "result":
        return e.CoreEventType.TOOL_RESULT, e.ToolResultData(tool=value or "", content="ok")
    todos = [] if value is None else [e.TodoItemData(content=value, status="in_progress")]
    return e.CoreEventType.TODO_UPDATE, e.TodoUpdateData(todos=todos)


def _shown(activity: TurnActivity) -> Shown:
    return activity.tool, None if activity.tool is not None else activity.todo


def _started() -> e.CoreEvent:
    return _event(
        e.CoreEventType.AGENT_EXECUTION_STARTED,
        e.AgentExecutionStartedData(
            stage="implementer",
            activity=e.AgentExecutionActivityData(mode="thinking", summary="working"),
        ),
        0,
        1,
    )


def _field(activity: p.AgentActivity, name: str) -> str | None:
    return getattr(activity, name) if activity.has_field(name) else None


def _said(activity: p.AgentActivity) -> Shown:
    """What a reader says of a written activity: the tool it runs, else its todo."""
    tool = _field(activity, "tool")
    return tool, _field(activity, "todo") if tool is None else None


def _writes(records: list[p.Record]) -> list[tuple[float, Shown, float]]:
    """Each change of the turn span's activity: the record's clock, what it says, its start."""
    writes: list[tuple[float, Shown, float]] = []
    said: p.AgentActivity | None = None
    for record in records:
        body = record.body
        if body is None or body.field != "span" or body.value.span.id != _TURN:
            continue
        label = body.value.span.label
        attrs = p.AgentTurnAttrs.from_binary(label.attrs.value)
        if attrs.activity is not None and attrs.activity != said:
            said = attrs.activity
            writes.append((record.clock, _said(said), said.since))
    return writes


@given(steps=st.lists(_STEP, max_size=30))
# Regression: a held change was written by the next call's record, which then held that call.
@example(
    steps=[
        (0, ("todo", "Pad the ring")),
        (2, ("call", "Read")),
        (1, ("result", "Read")),
        (4, ("call", "Edit")),
    ]
)
def test_a_turn_says_its_newest_activity_at_most_once_per_interval(
    steps: list[tuple[float, tuple[str, str | None]]],
) -> None:
    state = MappingState("run-a")
    records: list[p.Record] = []
    result = map_event(state, _started())
    state, records = result.state, [*records, *result.records]
    at = 0.0
    model = TurnActivity()
    changes: list[tuple[float, Shown]] = []
    for sequence, (pause, action) in enumerate(steps, start=2):
        at += pause
        kind, data = _payload(action)
        before = _shown(model)
        if isinstance(data, e.ToolCallData):
            model = model.called(data.tool)
        elif isinstance(data, e.ToolResultData):
            model = model.returned(data.tool)
        elif isinstance(data, e.TodoUpdateData):
            model = model.planned(data.todos)
        if _shown(model) != before:
            changes.append((at, _shown(model)))
        before_writes = _writes(records)
        result = map_event(state, _event(kind, data, at, sequence))
        state, records = result.state, [*records, *result.records]
        # A record of the turn an interval or more after the span last said an activity leaves
        # it saying the newest one: a change is held only inside the interval, never behind it.
        said = _writes(records)
        if changes and (not before_writes or at - before_writes[-1][0] >= ACTIVITY_INTERVAL):
            assert said[-1][1] == _shown(model)
    # Any later record of the run, past the interval, carries a change still held.
    later = _event(
        e.CoreEventType.USAGE_UPDATE,
        e.UsageUpdateData(input_tokens=1),
        at + ACTIVITY_INTERVAL,
        len(steps) + 2,
    )
    records = [*records, *map_event(state, later).records]

    writes = _writes(records)
    clocks = [clock for clock, _, _ in writes]
    assert all(b - a >= ACTIVITY_INTERVAL for a, b in pairwise(clocks))
    for clock, shown, since in writes:
        assert since <= clock
        assert (since, shown) in changes
    if changes:
        last, shown = changes[-1]
        assert writes[-1][1:] == (shown, last)
    else:
        assert writes == []


def test_a_held_activity_is_never_written_on_a_turn_that_ended() -> None:
    state = MappingState("run-a")
    events = [
        _started(),
        _event(e.CoreEventType.TOOL_CALL, e.ToolCallData(tool="Read"), 1, 2),
        _event(e.CoreEventType.TOOL_CALL, e.ToolCallData(tool="Bash"), 1.5, 3),
        _event(
            e.CoreEventType.AGENT_EXECUTION_FINISHED,
            e.AgentExecutionFinishedData(result="done"),
            2,
            4,
        ),
        _event(e.CoreEventType.USAGE_UPDATE, e.UsageUpdateData(input_tokens=1), 9, 5),
    ]
    records: list[p.Record] = []
    for observation in events:
        result = map_event(state, observation)
        state, records = result.state, [*records, *result.records]
    assert [shown for _, shown, _ in _writes(records)] == [("Read", None)]
    assert state.activities == ()
