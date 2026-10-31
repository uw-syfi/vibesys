"""The host helper's ``runs.summary``: a run's run level as one fold of its journal, in pages.

A client opening a run it did not watch needs what the run level shows, not the journal it comes
from (megabytes of tool output for a few kilobytes of spans). ``runs.summary`` returns that fold,
kept beside the journal (``server.run_summary``), so the cost of opening a run is the size of its
summary and not of its history.

The same module holds the ``usage.window`` messages (see "The usage window" below): they read the
usage records defined here and must load with the helper's other messages, because
``server.host_api`` names them in its unions, while their computation (``server.usage_window``)
loads on the first ``usage.window`` request.

Light: Pydantic, the standard library and ``server.host_envelope``. ``server.host_api`` imports
these messages into the helper's request and reply unions and gates them behind
``HelperFeature.RUN_SUMMARY`` (and ``usage.window`` behind ``HelperFeature.USAGE_WINDOW``).

Public surface: ``RunsSummaryRequest``, ``RunsSummaryReply``, ``TurnReply``, ``TokenTally``,
``SpanTokens``, ``UsageRecord``, ``ModelUsage``, ``RecordedCostBasis``, ``NO_TOKENS``, ``add_tokens``, ``InputGate``, ``InputGates``, ``RunTerminal``,
``RunTerminalKind``, ``TraceFormat``, ``trace_format``, the documents they
pass through (``RunSpanDocument``, ``RunOverviewDocument``, ``ReplySchemaDocument``) and the
limits and names ``RUN_SUMMARY_FILE``, ``RUN_SUMMARY_PAGE_MAX_BYTES``, ``INPUT_GATES_MAX``,
``INPUT_GATE_TAIL_MAX_CHARS``, ``RUN_TERMINAL_MESSAGE_MAX_CHARS`` and
``REPLY_SCHEMA_ID_PATTERN``; and the usage window's ``UsageWindowRequest``, ``UsageWindowReply``,
``RunSpend``, ``AgentSpend`` and the limits ``USAGE_WINDOW_MAX_RUNS``, ``USAGE_WINDOW_MAX_AGENTS``,
``USAGE_WINDOW_MAX_HOURS`` and ``RATE_WINDOW_S``.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from server.host_envelope import HelperReplyBase, HelperRequestBase, RunId

RUN_SUMMARY_FILE: Final = "run-summary.json"
"""The run's summary read model, beside its journal: a disposable cache of the journal's fold."""

RUN_SUMMARY_PAGE_MAX_BYTES: Final = 512 * 1024
"""The most bytes of spans, replies, schemas and usage one ``runs.summary`` page carries, so it stays a
helper line; a page always holds at least one item, so every read makes progress."""

INPUT_GATES_MAX: Final = 32
"""The most input gate outcomes a summary carries; a run has a handful (build, accuracy, benchmark,
one per validation recipe), so the bound only guards a journal that records a gate in a loop."""

INPUT_GATE_TAIL_MAX_CHARS: Final = 4000
"""The most characters of a gate's output a summary keeps, the end of it: where the error is."""

RUN_TERMINAL_MESSAGE_MAX_CHARS: Final = 16000
"""The most characters of a run's failure message a summary carries. A message is a few lines
(an exception and its cause), so this is far above any real one; a longer one ends in an ellipsis."""

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


class InputGate(BaseModel):
    """One gate that finished before the run's first idea began: how the run's input measured.

    A projection of the journal's ``gate_finished`` events that come before the first workstream
    span (a gate under an idea belongs to the idea, and its span carries it), and a pass of a gate
    that stood failed after it (the input's gate tried again). The client reads a
    gate's short reason from ``output_tail`` itself, as it does for a journal it folds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: NonNegativeInt
    """The event's sequence: a view of the run at an earlier sequence does not hold the gate yet."""
    gate: str
    """The gate's kind (``build``, ``accuracy``, ``benchmark``, ``validation``), an open set owned
    by the run protocol."""
    recipe: str | None
    """The validation recipe the gate ran, else ``None``."""
    passed: bool
    output_tail: Annotated[str, StringConstraints(max_length=INPUT_GATE_TAIL_MAX_CHARS)] | None
    """The end of what the gate printed, credentials redacted; ``None`` when it kept nothing."""


InputGates = Annotated[tuple[InputGate, ...], Field(max_length=INPUT_GATES_MAX)]
"""A summary's input gates, at most ``INPUT_GATES_MAX``."""


class RunTerminalKind(StrEnum):
    """Which terminal event ended a run's journal."""

    FINISHED = "finished"
    """``run_finished``: the run ran to its end."""
    FAILED = "failed"
    """``run_failed``: the run failed."""
    INTERRUPTED = "interrupted"
    """``run_interrupted``: the run was stopped before it ended."""


