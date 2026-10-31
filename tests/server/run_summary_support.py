"""Journals of generated run events, and the client's fold of them, for the run summary's tests.

``events`` draws typed ``RunEvent`` values (the models a run records), ``journal_lines`` writes them
as the event store does (``model_dump_json`` and a newline), and ``client_fold`` folds the typed
events the way ``@vibesys/run-state`` does (``foldTrace``, ``foldOverview`` and the reply entries of
the transcript fold). It is written over the typed models, not over the documents the helper
reads, so the helper's output is checked against the contract's own models and not against itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from hypothesis import strategies as st

from server.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    EventStatus,
    EventType,
    GateFinishedData,
    ModelTurnUsageData,
    OutputData,
    RunEvent,
    RunInterruptedData,
    RunStartedData,
    SpanEndedData,
    SpanStartedData,
    SpanUpdatedData,
    TurnUsageData,
)
from vibesys.api import AgentReply, GateKind, RunOverview
from vs_core.api import Span, SpanLabel, SpanStatus, SpanType

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

RUN_ID = "20261010-120000-11111111-dynamic"

EXECUTION_IDS = ("inv:planner:000001:0", "inv:implementer:000001:0", "inv:judge:000002:0")
ROLES = ("planner", "implementer", "judge")

_SPAN_IDS = ("flow:search#1", "flow:search/idea#1", "op:000001/implementer.1", "op:000002/gate.1")
_SCHEMAS: tuple[dict[str, Any], ...] = (
    {"title": "Plan", "type": "object", "properties": {"outcome": {"enum": ["nominated"]}}},
    {"title": "Review", "type": "object", "properties": {"verdict": {"type": "string"}}},
    {"title": "Plan", "type": "object", "properties": {"outcome": {"enum": ["blocked"]}}},
)


def _split() -> st.SearchStrategy[tuple[ModelTurnUsageData, ...]]:
    """No split, or the rows of one turn: unique models, exactly one primary, requests on it alone."""
    count = st.integers(min_value=0, max_value=5000)

    def rows(
        models: list[str], primary: int, turns: int
    ) -> st.SearchStrategy[tuple[ModelTurnUsageData, ...]]:
        return st.tuples(
            *(
                st.builds(
                    ModelTurnUsageData,
                    model=st.just(model),
                    input_tokens=count,
                    cache_creation_input_tokens=count,
                    cache_creation_1h_input_tokens=count,
                    cache_read_input_tokens=count,
                    output_tokens=count,
                    reasoning_output_tokens=count,
                    turns=st.just(turns if index == primary else 0),
                    recorded_usd=st.none() | st.floats(0, 5, allow_nan=False),
                    speed=st.sampled_from([None, "standard", "fast", "ultrafast"])
                    if index == primary
                    else st.none(),
                    primary=st.just(index == primary),
                )
                for index, model in enumerate(models)
            )
        )

    split = st.tuples(
        st.lists(
            st.sampled_from(["opus", "haiku", "sonnet", "gpt-6.1-sol"]),
            min_size=1,
            max_size=3,
            unique=True,
        ),
        st.integers(min_value=0, max_value=2),
        st.integers(min_value=1, max_value=30),
    ).flatmap(lambda parts: rows(parts[0], parts[1] % len(parts[0]), parts[2]))
    return st.just(()) | split


def _span() -> st.SearchStrategy[Span]:
    return st.builds(
        Span,
        id=st.sampled_from(_SPAN_IDS),
        parent=st.none(),
        type=st.sampled_from(SpanType),
        name=st.text(max_size=12),
        status=st.sampled_from(SpanStatus),
        started_at=st.floats(0, 1000, allow_nan=False),
        ended_at=st.none() | st.floats(0, 1000, allow_nan=False),
    )


def _reply() -> st.SearchStrategy[AgentReply]:
    return st.builds(
        AgentReply,
        kind=st.sampled_from(["Plan", "Review"]),
        json_schema=st.sampled_from(_SCHEMAS),
        value=st.fixed_dictionaries({"summary": st.text(max_size=20)}),
    )


def _overview() -> st.SearchStrategy[RunOverview]:
    return st.text(min_size=1, max_size=20).map(
        lambda text: RunOverview.model_validate({"objective": {"text": text}})
    )


@dataclass(frozen=True)
class _Draft:
    """An event before it has a sequence: its type, its payload and the turn span it names."""

    type: EventType
    data: Any
    span_id: str | None = None
    execution_id: str | None = None
    agent_kind: str | None = None
    text: str = ""
    status: EventStatus | None = None


def _drafts() -> st.SearchStrategy[_Draft]:
    span_data = st.tuples(
        st.sampled_from([SpanStartedData, SpanUpdatedData, SpanEndedData]), _span()
    ).map(
        lambda pair: _Draft(EventType(pair[0].model_fields["kind"].default), pair[0](span=pair[1]))
    )
    finished = st.tuples(st.none() | _reply(), st.none() | st.sampled_from(_SPAN_IDS)).map(
        lambda pair: _Draft(
            EventType.AGENT_EXECUTION_FINISHED,
            AgentExecutionFinishedData(result="done", reply=pair[0]),
            pair[1],
        )
    )
    started = _overview().map(
        lambda overview: _Draft(
            EventType.RUN_STARTED,
            RunStartedData(outer_loop="dynamic", input="x", overview=overview),
        )
    )
    unseen = st.just(
        _Draft(EventType.RUN_STARTED, RunStartedData(outer_loop="dynamic", input="no overview"))
    )
    counts = st.none() | st.integers(min_value=0, max_value=5000)
    usage_data = st.builds(
        TurnUsageData,
        input_tokens=counts,
        cache_creation_input_tokens=counts,
        cache_read_input_tokens=counts,
        output_tokens=counts,
        reasoning_output_tokens=counts,
        total_cost_usd=st.none() | st.floats(0, 5, allow_nan=False),
        duration_ms=counts,
        span_id=st.none() | st.sampled_from(_SPAN_IDS),
        # A line recorded before the split has none of these three; one after has them all.
        cost_basis=st.none()
        | st.sampled_from(["price_table", "provider_reported", "unknown_model"]),
        by_model=_split(),
        provider_session_id=st.none() | st.text(min_size=1, max_size=8),
    )
    spent = st.tuples(usage_data, st.none() | st.sampled_from(EXECUTION_IDS)).map(
        lambda pair: _Draft(EventType.TURN_USAGE_RECORDED, pair[0], execution_id=pair[1])
    )
    begun = st.tuples(
        st.sampled_from(range(len(EXECUTION_IDS))),
        st.none() | st.sampled_from(["codex", "claude_code"]),
        st.none() | st.sampled_from(["gpt-6-luna", "opus-5"]),
        st.none() | st.sampled_from(["low", "high"]),
    ).map(
        lambda parts: _Draft(
            EventType.AGENT_EXECUTION_STARTED,
            AgentExecutionStartedData(
                stage=ROLES[parts[0]],
                activity=AgentExecutionActivityData(mode="thinking", summary="working"),
                provider=parts[1],
                model=parts[2],
                reasoning_effort=parts[3],
            ),
            execution_id=EXECUTION_IDS[parts[0]],
            agent_kind=ROLES[parts[0]],
        )
    )
    tails = st.none() | st.text(max_size=40) | st.integers(3990, 4010).map(lambda n: "x" * n)
    gate = st.tuples(
        st.sampled_from(GateKind),
        st.none() | st.sampled_from(["unit", "lint"]),
        tails,
        st.sampled_from([EventStatus.FAILED, EventStatus.COMPLETED]),
    ).map(
        lambda parts: _Draft(
            EventType.GATE_FINISHED,
            GateFinishedData(gate=parts[0], recipe=parts[1], output_tail=parts[2]),
            status=parts[3],
        )
    )
    workstream = st.just(
        _Draft(
            EventType.SPAN_STARTED,
            SpanStartedData(
                span=Span(
                    id="flow:search/idea#1",
                    parent=None,
                    type=SpanType.FLOW,
                    name="idea",
                    status=SpanStatus.RUNNING,
                    started_at=1.0,
                    label=SpanLabel(kind="workstream"),
                )
            ),
        )
    )
    message = st.text(max_size=60)
    terminal = st.one_of(
        message.map(lambda text: _Draft(EventType.RUN_FAILED, None, text=text)),
        message.map(lambda text: _Draft(EventType.RUN_FINISHED, None, text=text)),
        message.map(
            lambda text: _Draft(
                EventType.RUN_INTERRUPTED, RunInterruptedData(reason="stop"), text=text
            )
        ),
    )
    noise = st.text(max_size=300).map(
        lambda text: _Draft(EventType.OUTPUT, OutputData(stream="stdout", content=text))
    )
    return st.one_of(
        span_data,
        span_data,
        finished,
        finished,
        spent,
        spent,
        begun,
        started,
        unseen,
        noise,
        gate,
        gate,
        workstream,
        terminal,
    )


@st.composite
def events(draw: st.DrawFn, max_size: int = 40) -> list[RunEvent]:
    """Events as a run records them, with repeated and regressing sequences now and then.

    A sequence normally follows the one before; a repeat or a step back is an event an older
    writer duplicated, which every reader of the journal skips.
    """
    drafts = draw(st.lists(_drafts(), max_size=max_size))
    steps = draw(
        st.lists(
            st.sampled_from([1, 1, 1, 1, 1, 2, 0, -1]), min_size=len(drafts), max_size=len(drafts)
        )
    )
    sequence = 0
    made: list[RunEvent] = []
    for draft, step in zip(drafts, steps, strict=True):
        sequence = max(1, sequence + step)
        made.append(
            RunEvent(
                sequence=sequence,
                run_id=RUN_ID,
                timestamp=datetime(2026, 10, 10, 12, tzinfo=UTC),
                type=draft.type,
                data=draft.data,
                span_id=draft.span_id,
                execution_id=draft.execution_id,
                agent_kind=draft.agent_kind,
                text=draft.text,
                status=draft.status,
            )
        )
    return made


def journal_lines(run_events: Iterable[RunEvent]) -> list[bytes]:
    """The journal lines the event store writes for ``run_events``."""
    return [event.model_dump_json().encode() + b"\n" for event in run_events]


@dataclass(frozen=True)
class ClientFold:
    """What a client folds from a journal prefix, by the models: the summary's expected value."""

    through_sequence: int
    event_count: int
    spans: tuple[dict[str, Any], ...]
    overview: dict[str, Any] | None
    replies: tuple[dict[str, Any], ...]
    """Each turn's last reply as ``{span_id, kind, json_schema, value}``, in first-reply order."""
    tokens: dict[str, int | None]
    """Every turn's spend summed (``ledgerWith``): input, cache_read, cache_write, output, reasoning."""
    usage: dict[str, dict[str, int | None]]
    """The same sum for each turn span a usage event names, in the order the spans first spent."""
    input_gates: tuple[dict[str, Any], ...]
    """The ``gate_finished`` events before the first workstream span, and after it the passes of a
    gate that stood failed (``foldInputGates``), at most 32."""
    terminal: dict[str, Any] | None
    """The last ``run_finished``, ``run_failed`` or ``run_interrupted`` event: its sequence, kind, text."""


