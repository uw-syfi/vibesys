"""The host helper's ``runs.summary``: a run's run level as one fold of its journal, in pages.

A client opening a run it did not watch needs what the run level shows, not the journal it comes
from (megabytes of tool output for a few kilobytes of spans). ``runs.summary`` returns that fold,
kept beside the journal (``server.run_summary``), so the cost of opening a run is the size of its
summary and not of its history.

Light: Pydantic, the standard library and ``server.host_envelope``. ``server.host_api`` imports
these messages into the helper's request and reply unions and gates them behind
``HelperFeature.RUN_SUMMARY``.

Public surface: ``RunsSummaryRequest``, ``RunsSummaryReply``, ``TurnReply``, ``TokenTally``,
``SpanTokens``, ``UsageRecord``, ``TraceFormat``, ``trace_format``, the documents they
pass through (``RunSpanDocument``, ``RunOverviewDocument``, ``ReplySchemaDocument``) and the
limits and names ``RUN_SUMMARY_FILE``, ``RUN_SUMMARY_PAGE_MAX_BYTES`` and
``REPLY_SCHEMA_ID_PATTERN``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    JsonValue,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
)

from server.host_envelope import HelperReplyBase, HelperRequestBase, RunId

RUN_SUMMARY_FILE: Final = "run-summary.json"
"""The run's summary read model, beside its journal: a disposable cache of the journal's fold."""

RUN_SUMMARY_PAGE_MAX_BYTES: Final = 512 * 1024
"""The most bytes of spans, replies, schemas and usage one ``runs.summary`` page carries, so it stays a
helper line; a page always holds at least one item, so every read makes progress."""

REPLY_SCHEMA_ID_PATTERN: Final = r"^[0-9a-f]{64}$"
"""A reply schema's identity: the SHA-256 of its canonical JSON (sorted keys, compact)."""

RunSpanDocument = dict[str, JsonValue]
"""One span of the run trace as JSON: the run protocol's ``Span`` (``WireSpan``), which a client
narrows with its generated type. The helper passes it through so it never imports the protocol."""

RunOverviewDocument = dict[str, JsonValue]
"""The run's overview as JSON: the run protocol's ``RunOverview``, passed through as spans are."""

ReplySchemaDocument = dict[str, JsonValue]
"""The JSON Schema of an agent reply model, as the journal recorded it."""


class TraceFormat(StrEnum):
    """Whether a journal records the run trace, the spans a run's ideas and measurements are read from."""

    RECORDED = "recorded"
    """The journal holds span events, or no round finished, so it has no trace to miss."""
    PREDATES_TRACE = "predates_trace"
    """Rounds finished and no span event was ever recorded: a VibeSys from before the run trace
    wrote it. It holds round events (a round's verdict, a reading when one was taken), not ideas
    or measurements, and none can be read from it."""


def trace_format(*, spans: int, rounds: int) -> TraceFormat:
    """The format of a journal in which ``spans`` span events and ``rounds`` finished rounds were seen.

    The one rule both readers of a journal (the summary and the digest) apply, so a list row and
    the run it opens never disagree. A run records each round's end together with its workstream
    span, so a journal with finished rounds and no span event is one the trace did not exist for.
    """
    return TraceFormat.PREDATES_TRACE if rounds > 0 and spans == 0 else TraceFormat.RECORDED


class RunsSummaryRequest(HelperRequestBase):
    """One page of a run's summary: what its run level shows, as of the journal's end now.

    The summary is the journal's fold the client would compute itself (see
    ``RunsSummaryReply``), kept beside the journal so a read costs what the summary holds, not
    what the journal does. Its items (every span, then every reply) are read in pages: start at
    ``offset`` 0 and continue at each page's ``next_offset`` until it is ``None``.
    """

    type: Literal["runs.summary"] = "runs.summary"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    run_id: RunId
    offset: NonNegativeInt = 0
    """The index of the first item to return, counting spans, then replies, usage, then records."""


class TurnReply(BaseModel):
    """The typed reply an agent turn ended with, its schema named by ``schema_id``.

    ``kind``, the schema and ``value`` are the three fields of the run protocol's ``AgentReply``;
    the schema is carried once per page in ``schemas`` because it is the repeated, large part.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    """The agent-turn span the turn ran as: the event's ``span_id``."""
    kind: str
    """The reply model's name."""
    schema_id: Annotated[str, StringConstraints(pattern=REPLY_SCHEMA_ID_PATTERN)]
    """The key of the reply model's JSON Schema in a page's ``schemas``."""
    value: dict[str, JsonValue]
    """The validated reply."""


class TokenTally(BaseModel):
    """Tokens spent, counted as the run protocol's ``turn_usage_recorded`` reports them.

    A count the provider did not report is ``None``, not zero: a sum is ``None`` until some turn
    reports the count, and then adds only the turns that did, so a reader can tell "none" from
    "zero" and a sum over turns with a missing count is a lower bound.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: NonNegativeInt | None
    """Input tokens, cached ones included."""
    cache_read: NonNegativeInt | None
    """The part of ``input`` the provider served from its cache."""
    cache_write: NonNegativeInt | None
    """The part of ``input`` written to the provider's cache."""
    output: NonNegativeInt | None
    """Output tokens, reasoning included."""
    reasoning: NonNegativeInt | None
    """The part of ``output`` the provider reports as reasoning."""


