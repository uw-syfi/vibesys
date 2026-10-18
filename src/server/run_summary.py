"""A run's summary: the fold of its journal that the run level shows, kept up to date as it grows.

``RunSummaryFold`` reads journal lines in order and keeps what a client reading the whole journal
would fold for the run level: the run trace (every span as its latest span event left it), the
overview a ``run_started`` event declares, and the last typed reply of each agent turn. It is a
pure read model over documents: it opens no file and reads no clock, so a property test can feed it
every prefix of a journal. ``server.host_helper_os`` owns where its state lives (beside the
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
    RUN_SUMMARY_PAGE_MAX_BYTES,
    SpanTokens,
    TokenTally,
    TraceFormat,
    TurnReply,
    trace_format,
)

if TYPE_CHECKING:
    from server.host_run_summary import ReplySchemaDocument, RunOverviewDocument, RunSpanDocument

RUN_SUMMARY_VERSION: Final = 3
"""The version of the fold's semantics and of ``SummaryState``'s shape.

A cached state of another version is dropped and recomputed from the journal, so a change to what
the fold keeps never serves an old fold as the new one: bump it with any such change."""

_SPAN_EVENTS: Final = frozenset({"span_started", "span_updated", "span_ended"})
_FOLDED_EVENTS: Final = frozenset(
    {
        *_SPAN_EVENTS,
        "run_started",
        "round_finished",
        "agent_execution_finished",
        "turn_usage_recorded",
    }
)
_NO_TOKENS: Final = TokenTally(
    input=None, cache_read=None, cache_write=None, output=None, reasoning=None
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


@dataclass(frozen=True)
class SummaryPage:
    """One page of a summary's items: spans, then replies (with the schemas they need), then usage."""

    spans: tuple[RunSpanDocument, ...]
    replies: tuple[TurnReply, ...]
    schemas: dict[str, ReplySchemaDocument]
    usage: tuple[SpanTokens, ...]
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
    _tokens: TokenTally = _NO_TOKENS
    _usage: dict[str, TokenTally] = field(default_factory=dict)

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
        """The items from index ``offset`` (spans, then replies, then usage) that fit ``max_bytes``.

        A page holds at least one item when any remains. It carries the schemas of its replies that
        no earlier reply uses, so the pages of one summary carry each schema once.
        """
        spans = list(self._spans.values())
        replies = list(self._replies.values())
        usage = [SpanTokens(span_id=key, tally=value) for key, value in self._usage.items()]
        replies_from = len(spans)
        usage_from = replies_from + len(replies)
        total = usage_from + len(usage)
        taken_spans: list[RunSpanDocument] = []
        taken_replies: list[TurnReply] = []
        taken_usage: list[SpanTokens] = []
        schemas: dict[str, ReplySchemaDocument] = {}
        introduced = {reply.schema_id for reply in replies[: max(0, offset - replies_from)]}
        size = 0
        index = offset
        while index < total:
            new_schema: str | None = None
            if index < replies_from:
                cost = _size(spans[index])
            elif index < usage_from:
                reply = replies[index - replies_from]
                if reply.schema_id not in introduced and reply.schema_id not in schemas:
                    new_schema = reply.schema_id
                cost = _size(reply.value) + (
                    _size(self._schemas[new_schema]) if new_schema is not None else 0
                )
            else:
                cost = _size(usage[index - usage_from].tally.model_dump(mode="json"))
            if index > offset and size + cost > max_bytes:
                break
            if index < replies_from:
                taken_spans.append(spans[index])
            elif index < usage_from:
                taken_replies.append(replies[index - replies_from])
                if new_schema is not None:
                    schemas[new_schema] = self._schemas[new_schema]
            else:
                taken_usage.append(usage[index - usage_from])
            size += cost
            index += 1
        return SummaryPage(
            spans=tuple(taken_spans),
            replies=tuple(taken_replies),
            schemas=schemas,
            usage=tuple(taken_usage),
            next_offset=index if index < total else None,
        )

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
        if not isinstance(data, dict):
            return
        if kind in _SPAN_EVENTS:
            span = data.get("span")
            if isinstance(span, dict) and isinstance(span.get("id"), str):
                self._spans[cast("str", span["id"])] = span
        elif kind == "run_started" and data.get("kind") == "run_started":
            overview = data.get("overview")
            if isinstance(overview, dict):
                self._overview = overview
        elif kind == "round_finished":
            self.rounds_finished += 1
        elif kind == "agent_execution_finished" and data.get("kind") == "agent_execution_finished":
            self._fold_reply(event.get("span_id"), data.get("reply"))
        elif kind == "turn_usage_recorded" and data.get("kind") == "turn_usage_recorded":
            self._fold_usage(data)

    def _fold_usage(self, data: dict[str, JsonValue]) -> None:
        """Add one finished turn's spend to the run's, and to its span's when it names one."""
        spent = TokenTally(
            input=_count(data.get("input_tokens")),
            cache_read=_count(data.get("cache_read_input_tokens")),
            cache_write=_count(data.get("cache_creation_input_tokens")),
            output=_count(data.get("output_tokens")),
            reasoning=_count(data.get("reasoning_output_tokens")),
        )
        self._tokens = _plus(self._tokens, spent)
        span_id = data.get("span_id")
        if isinstance(span_id, str):
            self._usage[span_id] = _plus(self._usage.get(span_id, _NO_TOKENS), spent)

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


def _count(value: JsonValue) -> int | None:
    """A reported token count; anything else (absent, null, malformed) is not reported."""
    return value if type(value) is int and value >= 0 else None


def _sum(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    return a if b is None else a + b


def _plus(a: TokenTally, b: TokenTally) -> TokenTally:
    """``a`` and ``b`` summed count by count; a count neither reported stays unreported."""
    return TokenTally(
        input=_sum(a.input, b.input),
        cache_read=_sum(a.cache_read, b.cache_read),
        cache_write=_sum(a.cache_write, b.cache_write),
        output=_sum(a.output, b.output),
        reasoning=_sum(a.reasoning, b.reasoning),
    )


def _decode_event(line: bytes) -> dict[str, JsonValue] | None:
    """The journal event on ``line``: a JSON object with an integer ``sequence``, else ``None``."""
    try:
        event = _EVENT.validate_json(line)
    except ValueError:
        return None
    return event if type(event.get("sequence")) is int else None


def _size(document: JsonValue) -> int:
    return len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode())
