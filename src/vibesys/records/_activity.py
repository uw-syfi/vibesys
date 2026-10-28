"""A running turn's activity on its span: derived from its tool calls and todos, written sparsely.

The derivation is the server's (``vibesys.turn_activity``); text output is left out, because it
streams many chunks a second and only says whether the agent thinks or answers. A turn writes a
changed activity at once when its span last said one ``ACTIVITY_INTERVAL`` or more ago; a change
inside the interval is held, and the run's first record at least that long after the last write
carries it, still dated when it began. So a run writes at most one activity per turn per interval.

ponytail: a change held just before the whole run falls silent (one agent starting a long command
right after another change) stays unwritten until the run records anything again; a periodic flush
in the journal shell closes that if it proves common.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._agents import owner, turn, update_turn
from vibesys.records._types import ActivityMark
from vibesys.turn_activity import TurnActivity

if TYPE_CHECKING:
    from vibesys.records._common import Projection

ACTIVITY_INTERVAL = 2.0
"""Run-clock seconds between two activity writes of one turn.

An agent takes longer than this to answer a tool result, so each call of a working turn is still
written; a burst of calls (several reads at once) writes once.
"""


def _shown(activity: TurnActivity) -> tuple[str | None, str | None]:
    """What a reader says of an activity: the tool it runs, else its todo."""
    return activity.tool, None if activity.tool is not None else activity.todo


def _derived(activity: TurnActivity, data: object) -> TurnActivity:
    if isinstance(data, e.ToolCallData):
        return activity.called(data.tool)
    if isinstance(data, e.ToolResultData):
        return activity.returned(data.tool)
    if isinstance(data, e.TodoUpdateData):
        return activity.planned(data.todos)
    message = "activity requires a tool call, tool result or todo update"
    raise TypeError(message)


def _ended(span: p.Span | None) -> bool:
    return span is None or span.has_field("ended_at")


def _write(projection: Projection, span_id: str) -> None:
    mark = projection.activities[span_id]
    span = projection.spans[span_id]
    label = span.label
    if label is None or label.kind != "agent_turn" or label.attrs is None:
        return
    attrs = p.AgentTurnAttrs.from_binary(label.attrs.value)
    attrs.activity = p.AgentActivity(
        tool=mark.activity.tool or None, todo=mark.activity.todo or None, since=mark.since
    )
    update_turn(projection, span, attrs)
    projection.activities[span_id] = replace(mark, written_at=projection.clock, held=False)


def flush(projection: Projection) -> None:
    """Write each held activity whose interval has passed; forget turns that ended."""
    for span_id, mark in tuple(projection.activities.items()):
        if _ended(projection.spans.get(span_id)):
            del projection.activities[span_id]
        elif (
            mark.held
            and mark.written_at is not None
            and projection.clock - mark.written_at >= ACTIVITY_INTERVAL
        ):
            _write(projection, span_id)


def observe(projection: Projection) -> None:
    """Fold one tool call, tool result or todo list into its turn's activity."""
    span_id = owner(projection)
    if span_id == "run":
        return
    if span_id not in projection.spans:
        span, attrs = turn(projection)
        update_turn(projection, span, attrs)
    if _ended(projection.spans.get(span_id)):
        return
    mark = projection.activities.get(span_id, ActivityMark(TurnActivity(), projection.clock))
    activity = _derived(mark.activity, projection.event.data)
    if _shown(activity) == _shown(mark.activity):
        projection.activities[span_id] = replace(mark, activity=activity)
        return
    projection.activities[span_id] = ActivityMark(
        activity, projection.clock, mark.written_at, held=True
    )
    if mark.written_at is None or projection.clock - mark.written_at >= ACTIVITY_INTERVAL:
        _write(projection, span_id)