class SpanTokens(BaseModel):
    """The tokens the turns of one agent-turn span spent, summed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    """The agent-turn span the turns ran as: the usage event's ``span_id``."""
    tally: TokenTally


class UsageRecord(BaseModel):
    """What one agent turn spent, and who and when it was: the one definition of a usage record.

    A projection of the journal (``server.run_usage``): a ``turn_usage_recorded`` event joined to
    the ``agent_execution_started`` of its ``execution_id``. A value the journal does not hold is
    ``None``, never a guess: a turn whose execution start is missing has no role, harness, model,
    effort, version or start. The run is a field so records of many runs fold together without
    changing the fold. ``role`` and ``harness`` are open sets (the roles of a run's flow, the
    providers agentshim runs) owned elsewhere, so this contract names no list of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str | None
    """The run the turn belongs to: the usage event's ``run_id``."""
    execution_id: str | None
    """The agent execution the turn ran as; the key joining the usage to its start."""
    span_id: str | None
    """The agent-turn span of the run trace the turn ran as, when the journal names it. The idea
    the turn served is read from the trace by this span or by ``execution_id``."""
    round_label: str | None
    """The label the journal gave the turn's round, as recorded."""
    role: str | None
    """The agent's role (``agent_kind``), from the execution's start."""
    harness: str | None
    """The provider CLI that ran the turn (for example ``codex``), from the execution's start."""
    model: str | None
    reasoning_effort: str | None
    """The effort the session was configured with; ``None``: the provider's default or unknown."""
    harness_version: str | None
    started_at: AwareDatetime | None
    """When the execution started."""
    ended_at: AwareDatetime | None
    """When the turn's usage was recorded, which is when the turn ended."""
    tokens: TokenTally
    cost_usd: NonNegativeFloat | None
    duration_ms: NonNegativeInt | None


class RunsSummaryReply(HelperReplyBase):
    """One page of a run's summary: the client's fold of its journal, to ``through_sequence``.

    Every field equals what a client reading the whole journal up to ``through_sequence`` folds
    itself, which is the contract the helper's tests hold it to:

    - ``spans``: the run trace (``foldTrace``): each span as its latest span event left it, in
      the order the spans started, so each comes after its parent;
    - ``overview``: the ``overview`` of the last ``run_started`` event that declares one
      (``foldOverview``), with no unit or direction a live server adds from run state;
    - ``replies``: the last typed reply of each agent-turn span, by ``agent_execution_finished``
      events with a ``span_id``, in the order the spans first replied (``implementerOutcomes``);
    - ``tokens`` and ``usage``: the run's spend, folded from ``turn_usage_recorded`` events by
      summing (``ledgerWith``): ``tokens`` over every turn, those the trace names and those it
      does not, ``usage`` over each turn span that is named, in the order the spans first spent;
    - ``records``: one ``UsageRecord`` per ``turn_usage_recorded`` event, in journal order, whose
      tokens sum to ``tokens`` (``server.run_usage``);
    - the idea rows, the measurement points and the chart are folds of ``spans`` and ``replies``
      the client already owns (``CandidateFold``, ``performanceFold``); nothing duplicates them.

    An event at or before the last folded sequence is skipped whole, as the client skips it. The
    run level is final at ``through_sequence``. Events after it are the live tail: read them with
    ``runs.read_events`` from ``through_sequence + 1``, or subscribe after ``through_sequence``. A
    run still writing can move between the pages of one read: every page names its
    ``through_sequence``, and a read whose pages disagree starts again.
    """

    type: Literal["runs.summary"] = "runs.summary"
    through_sequence: NonNegativeInt
    """The sequence of the last event folded; 0 when the journal holds no event."""
    event_count: NonNegativeInt
    """The events folded: the journal's events, counted as ``runs.read_events`` would return them."""
    journal_bytes: NonNegativeInt
    """The journal bytes those events occupy, for progress and for sizing the full history."""
    trace: TraceFormat
    """Whether the journal records a run trace. ``predates_trace``: ``spans`` is empty because the
    journal predates it, not because the run tried nothing; ``rounds_finished`` is what it says."""
    rounds_finished: NonNegativeInt
    """The ``round_finished`` events folded: the rounds the journal records as having ended."""
    span_count: NonNegativeInt
    """Every span of the run, across all pages."""
    reply_count: NonNegativeInt
    """Every reply of the run, across all pages."""
    usage_count: NonNegativeInt
    """Every span with a spend, across all pages."""
    record_count: NonNegativeInt
    """Every usage record of the run, across all pages."""
    tokens: TokenTally
    """The whole run's spend, on every page so a page stands alone."""
    overview: RunOverviewDocument | None
    """On every page, so a page stands alone; ``None`` before a ``run_started`` declared one."""
    spans: tuple[RunSpanDocument, ...]
    """This page's spans, from index ``offset`` (a span's index is its place in the run trace)."""
    replies: tuple[TurnReply, ...]
    """This page's replies, from index ``max(0, offset - span_count)`` in the run's replies."""
    schemas: dict[str, ReplySchemaDocument]
    """The schemas of this page's replies that no earlier reply uses, by ``TurnReply.schema_id``."""
    usage: tuple[SpanTokens, ...]
    """This page's per-span spend, from index ``max(0, offset - span_count - reply_count)``."""
    records: tuple[UsageRecord, ...]
    """This page's usage records, from index ``max(0, offset`` minus every earlier item``)``."""
    next_offset: PositiveInt | None
    """The ``offset`` of the next page, or ``None`` when this page ends the summary."""