class RunTerminal(BaseModel):
    """How the run's journal ended: the last terminal event it records, and what it said."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: NonNegativeInt
    kind: RunTerminalKind
    message: Annotated[str, StringConstraints(max_length=RUN_TERMINAL_MESSAGE_MAX_CHARS)] | None
    """The event's whole account of why, not cut to a line; ``None`` when it said nothing."""


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
    with_ending: bool | None = None
    """Ask for ``input_gates`` and ``terminal``. Sent only after ``run_ending``."""


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


NO_TOKENS: Final = TokenTally(
    input=None, cache_read=None, cache_write=None, output=None, reasoning=None
)
"""The tally of nothing spent: no count is reported yet."""


def _sum(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    return a if b is None else a + b


def add_tokens(a: TokenTally, b: TokenTally) -> TokenTally:
    """``a`` and ``b`` summed count by count; a count neither reported stays unreported."""
    return TokenTally(
        input=_sum(a.input, b.input),
        cache_read=_sum(a.cache_read, b.cache_read),
        cache_write=_sum(a.cache_write, b.cache_write),
        output=_sum(a.output, b.output),
        reasoning=_sum(a.reasoning, b.reasoning),
    )


class SpanTokens(BaseModel):
    """The tokens the turns of one agent-turn span spent, summed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str
    """The agent-turn span the turns ran as: the usage event's ``span_id``."""
    tally: TokenTally


RecordedCostBasis = Literal["price_table", "provider_reported", "unknown_model"]
"""How a turn's cost was reached, as the journal records it: ``vs_agent.contracts.CostBasis``'s
values and the first three of ``vibesys.store.v1.UsageCostBasis``. The proto's last two are never
recorded; ``server.run_usage.basis_of`` derives them."""

UsageSpeedName = Literal["standard", "fast", "ultrafast"]
"""The service speed a request ran at: ``vibesys.store.v1.UsageSpeed`` without its unreported."""


class ModelUsage(BaseModel):
    """One model's share of one finished turn: the journal's ``by_model`` row of a usage record.

    The counts are always known: a row exists only for a turn whose increment was measured.
    ``recorded_usd`` is the price agentshim recorded for these tokens at the standard tier, ``None``
    when it has no price for the model; it is not a bill, and nothing here recomputes it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    """The model id as the provider reported it."""
    input: NonNegativeInt
    """Input tokens, cached ones included."""
    cache_read: NonNegativeInt
    cache_write: NonNegativeInt
    cache_write_1h: NonNegativeInt
    """The part of ``cache_write`` written with a one-hour lifetime."""
    output: NonNegativeInt
    """Output tokens, reasoning included."""
    reasoning: NonNegativeInt
    turns: NonNegativeInt
    """The turn's requests. Only the primary row counts them, so summing rows counts each once."""
    recorded_usd: NonNegativeFloat | None
    speed: UsageSpeedName | None
    """``None``: the provider reported none, which reads as standard."""
    primary: bool
    """The model the turn was run with. Exactly one row of a turn is."""


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
    client: Literal["scripted", "provider"] | None = None
    """``scripted`` when a scripted client ran the turn. The harness and model stay the selection."""
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
    cost_basis: RecordedCostBasis | None = None
    """How ``cost_usd`` was reached; ``None``: the turn reported no cost, or the journal line
    predates the field (``server.run_usage.basis_of`` tells the two apart by the cost)."""
    by_model: tuple[ModelUsage, ...] = ()
    """Each model the turn ran. ``tokens`` counts the main conversation only, so these rows are a
    superset of it; empty when the provider reports no split, or the line predates the field."""
    provider_session_id: str | None = None
    """The provider's id of the conversation the turn ran in; ``None``: it named none, or the
    line predates the field."""


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
    - ``input_gates``: the ``gate_finished`` events before the first ``span_started`` of a
      workstream span, and after it the passes of a gate that stood failed (a retry of the input's
      gate: ``foldInputGates``), a client's reading of how the run's input measured;
    - ``terminal``: the last ``run_finished``, ``run_failed`` or ``run_interrupted`` event;
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
    input_gates: Annotated[InputGates | None, Field(exclude_if=lambda gates: gates is None)] = None
    """The gates that finished before the first idea began, in journal order, on every page; only
    with ``with_ending``."""
    terminal: Annotated[RunTerminal | None, Field(exclude_if=lambda ended: ended is None)] = None
    """The last terminal event of the journal, on every page; only with ``with_ending``, and absent
    while the run has not ended (or crashed without writing one), which a client then reads from
    the run's registry record."""
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


# The usage window

USAGE_WINDOW_MAX_RUNS: Final = 100
"""The most runs one request names (the size of a ``runs.list`` digest page), so the work is bounded."""
USAGE_WINDOW_MAX_AGENTS: Final = 64
"""The most agents (a role, harness and model) one reply lists; the costliest come first."""
USAGE_WINDOW_MAX_HOURS: Final = 336
"""The longest window, in hours: two weeks."""
RATE_WINDOW_S: Final = 3600
"""The trailing seconds, ending at the window's end, that ``spend_per_hour_usd`` is read over."""


class UsageWindowRequest(HelperRequestBase):
    """What the named runs of a project spent in ``[since, until)``.

    ``until`` is "now" for the reply: the helper reads no clock for it, so a client names the time it
    is asking at (or the end of a past window) and the answer is a function of the journals and
    these two instants. A turn is placed at the time it ended (``UsageRecord.ended_at``).
    """

    type: Literal["usage.window"] = "usage.window"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    run_ids: Annotated[tuple[RunId, ...], Field(max_length=USAGE_WINDOW_MAX_RUNS)]
    """The runs to count, as ``runs.list`` names them; a run the project does not have is reported
    in the reply's ``missing_runs``. A repeated id counts once."""
    since: AwareDatetime
    until: AwareDatetime

    @model_validator(mode="after")
    def _window_is_bounded(self) -> Self:
        if not self.since < self.until:
            message = "since must be before until"
            raise ValueError(message)
        if self.until - self.since > timedelta(hours=USAGE_WINDOW_MAX_HOURS):
            message = f"the window may span at most {USAGE_WINDOW_MAX_HOURS} hours"
            raise ValueError(message)
        return self


class RunSpend(BaseModel):
    """What one run spent in the window; only runs that had a turn end in it are listed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    task: str | None
    """The task the run was started for, as ``runs.list`` names it; ``None`` when the run records none."""
    cost_usd: NonNegativeFloat
    """The reported cost of the run's turns that ended in the window."""
    unpriced_turns: NonNegativeInt = 0
    """The run's turns in the window that reported no cost (a helper that predates it sends none)."""


class AgentSpend(BaseModel):
    """What one kind of agent spent in the window: a role on a harness and model.

    A part the journal did not record for a turn is ``None``, so the turns of an execution whose
    start is missing form their own entry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str | None
    harness: str | None
    model: str | None
    cost_usd: NonNegativeFloat
    turns: NonNegativeInt
    """The turns, priced or not, that ended in the window."""
    unpriced_turns: NonNegativeInt = 0
    """Those of ``turns`` that reported no cost (a helper that predates it sends none)."""


class UsageWindowReply(HelperReplyBase):
    """What the Usage page shows for one window.

    Costs are the sums of the turns' reported ``cost_usd`` (a turn that reported none counts in
    ``unpriced_turns`` and adds nothing, so a total is a lower bound while it is not zero). A turn
    with no recorded end time cannot be placed in a window: it counts in ``untimed_turns`` and
    nowhere else. ``new_bests`` counts the ideas kept (``IdeaVerdict.KEPT``) whose result landed in
    the window, and ``cost_per_new_best_usd`` is ``spent_usd`` over it, ``None`` when there is none.
    A run's kept ideas beyond ``DASHBOARD_MAX_POINTS`` are not counted.
    """

    type: Literal["usage.window"] = "usage.window"
    since: AwareDatetime
    until: AwareDatetime
    spent_usd: NonNegativeFloat
    spend_per_hour_usd: NonNegativeFloat
    """The cost of the turns that ended in the last ``min(RATE_WINDOW_S, window)`` seconds before
    ``until``, per hour of that span."""
    tokens: TokenTally
    """The tokens of the turns in the window, summed as a run's are."""
    turns: NonNegativeInt
    unpriced_turns: NonNegativeInt
    untimed_turns: NonNegativeInt
    new_bests: NonNegativeInt
    cost_per_new_best_usd: NonNegativeFloat | None
    by_run: Annotated[tuple[RunSpend, ...], Field(max_length=USAGE_WINDOW_MAX_RUNS)]
    """The costliest run first (ties by run id)."""
    by_agent: Annotated[tuple[AgentSpend, ...], Field(max_length=USAGE_WINDOW_MAX_AGENTS)]
    """The costliest agent first (ties by role, harness, model)."""
    agents_omitted: NonNegativeInt
    """The agents that spent in the window beyond ``USAGE_WINDOW_MAX_AGENTS``."""
    missing_runs: Annotated[tuple[RunId, ...], Field(max_length=USAGE_WINDOW_MAX_RUNS)]
    """The requested runs the project does not have, in the order asked."""
