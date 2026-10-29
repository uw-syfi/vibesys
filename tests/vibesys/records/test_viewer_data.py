"""Differential viewer projections across generated, interleaved journal sequences."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from server.run_summary import RunSummaryFold, SummaryState
from vibesys.records.api import MappingState, map_event


def journals() -> st.SearchStrategy[list[e.CoreEvent]]:
    return st.builds(
        journal,
        wall=st.integers(min_value=1700000000, max_value=1900000000),
        specs=st.lists(
            st.tuples(st.integers(1, 1000), st.sampled_from(["model-a", "model-b"])),
            min_size=1,
            max_size=5,
        ),
    )


def journal(wall: int, specs: list[tuple[int, str]]) -> list[e.CoreEvent]:
    observations: list[e.CoreEvent] = []

    def add(kind: e.CoreEventType, at: int, data: object = None, **fields: object) -> None:
        observations.append(
            e.CoreEvent.model_validate(
                dict(
                    type=kind,
                    timestamp=datetime.fromtimestamp(wall, UTC) + timedelta(seconds=at),
                    run_id="run-a",
                    data=data,
                    **fields,
                )
            )
        )

    add(e.CoreEventType.RUN_STARTED, 0, e.RunStartedData(outer_loop="dynamic", input="input"))
    for index, (duration, model) in enumerate(specs):
        start = index + 1
        end = start + duration
        identity = f"turn-{index}"
        span_id = f"op:{identity}"
        source = {
            "id": span_id,
            "parent": None,
            "type": "operation",
            "name": "ask",
            "status": "running",
            "started_at": wall + start,
            "invocation": identity,
            "label": {
                "kind": "agent_turn",
                "attrs_json": json.dumps({"agent": identity, "role": "implementer", "model": None}),
            },
        }
        add(e.CoreEventType.SPAN_STARTED, start, {"kind": "span_started", "span": source})
        add(
            e.CoreEventType.AGENT_EXECUTION_STARTED,
            start,
            e.AgentExecutionStartedData(
                stage="implement",
                provider="provider-a",
                model=model,
                activity=e.AgentExecutionActivityData(mode="thinking", summary="Working"),
            ),
            execution_id=identity,
            agent_kind="implementer",
        )
        add(
            e.CoreEventType.AGENT_OUTPUT_CHUNK,
            start,
            e.AgentOutputChunkData(channel="assistant", content="Run narration"),
        )
        add(
            e.CoreEventType.AGENT_OUTPUT_CHUNK,
            start,
            e.AgentOutputChunkData(channel="assistant", content="Turn output"),
            execution_id=identity,
            agent_kind="implementer",
        )
        add(
            e.CoreEventType.TURN_USAGE_RECORDED,
            end,
            e.TurnUsageData(span_id=span_id, input_tokens=duration, duration_ms=duration * 1000),
            execution_id=identity,
        )
        add(
            e.CoreEventType.AGENT_EXECUTION_FINISHED,
            end,
            e.AgentExecutionFinishedData(),
            execution_id=identity,
        )
        source = dict(source, status="done", ended_at=wall + end)
        add(e.CoreEventType.SPAN_ENDED, end, {"kind": "span_ended", "span": source})
        gate = {
            "id": f"op:gate-{index}",
            "parent": None,
            "type": "operation",
            "name": "measure",
            "status": "done",
            "started_at": wall + start,
            "ended_at": wall + end,
            "label": {
                "kind": "gate",
                "attrs_json": json.dumps(
                    {
                        "stage": "measurement",
                        "metrics": [
                            {
                                "name": "throughput",
                                "value": duration,
                                "direction": "max",
                                "unit": "ops/s",
                            }
                        ],
                    }
                ),
            },
        }
        if index % 2 == 0:
            add(
                e.CoreEventType.SPAN_STARTED,
                start,
                {
                    "kind": "span_started",
                    "span": dict(gate, status="running", ended_at=None),
                },
            )
        add(e.CoreEventType.SPAN_ENDED, end, {"kind": "span_ended", "span": gate})
    observations.sort(key=lambda event: event.timestamp)
    add(e.CoreEventType.RUN_FINISHED, max(s + i + 1 for i, (s, _) in enumerate(specs)))
    return [event.model_copy(update={"sequence": i}) for i, event in enumerate(observations, 1)]


def project(
    events: list[e.CoreEvent], *, omit_nulls: bool = False
) -> tuple[dict[str, p.Span], list[p.Record], RunSummaryFold]:
    origin = events[0].timestamp.timestamp()
    state = MappingState("run-a", wall_span_clock=True, clock_epochs=((origin, 0),))
    records: list[p.Record] = []
    legacy = RunSummaryFold()
    for event in events:
        wire = usage_wire(event, omit_nulls=omit_nulls)
        assert legacy.consume(wire)
        result = map_event(state, e.CoreEvent.model_validate_json(wire))
        state = result.state
        records.extend(result.records)
    return {key: p.Span.from_binary(value) for key, value in state.spans}, records, legacy


@pytest.mark.regression
@given(events=journals())
@example(events=journal(1792599885, [(3, "model-a"), (7, "model-b")]))
def test_metric_and_span_durations_match_legacy_clock(events: list[e.CoreEvent]) -> None:
    spans, records, legacy = project(events)
    origin = events[0].timestamp.timestamp()
    for raw in legacy.state().spans:
        source = e.SpanEndedData.model_validate({"span": raw}).span
        mapped = spans[source.id]
        assert source.ended_at is not None
        assert mapped.started_at == pytest.approx(source.started_at - origin)
        assert mapped.ended_at == pytest.approx(source.ended_at - origin)
        assert mapped.ended_at - mapped.started_at == pytest.approx(
            source.ended_at - source.started_at
        )
    latest_metrics: dict[str, p.MetricPoint] = {}
    for record in records:
        if record.body and record.body.field == "metric":
            metric = record.body.value
            latest_metrics[metric.span] = metric
            assert metric.clock <= record.clock
    for span_id, metric in latest_metrics.items():
        assert metric.clock == spans[span_id].ended_at


@pytest.mark.regression
@given(events=journals())
@example(events=journal(1792599885, [(3, "model-a"), (7, "model-b")]))
def test_final_turn_models_match_legacy_fold(events: list[e.CoreEvent]) -> None:
    spans, _, legacy = project(events)
    for record in legacy.records:
        assert record.span_id is not None
        span = spans[record.span_id]
        assert span.label is not None
        assert span.label.attrs is not None
        attrs = p.AgentTurnAttrs.from_binary(span.label.attrs.value)
        assert attrs.model == record.model
        assert attrs.provider == record.harness
        assert attrs.usage is not None
        assert attrs.started_at is not None
        assert attrs.ended_at is not None
        assert attrs.usage.duration_ms == record.duration_ms
        assert attrs.started_at.to_datetime() == record.started_at
        assert attrs.ended_at.to_datetime() == record.ended_at


@pytest.mark.regression
@given(events=journals())
@example(events=journal(1792599885, [(3, "model-a"), (7, "model-b")]))
def test_run_output_does_not_invent_turns(events: list[e.CoreEvent]) -> None:
    spans, records, legacy = project(events)
    turns = [span for span in spans.values() if span.label and span.label.kind == "agent_turn"]
    assert len(turns) == len(legacy.records)
    assert all(span.status == p.SpanStatus.DONE and span.ended_at is not None for span in turns)
    narration = [
        record.body.value
        for record in records
        if record.body and record.body.field == "stream" and not record.body.value.has_field("span")
    ]
    assert narration  # Run output is retained without fabricating an agent owner.


@pytest.mark.regression
@given(command=st.text(alphabet="command 123", min_size=1, max_size=30))
@example(command="measure")
def test_gate_enrichment_retains_command_and_runtime_label(command: str) -> None:
    observations = journal(1792599885, [(3, "model-a")])
    gate = next(
        event.data.span
        for event in observations
        if isinstance(event.data, e.SpanEndedData)
        and event.data.span.label is not None
        and event.data.span.label.kind == "gate"
    )
    state = MappingState("run-a", wall_span_clock=True, clock_epochs=((1792599885, 0),))
    for event in observations:
        if event.type is not e.CoreEventType.RUN_FINISHED:
            state = map_event(state, event).state
    for payload in (
        e.GateStartedData(gate=e.GateKind.BENCHMARK, span_id=gate.id, command=command),
        e.GateFinishedData(
            gate=e.GateKind.BENCHMARK, span_id=gate.id, metric="throughput", value=3
        ),
    ):
        event = e.CoreEvent.model_validate(
            {
                "type": payload.kind,
                "timestamp": observations[-1].timestamp,
                "run_id": "run-a",
                "data": payload,
            }
        )
        state = map_event(state, event).state
    mapped = p.Span.from_binary(dict(state.spans)[gate.id])
    assert mapped.label is not None
    assert mapped.label.attrs is not None
    attrs = p.GateAttrs.from_binary(mapped.label.attrs.value)
    assert attrs.command == command
    assert mapped.ended_at == 4


def usage_wire(event: e.CoreEvent, *, omit_nulls: bool) -> bytes:
    document = json.loads(event.model_dump_json())
    if omit_nulls and isinstance(event.data, e.TurnUsageData):
        document["data"] = {
            key: value for key, value in document["data"].items() if value is not None
        }
    return json.dumps(document, separators=(",", ":")).encode()


@pytest.fixture(scope="module")
def usage_source() -> tuple[MappingState, SummaryState, e.CoreEvent, e.CoreEvent]:
    events = journal(1700000000, [(1, "model-a"), (2, "model-b"), (3, "model-a")])
    source = next(event for event in events if event.type == e.CoreEventType.TURN_USAGE_RECORDED)
    finish = events[-1]
    prefix = [event for event in events[:-1] if event.type != e.CoreEventType.TURN_USAGE_RECORDED]
    state = MappingState("run-a", wall_span_clock=True, clock_epochs=((1700000000, 0),))
    summary = RunSummaryFold()
    for sequence, event in enumerate(prefix, 1):
        wire = usage_wire(event.model_copy(update={"sequence": sequence}), omit_nulls=False)
        assert summary.consume(wire)
        state = map_event(state, e.CoreEvent.model_validate_json(wire)).state
    return state, summary.state(), source, finish


TOKEN_FIELDS = (
    ("input", "input_tokens", "input"),
    ("cache_read", "cache_read_input_tokens", "cache_read"),
    ("cache_creation", "cache_creation_input_tokens", "cache_write"),
    ("output", "output_tokens", "output"),
    ("reasoning", "reasoning_output_tokens", "reasoning"),
)


def assert_usage_sources(state: MappingState, summary: RunSummaryFold) -> None:
    spans = [p.Span.from_binary(value) for _, value in state.spans]
    usage = [
        p.AgentTurnAttrs.from_binary(span.label.attrs.value).usage
        for span in spans
        if span.label is not None
        and span.label.kind == "agent_turn"
        and span.label.attrs is not None
    ]
    for native, _, legacy in TOKEN_FIELDS:
        reported = [
            getattr(item, native) for item in usage if item is not None and item.has_field(native)
        ]
        assert (sum(reported) if reported else None) == getattr(summary.tokens, legacy)


@pytest.mark.regression
@given(
    increments=st.lists(
        st.tuples(
            st.integers(0, 2),
            st.dictionaries(
                st.sampled_from([source for _, source, _ in TOKEN_FIELDS]),
                st.one_of(st.none(), st.integers(0, 1000000)),
                max_size=5,
            ),
            st.booleans(),
        ),
        min_size=1,
        max_size=8,
    ),
    omit_nulls=st.booleans(),
    restart_at=st.integers(0, 8),
)
@example(
    increments=[
        (0, {"input_tokens": 424250, "output_tokens": 0}, True),
        (0, {"input_tokens": 857297, "output_tokens": 0}, True),
    ],
    omit_nulls=False,
    restart_at=1,
)
def test_list_and_page_token_totals_share_the_event_source(
    usage_source: tuple[MappingState, SummaryState, e.CoreEvent, e.CoreEvent],
    increments: list[tuple[int, dict[str, int | None], bool]],
    *,
    omit_nulls: bool,
    restart_at: int,
) -> None:
    state, checkpoint, source, finish = usage_source
    summary = RunSummaryFold.from_state(checkpoint)
    for index, (turn, reading, named_span) in enumerate(increments):
        if index == restart_at:
            summary = RunSummaryFold.from_state(summary.state())
        event = source.model_copy(
            update={
                "sequence": checkpoint.through_sequence + index + 1,
                "timestamp": finish.timestamp,
                "execution_id": None if named_span else f"turn-{turn}",
                "data": e.TurnUsageData.model_validate(
                    {"span_id": f"op:turn-{turn}" if named_span else None, **reading}
                ),
            }
        )
        wire = usage_wire(event, omit_nulls=omit_nulls)
        assert summary.consume(wire)
        state = map_event(state, e.CoreEvent.model_validate_json(wire)).state
        assert_usage_sources(state, summary)


def turn_input_tokens(state: MappingState) -> dict[str, int | None]:
    totals: dict[str, int | None] = {}
    for key, value in state.spans:
        span = p.Span.from_binary(value)
        if (
            span.label is not None
            and span.label.kind == "agent_turn"
            and span.label.attrs is not None
        ):
            usage = p.AgentTurnAttrs.from_binary(span.label.attrs.value).usage
            totals[key] = None if usage is None or not usage.has_field("input") else usage.input
    return totals


@pytest.mark.regression
@given(
    sources=st.dictionaries(
        st.integers(1000, 1100),
        st.tuples(st.integers(0, 2), st.integers(0, 100000)),
        min_size=1,
        max_size=6,
    ),
    deliveries=st.lists(st.integers(0, 5), min_size=1, max_size=14),
    restart_at=st.integers(0, 14),
)
@example(sources={1000: (0, 100)}, deliveries=[0, 0], restart_at=1)
def test_usage_counts_each_source_event_once_however_it_is_delivered(
    usage_source: tuple[MappingState, SummaryState, e.CoreEvent, e.CoreEvent],
    sources: dict[int, tuple[int, int]],
    deliveries: list[int],
    restart_at: int,
) -> None:
    """Repeated, reordered and replayed deliveries, and a restart from a saved state, add once."""
    state, _, template, finish = usage_source
    ordered = sorted(sources.items())

    def event(sequence: int, turn: int, tokens: int) -> e.CoreEvent:
        return template.model_copy(
            update={
                "sequence": sequence,
                "timestamp": finish.timestamp,
                "execution_id": None,
                "data": e.TurnUsageData(span_id=f"op:turn-{turn}", input_tokens=tokens),
            }
        )

    stream = [ordered[index % len(ordered)] for index in deliveries]
    stream += ordered  # every source arrives at least once
    saved = state
    for position, (sequence, (turn, tokens)) in enumerate(stream):
        if position == restart_at:
            saved = state
        state = map_event(state, event(sequence, turn, tokens)).state
    # A restart resumes from the saved state and sees the whole stream again, as a replay does.
    for sequence, (turn, tokens) in stream:
        saved = map_event(saved, event(sequence, turn, tokens)).state
    expected: dict[str, int] = {}
    for _, (turn, tokens) in ordered:
        expected[f"op:turn-{turn}"] = expected.get(f"op:turn-{turn}", 0) + tokens
    base = turn_input_tokens(usage_source[0])
    for each in (state, saved):
        totals = turn_input_tokens(each)
        for span, total in expected.items():
            assert totals[span] == total + (base.get(span) or 0)


@pytest.mark.regression
@given(count=st.integers(16, 32), ahead=st.lists(st.integers(1, 31), max_size=10))
@example(count=16, ahead=[15, 2, 15])
def test_ordered_usage_retains_bounded_replay_state(
    usage_source: tuple[MappingState, SummaryState, e.CoreEvent, e.CoreEvent],
    count: int,
    ahead: list[int],
) -> None:
    """Reordering, repetition and closing every sequence gap compact back to one frontier."""
    _, _, template, _ = usage_source
    state = MappingState("run-a")
    source = template.model_copy(
        update={
            "execution_id": "scale-turn",
            "data": e.TurnUsageData(span_id="op:scale-turn", input_tokens=1),
        }
    )
    first = 1
    state = map_event(state, source.model_copy(update={"sequence": first})).state
    retained = len(repr(state))
    for offset in ahead:
        state = map_event(
            state, source.model_copy(update={"sequence": first + offset % count})
        ).state
    for sequence in range(first + 1, first + count):
        state = map_event(state, source.model_copy(update={"sequence": sequence})).state
    assert turn_input_tokens(state)["op:scale-turn"] == count
    # The public immutable cursor includes fixed span snapshots. With the same span set, its
    # retained representation is bounded independently of the number of ordered usage events.
    assert len(repr(state)) <= retained + 64
