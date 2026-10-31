"""A run's summary: the fold of its journal that the run level shows, kept up to date as it grows.

``RunSummaryFold`` reads journal lines in order and keeps what a client reading the whole journal
would fold for the run level: the run trace (every span as its latest span event left it), the
overview a ``run_started`` event declares, the last typed reply of each agent turn, and the usage
record of each finished turn (``server.run_usage``), the input gates (the gates that finished before
the first idea began) and the terminal event that ended the journal. It is a pure read model over documents: it
opens no file and reads no clock, so a property test can feed it every prefix of a journal. ``server.host_helper_os`` owns where its state lives (beside the
journal, as a disposable cache) and when it is valid.

The fold holds the semantics of the client's, event by event (``foldTrace``, ``foldOverview`` and
the reply entries of the transcript fold in ``@vibesys/run-state``): an event at or before the
last folded sequence is already in and is skipped whole, a span keeps the place it started at, and
the latest event of a span or a turn wins. One definition of the fold lives here; the client's
ideas, measurement points and chart are folds of what it returns.

A line the fold cannot read as an event ends the journal there, as ``runs.read_events`` ends it. A
line of a kind the fold does not use is counted by the sequence and kind near its start, without
decoding the rest: the same shortcut ``event_page`` takes for lines before its start, and what
keeps a cold read of a long journal near the cost of reading its bytes.

Light like the helper: the standard library and ``server.host_run_summary``.

Public surface: ``RunSummaryFold``, ``Summarized``, ``SummaryState``, ``SummaryPage``,
``schema_id``, ``RUN_SUMMARY_VERSION``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from pydantic import JsonValue, TypeAdapter

from server.host_run_summary import (
    INPUT_GATE_TAIL_MAX_CHARS,
    INPUT_GATES_MAX,
    NO_TOKENS,
    RUN_SUMMARY_PAGE_MAX_BYTES,
    RUN_TERMINAL_MESSAGE_MAX_CHARS,
    InputGate,
    RateLimitReading,
    RunTerminal,
    RunTerminalKind,
    SpanTokens,
    TokenTally,
    TraceFormat,
    TurnReply,
    UsageRecord,
    add_tokens,
    trace_format,
)
from server.run_usage import USAGE_EVENTS, ExecutionStart, UsageFold
from vs_redaction.api import redact_diagnostic_tail

if TYPE_CHECKING:
    from server.host_run_summary import ReplySchemaDocument, RunOverviewDocument, RunSpanDocument

RUN_SUMMARY_VERSION: Final = 6
"""The version of the fold's semantics and of ``SummaryState``'s shape.

A cached state of another version is dropped and recomputed from the journal, so a change to what
the fold keeps never serves an old fold as the new one: bump it with any such change. That
includes a field a record gains: a cache written by an older reader holds the records it knew,
without the fields a newer writer put on the journal's lines, and nothing else tells the two apart.

6: the records gained ``cost_basis``, ``by_model`` and ``provider_session_id``, and the fold gained
the latest rate limit readings (``rate_limit_update`` joined the folded events)."""

_SPAN_EVENTS: Final = frozenset({"span_started", "span_updated", "span_ended"})
_TERMINAL_KINDS: Final = {
    "run_finished": RunTerminalKind.FINISHED,
    "run_failed": RunTerminalKind.FAILED,
    "run_interrupted": RunTerminalKind.INTERRUPTED,
}
_ELLIPSIS: Final = "\u2026"
_FOLDED_EVENTS: Final = frozenset(
    {
        *_SPAN_EVENTS,
        "run_started",
        "round_finished",
        "agent_execution_finished",
        "gate_finished",
        *_TERMINAL_KINDS,
        *USAGE_EVENTS,
    }
)

_PREFIX_BYTES: Final = 512
_SEQUENCE: Final = re.compile(rb'"sequence":(\d+)[,}]')
_TYPE: Final = re.compile(rb'"type":"([a-z_]+)"')
_EVENT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True)
class SummaryState:
    """What a fold holds, as plain documents: the form a cached fold is written and read in."""

    through_sequence: int
    event_count: int
    rounds_finished: int
    """The ``round_finished`` events folded."""
    spans: tuple[RunSpanDocument, ...]
    overview: RunOverviewDocument | None
    replies: tuple[TurnReply, ...]
    schemas: dict[str, ReplySchemaDocument]
    """The schemas ``replies`` use, by ``TurnReply.schema_id``, and no others."""
    tokens: TokenTally
    """The spend of every turn, those a span names and those none does."""
    usage: tuple[SpanTokens, ...]
    """The spend of each turn span, in the order the spans first spent."""
    executions: tuple[ExecutionStart, ...]
    """The agent executions that started, which a turn's record joins to by execution id."""
    records: tuple[UsageRecord, ...]
    """One record per finished turn, in journal order."""
    input_gates: tuple[InputGate, ...]
    """The gates that finished before the first workstream span began."""
    gates_settled: bool
    """Whether a workstream span has begun: a later gate is an idea's, not the input's."""
    terminal: RunTerminal | None
    """The last terminal event folded."""
    rate_limits: tuple[RateLimitReading, ...] = ()
    """The latest reading of each rate limit window the run saw, in the order the windows first appeared."""