_TERMINAL_KINDS = {
    EventType.RUN_FINISHED: "finished",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_INTERRUPTED: "interrupted",
}


NO_TOKENS: dict[str, int | None] = {
    "input": None,
    "cache_read": None,
    "cache_write": None,
    "output": None,
    "reasoning": None,
}


def _added(a: dict[str, int | None], b: dict[str, int | None]) -> dict[str, int | None]:
    """``a`` and ``b`` summed count by count, as the client's ``addTokens`` does."""

    def plus(x: int | None, y: int | None) -> int | None:
        if x is None:
            return y
        return x if y is None else x + y

    return {key: plus(a[key], b[key]) for key in a}


def _stands_failed(gates: Sequence[dict[str, Any]], data: GateFinishedData) -> bool:
    """Whether the last kept attempt of ``data``'s gate (and recipe) failed: a retry may supersede it."""
    attempts = [g for g in gates if g["gate"] == data.gate.value and g["recipe"] == data.recipe]
    return bool(attempts) and not attempts[-1]["passed"]


def _ending_fold(
    run_events: Sequence[RunEvent],
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any] | None]:
    """The input gates and the terminal event, as ``foldInputGates`` and the run's last terminal event read them."""
    gates: list[dict[str, Any]] = []
    settled = False
    terminal: dict[str, Any] | None = None
    last = 0
    for event in run_events:
        if event.sequence <= last:
            continue
        last = event.sequence
        data = event.data
        if event.type in _TERMINAL_KINDS:
            text = event.text.strip()
            terminal = {
                "sequence": event.sequence,
                "kind": _TERMINAL_KINDS[event.type],
                "message": text or None,
            }
        if (
            isinstance(data, GateFinishedData)
            and len(gates) < 32
            and (
                not settled
                or (event.status is not EventStatus.FAILED and _stands_failed(gates, data))
            )
        ):
            gates.append(
                {
                    "sequence": event.sequence,
                    "gate": data.gate.value,
                    "recipe": data.recipe,
                    "passed": event.status is not EventStatus.FAILED,
                    "output_tail": None if data.output_tail is None else data.output_tail[-4000:],
                }
            )
        if isinstance(data, SpanStartedData) and event.type is EventType.SPAN_STARTED:
            settled = settled or (
                data.span.label is not None and data.span.label.kind == "workstream"
            )
    return tuple(gates), terminal


