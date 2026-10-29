"""Flows: a search written as straight-line async code whose effects are journaled and replayed.

The vocabulary and the rules for flow code are in ``docs/contributing/flows.md``. In short, a
search is ``@search(name, config=Model)`` over ``async def root(ctx, config)``. The root and the
flows it starts receive ``ctx: Flow``: they ask agents (``ctx.agents``), run candidates in
workspaces (``ctx.workspaces``), measure them (``ctx.evaluation``), run concurrent tasks in
groups (``ctx.group``), wait on sources (``ctx.first``) and name their winner (``ctx.choose``).
They never touch the clock, I/O or randomness, and they never finish the run: when the root
returns, the framework adopts the last choice and ends the run.

There is no asyncio. Awaiting suspends the coroutine; the driver resumes exactly the flows whose
sources an incoming event filled, until every flow is suspended again. The decisions issued along
the way are the output of that step, in issue order. Persistence is replay (see ``_define``).

The files: ``_define`` is ``@search`` and the driver; ``_defs`` registers flows; ``_flow`` is
``Flow`` and ``Group``; ``_kit`` is the handle core's own aspects use; ``_agents``,
``_workspaces``, ``_evaluation``, ``_owners`` and ``_winner`` are the aspects and the ending; ``_roles`` is
the role declaration; ``_run`` drives the coroutines; ``_sources`` is what a flow waits on;
``_resource`` is ``@resource``; ``_retry`` is the retry values; ``_state`` is the persisted state;
``_spans`` is the run trace's read model and ``_trace`` records it; ``_labels`` is the
search's span kinds and labels.
"""

from ._agents import Agents, Parking, Session
from ._define import FlowDriver, SearchDef, search
from ._defs import flow
from ._errors import Deadlock, FlowCodeChanged, Stopped, TaskStopped, TimeUp
from ._evaluation import CaptureSpec, Compared, Evaluation
from ._flow import Clock, Flow, Group, RunTask
from ._labels import Label, LabelFields, SpanKind
from ._owners import Owners
from ._resource import resource
from ._retry import Again, GaveUp, RetryPolicy, Tries
from ._roles import (
    MARKDOWN_PROSE_CONVENTION,
    MAX_CORRECTIONS,
    MAX_LOST_TURNS,
    MAX_RENDER_SECONDS,
    MAX_TURN_SECONDS,
    PLAIN_LINE_CONVENTION,
    RevisionReply,
    Role,
    UnknownRoleError,
    hours,
    markdown_prose,
    minutes,
    plain_line,
    reply_errors,
)
from ._sources import (
    Channel,
    ChannelKey,
    Latch,
    LatchKey,
    Pending,
    Source,
    StopSource,
    Task,
    Timer,
)
from ._spans import (
    AttemptOutcome,
    FailureKind,
    RunTrace,
    Span,
    SpanAttempt,
    SpanChange,
    SpanFailure,
    SpanId,
    SpanLabel,
    SpanLink,
    SpanStatus,
    SpanStop,
    SpanType,
    SpanWaitKind,
    WaitingOn,
    WireSpan,
    span_changes,
)
from ._state import (
    Dropped,
    FlowState,
    JournaledEvent,
    RestartSettled,
    RestartSettleReason,
    Waiting,
)
from ._trace import operation_span
from ._winner import ADOPTED, INPUT, NOT_ADOPTED, EndReason, Input, Kept, NoWinner, Uncompared
from ._workspaces import Assessment, NotOpened, NotReopened, Workspace, Workspaces

__all__ = [
    "ADOPTED",
    "INPUT",
    "MARKDOWN_PROSE_CONVENTION",
    "MAX_CORRECTIONS",
    "MAX_LOST_TURNS",
    "MAX_RENDER_SECONDS",
    "MAX_TURN_SECONDS",
    "NOT_ADOPTED",
    "PLAIN_LINE_CONVENTION",
    "Again",
    "Agents",
    "Assessment",
    "AttemptOutcome",
    "CaptureSpec",
    "Channel",
    "ChannelKey",
    "Clock",
    "Compared",
    "Deadlock",
    "Dropped",
    "EndReason",
    "Evaluation",
    "FailureKind",
    "Flow",
    "FlowCodeChanged",
    "FlowDriver",
    "FlowState",
    "GaveUp",
    "Group",
    "Input",
    "JournaledEvent",
    "Kept",
    "Label",
    "LabelFields",
    "Latch",
    "LatchKey",
    "NoWinner",
    "NotOpened",
    "NotReopened",
    "Owners",
    "Parking",
    "Pending",
    "RestartSettleReason",
    "RestartSettled",
    "RetryPolicy",
    "RevisionReply",
    "Role",
    "RunTask",
    "RunTrace",
    "SearchDef",
    "Session",
    "Source",
    "Span",
    "SpanAttempt",
    "SpanChange",
    "SpanFailure",
    "SpanId",
    "SpanKind",
    "SpanLabel",
    "SpanLink",
    "SpanStatus",
    "SpanStop",
    "SpanType",
    "SpanWaitKind",
    "StopSource",
    "Stopped",
    "Task",
    "TaskStopped",
    "TimeUp",
    "Timer",
    "Tries",
    "Uncompared",
    "UnknownRoleError",
    "Waiting",
    "WaitingOn",
    "WireSpan",
    "Workspace",
    "Workspaces",
    "flow",
    "hours",
    "markdown_prose",
    "minutes",
    "operation_span",
    "plain_line",
    "reply_errors",
    "resource",
    "search",
    "span_changes",
]