@dataclass(frozen=True)
class SummaryPage:
    """One page of a summary's items: spans, replies (with their schemas), usage, then records."""

    spans: tuple[RunSpanDocument, ...]
    replies: tuple[TurnReply, ...]
    schemas: dict[str, ReplySchemaDocument]
    usage: tuple[SpanTokens, ...]
    records: tuple[UsageRecord, ...]
    next_offset: int | None


@dataclass(frozen=True)
class Summarized:
    """A journal's fold and the journal bytes its events occupy."""

    fold: RunSummaryFold
    journal_bytes: int


def schema_id(schema: ReplySchemaDocument) -> str:
    """The identity of a reply schema: the SHA-256 of its canonical JSON."""
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class RunSummaryFold:
    """The run level's fold of a journal, line by line.

    ``consume`` folds one journal line; ``state`` reads what has been folded. A fold built from a
    ``SummaryState`` continues it, so folding the lines after a state's journal prefix gives the
    state of the whole journal.
    """

    through_sequence: int = 0
    event_count: int = 0
    rounds_finished: int = 0
    _spans: dict[str, RunSpanDocument] = field(default_factory=dict)
    _overview: RunOverviewDocument | None = None
    _replies: dict[str, TurnReply] = field(default_factory=dict)
    _schemas: dict[str, ReplySchemaDocument] = field(default_factory=dict)
    _tokens: TokenTally = NO_TOKENS
    _usage: dict[str, TokenTally] = field(default_factory=dict)
    _turns: UsageFold = field(default_factory=UsageFold)
    _gates: list[InputGate] = field(default_factory=list)
    _gates_settled: bool = False
    _terminal: RunTerminal | None = None

    @classmethod
    def from_state(cls, state: SummaryState) -> RunSummaryFold:
        """A fold that has folded what ``state`` holds."""
        return cls(
            through_sequence=state.through_sequence,
            event_count=state.event_count,
            rounds_finished=state.rounds_finished,
            _spans={cast("str", span["id"]): span for span in state.spans},
            _overview=state.overview,
            _replies={reply.span_id: reply for reply in state.replies},
            _schemas=dict(state.schemas),
            _tokens=state.tokens,
            _usage={entry.span_id: entry.tally for entry in state.usage},
            _turns=UsageFold.from_state(state.executions, state.records, state.rate_limits),
            _gates=list(state.input_gates),
            _gates_settled=state.gates_settled,
            _terminal=state.terminal,
        )

    def state(self) -> SummaryState:
        """What has been folded, with only the schemas its replies use."""
        replies = tuple(self._replies.values())
        used = {reply.schema_id for reply in replies}
        return SummaryState(
            through_sequence=self.through_sequence,
            event_count=self.event_count,
            rounds_finished=self.rounds_finished,
            spans=tuple(self._spans.values()),
            overview=self._overview,
            replies=replies,
            schemas={key: value for key, value in self._schemas.items() if key in used},
            tokens=self._tokens,
            usage=tuple(SpanTokens(span_id=key, tally=value) for key, value in self._usage.items()),
            executions=self._turns.starts,
            records=self._turns.records,
            input_gates=tuple(self._gates),
            gates_settled=self._gates_settled,
            terminal=self._terminal,
            rate_limits=self._turns.limits,
        )

    def consume(self, line: bytes, *, complete: bool = True) -> bool:
        """Fold one journal line; false when it is not a readable event, where the journal ends.

        A blank line is nothing. An event at or before the last folded sequence is skipped. A line
        of a kind the fold does not use is read by its first bytes unless ``complete`` is false:
        a last line the writer has not terminated is decoded whole, since its start can look like
        an event whose end was never written.
        """
        if not line.strip():
            return True
        head = line[:_PREFIX_BYTES]
        sequence = _SEQUENCE.search(head)
        kind = _TYPE.search(head)
        if (
            complete
            and sequence is not None
            and kind is not None
            and kind.group(1).decode() not in _FOLDED_EVENTS
            and line[-3:].rstrip().endswith(b"}")
        ):
            self._count(int(sequence.group(1)))
            return True
        event = _decode_event(line)
        if event is None:
            return False
        at = cast("int", event["sequence"])
        if not self._count(at):
            return True
        self._fold(event)
        return True

    def page(self, offset: int, max_bytes: int = RUN_SUMMARY_PAGE_MAX_BYTES) -> SummaryPage:
        """The items from index ``offset`` (spans, replies, usage, then records) that fit ``max_bytes``.

        A page holds at least one item when any remains. It carries the schemas of its replies that
        no earlier reply uses, so the pages of one summary carry each schema once.
        """
        spans = list(self._spans.values())
        replies = list(self._replies.values())
        usage = [SpanTokens(span_id=key, tally=value) for key, value in self._usage.items()]
        records = list(self._turns.records)
        items: list[RunSpanDocument | TurnReply | SpanTokens | UsageRecord] = [
            *spans,
            *replies,
            *usage,
            *records,
        ]
        replies_from = len(spans)
        introduced = {reply.schema_id for reply in replies[: max(0, offset - replies_from)]}
        schemas: dict[str, ReplySchemaDocument] = {}
        size = 0
        index = offset
        while index < len(items):
            item = items[index]
            new_schema: str | None = None
            if (
                isinstance(item, TurnReply)
                and item.schema_id not in introduced
                and item.schema_id not in schemas
            ):
                new_schema = item.schema_id
            cost = _item_size(item) + (
                _size(self._schemas[new_schema]) if new_schema is not None else 0
            )
            if index > offset and size + cost > max_bytes:
                break
            if new_schema is not None:
                schemas[new_schema] = self._schemas[new_schema]
            size += cost
            index += 1
        usage_from = replies_from + len(replies)
        records_from = usage_from + len(usage)
        return SummaryPage(
            spans=tuple(spans[offset:index]),
            replies=tuple(replies[max(0, offset - replies_from) : max(0, index - replies_from)]),
            schemas=schemas,
            usage=tuple(usage[max(0, offset - usage_from) : max(0, index - usage_from)]),
            records=tuple(records[max(0, offset - records_from) : max(0, index - records_from)]),
            next_offset=index if index < len(items) else None,
        )

    @property
    def input_gates(self) -> tuple[InputGate, ...]:
        """The gates that finished before the first idea began, folded so far."""
        return tuple(self._gates)

    @property
    def terminal(self) -> RunTerminal | None:
        """The last terminal event folded so far."""
        return self._terminal

    @property
    def trace(self) -> TraceFormat:
        """Whether the journal folded so far records a run trace (``trace_format``)."""
        return trace_format(spans=len(self._spans), rounds=self.rounds_finished)

    @property
    def overview(self) -> RunOverviewDocument | None:
        """The overview the last ``run_started`` that declares one carried."""
        return self._overview

    @property
    def tokens(self) -> TokenTally:
        """The spend of every turn folded so far."""
        return self._tokens

    @property
    def usage_count(self) -> int:
        """Turn spans that have a spend folded so far."""
        return len(self._usage)

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        """The usage records folded so far, in journal order."""
        return self._turns.records

    @property
    def record_count(self) -> int:
        """Usage records folded so far."""
        return len(self._turns.records)

    @property
    def span_count(self) -> int:
        """Spans folded so far."""
        return len(self._spans)

    @property
    def reply_count(self) -> int:
        """Spans that have a reply folded so far."""
        return len(self._replies)

    def _count(self, sequence: int) -> bool:
        """Take an event of ``sequence`` into the count; false when it is already folded."""
        if sequence <= self.through_sequence:
            return False
        self.through_sequence = sequence
        self.event_count += 1
        return True

    def _fold(self, event: dict[str, JsonValue]) -> None:
        kind = event.get("type")
        data = event.get("data")
        if isinstance(kind, str) and kind in _TERMINAL_KINDS:
            # A terminal event may carry no data: its text is the account.
            self._terminal = _terminal_of(event, _TERMINAL_KINDS[kind])
        elif isinstance(data, dict) and isinstance(kind, str):
            self._fold_data(kind, event, data)

    def _fold_data(
        self, kind: str, event: dict[str, JsonValue], data: dict[str, JsonValue]
    ) -> None:
        if kind == "gate_finished":
            self._fold_gate(event, data)
        elif kind in _SPAN_EVENTS:
            self._fold_span(kind, data.get("span"))
        elif kind == "run_started" and data.get("kind") == "run_started":
            overview = data.get("overview")
            if isinstance(overview, dict):
                self._overview = overview
        elif kind == "round_finished":
            self.rounds_finished += 1
        elif kind == "agent_execution_finished" and data.get("kind") == "agent_execution_finished":
            self._fold_reply(event.get("span_id"), data.get("reply"))
        elif kind in USAGE_EVENTS:
            record = self._turns.consume(event)
            if record is not None:
                self._fold_usage(record)

    def _fold_span(self, kind: str, span: JsonValue) -> None:
        """Keep the latest event of a span; the first workstream span to begin ends the input's gates."""
        if isinstance(span, dict) and isinstance(span.get("id"), str):
            self._spans[cast("str", span["id"])] = span
        if kind == "span_started" and _is_workstream(span):
            self._gates_settled = True

    def _fold_gate(self, event: dict[str, JsonValue], data: dict[str, JsonValue]) -> None:
        """Keep a gate that finished before the first idea began (``foldInputGates``).

        Once an idea has begun, a pass of a gate that still stands failed is kept too: the input's
        gate tried again, which supersedes the failure.
        """
        gate = data.get("gate")
        if len(self._gates) >= INPUT_GATES_MAX or not isinstance(gate, str):
            return
        recipe = data.get("recipe")
        recipe = recipe if isinstance(recipe, str) else None
        if self._gates_settled and (
            event.get("status") == "failed" or not self._stands_failed(gate, recipe)
        ):
            return
        tail = data.get("output_tail")
        self._gates.append(
            InputGate(
                sequence=cast("int", event["sequence"]),
                gate=gate,
                recipe=recipe,
                passed=event.get("status") != "failed",
                output_tail=(
                    redact_diagnostic_tail(tail, max_chars=INPUT_GATE_TAIL_MAX_CHARS)
                    if isinstance(tail, str)
                    else None
                ),
            )
        )

    def _stands_failed(self, gate: str, recipe: str | None) -> bool:
        """Whether the last kept attempt of this gate (and recipe) failed."""
        attempts = [kept for kept in self._gates if kept.gate == gate and kept.recipe == recipe]
        return bool(attempts) and not attempts[-1].passed

    def _fold_usage(self, record: UsageRecord) -> None:
        """Add one finished turn's spend to the run's, and to its span's when it names one."""
        self._tokens = add_tokens(self._tokens, record.tokens)
        if record.span_id is not None:
            self._usage[record.span_id] = add_tokens(
                self._usage.get(record.span_id, NO_TOKENS), record.tokens
            )

    def _fold_reply(self, span_id: JsonValue, reply: JsonValue) -> None:
        if not isinstance(span_id, str) or not isinstance(reply, dict):
            return
        reply_kind = reply.get("kind")
        schema = reply.get("json_schema")
        value = reply.get("value")
        if not isinstance(reply_kind, str) or not isinstance(schema, dict):
            return
        if not isinstance(value, dict):
            return
        key = schema_id(schema)
        self._schemas[key] = schema
        self._replies[span_id] = TurnReply(
            span_id=span_id, kind=reply_kind, schema_id=key, value=value
        )


