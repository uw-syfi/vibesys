"""The host helper's ``runs.summary``: a run's run level as one fold of its journal, in pages.

A client opening a run it did not watch needs what the run level shows, not the journal it comes
from (megabytes of tool output for a few kilobytes of spans). ``runs.summary`` returns that fold,
kept beside the journal (``server.run_summary``), so the cost of opening a run is the size of its
summary and not of its history.

Light: Pydantic, the standard library and ``server.host_envelope``. ``server.host_api`` imports
these messages into the helper's request and reply unions and gates them behind
``HelperFeature.RUN_SUMMARY``.

Public surface: ``RunsSummaryRequest``, ``RunsSummaryReply``, ``TurnReply``, the documents they
pass through (``RunSpanDocument``, ``RunOverviewDocument``, ``ReplySchemaDocument``) and the
limits and names ``RUN_SUMMARY_FILE``, ``RUN_SUMMARY_PAGE_MAX_BYTES`` and
``REPLY_SCHEMA_ID_PATTERN``.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
)

from server.host_envelope import HelperReplyBase, HelperRequestBase, RunId

RUN_SUMMARY_FILE: Final = "run-summary.json"
"""The run's summary read model, beside its journal: a disposable cache of the journal's fold."""

RUN_SUMMARY_PAGE_MAX_BYTES: Final = 512 * 1024
"""The most bytes of spans, replies and schemas one ``runs.summary`` page carries, so it stays a
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
    """The index of the first item to return, counting the spans first and then the replies."""


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
    span_count: NonNegativeInt
    """Every span of the run, across all pages."""
    reply_count: NonNegativeInt
    """Every reply of the run, across all pages."""
    overview: RunOverviewDocument | None
    """On every page, so a page stands alone; ``None`` before a ``run_started`` declared one."""
    spans: tuple[RunSpanDocument, ...]
    """This page's spans, from index ``offset`` (a span's index is its place in the run trace)."""
    replies: tuple[TurnReply, ...]
    """This page's replies, from index ``max(0, offset - span_count)`` in the run's replies."""
    schemas: dict[str, ReplySchemaDocument]
    """The schemas of this page's replies that no earlier reply uses, by ``TurnReply.schema_id``."""
    next_offset: PositiveInt | None
    """The ``offset`` of the next page, or ``None`` when this page ends the summary."""