def client_fold(run_events: Sequence[RunEvent]) -> ClientFold:
    """Fold ``run_events`` as ``foldTrace``, ``foldOverview`` and the transcript's reply entries do."""
    last = 0
    count = 0
    spans: dict[str, dict[str, Any]] = {}
    overview: dict[str, Any] | None = None
    replies: dict[str, dict[str, Any]] = {}
    total = dict(NO_TOKENS)
    by_span: dict[str, dict[str, int | None]] = {}
    for event in run_events:
        if event.sequence <= last:
            continue
        last = event.sequence
        count += 1
        data = event.data
        if isinstance(data, SpanStartedData | SpanUpdatedData | SpanEndedData):
            spans[data.span.id] = json.loads(data.span.model_dump_json())
        elif isinstance(data, RunStartedData) and data.overview is not None:
            overview = json.loads(data.overview.model_dump_json())
        elif isinstance(data, TurnUsageData):
            spent = {
                "input": data.input_tokens,
                "cache_read": data.cache_read_input_tokens,
                "cache_write": data.cache_creation_input_tokens,
                "output": data.output_tokens,
                "reasoning": data.reasoning_output_tokens,
            }
            total = _added(total, spent)
            if data.span_id is not None:
                by_span[data.span_id] = _added(by_span.get(data.span_id, NO_TOKENS), spent)
        elif (
            isinstance(data, AgentExecutionFinishedData)
            and data.reply is not None
            and event.span_id is not None
        ):
            replies[event.span_id] = {
                "span_id": event.span_id,
                **json.loads(data.reply.model_dump_json()),
            }
    return ClientFold(
        last,
        count,
        tuple(spans.values()),
        overview,
        tuple(replies.values()),
        total,
        by_span,
        *_ending_fold(run_events),
    )