def _is_workstream(span: JsonValue) -> bool:
    """Whether ``span`` is an idea's workstream (the client's ``label.kind === 'workstream'``)."""
    label = span.get("label") if isinstance(span, dict) else None
    return isinstance(label, dict) and label.get("kind") == "workstream"


def _terminal_of(event: dict[str, JsonValue], kind: RunTerminalKind) -> RunTerminal:
    """The terminal ``event`` as the summary carries it: its text (else its diagnostic's summary), whole."""
    text = event.get("text")
    diagnostic = event.get("diagnostic")
    summary = diagnostic.get("summary") if isinstance(diagnostic, dict) else None
    message = next(
        (each.strip() for each in (text, summary) if isinstance(each, str) and each.strip()), None
    )
    if message is not None and len(message) > RUN_TERMINAL_MESSAGE_MAX_CHARS:
        message = message[: RUN_TERMINAL_MESSAGE_MAX_CHARS - 1] + _ELLIPSIS
    return RunTerminal(sequence=cast("int", event["sequence"]), kind=kind, message=message)


def _decode_event(line: bytes) -> dict[str, JsonValue] | None:
    """The journal event on ``line``: a JSON object with an integer ``sequence``, else ``None``."""
    try:
        event = _EVENT.validate_json(line)
    except ValueError:
        return None
    return event if type(event.get("sequence")) is int else None


def _item_size(item: RunSpanDocument | TurnReply | SpanTokens | UsageRecord) -> int:
    """The bytes a page spends on one item, a reply's schema aside."""
    if isinstance(item, TurnReply):
        return _size(item.value)
    if isinstance(item, SpanTokens):
        return _size(item.tally.model_dump(mode="json"))
    if isinstance(item, UsageRecord):
        return _size(item.model_dump(mode="json"))
    return _size(item)


def _size(document: JsonValue) -> int:
    return len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode())
