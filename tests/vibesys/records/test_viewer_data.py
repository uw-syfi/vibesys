"""Differential viewer projections across generated, interleaved journal sequences."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vibesys.events as e
import vs_proto.api as p
from server.run_summary import RunSummaryFold
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


def project(events: list[e.CoreEvent]) -> tuple[dict[str, p.Span], list[p.Record], RunSummaryFold]:
    origin = events[0].timestamp.timestamp()
    state = MappingState("run-a", wall_span_clock=True, clock_epochs=((origin, 0),))
    records: list[p.Record] = []
    legacy = RunSummaryFold()
    for event in events:
        assert legacy.consume(event.model_dump_json().encode())
        result = map_event(state, event)
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
        span = spans[record.span_id]
        attrs = p.AgentTurnAttrs.from_binary(span.label.attrs.value)
        assert attrs.model == record.model
        assert attrs.provider == record.harness
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
        if isinstance(event.data, e.SpanEndedData) and event.data.span.label.kind == "gate"
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
    attrs = p.GateAttrs.from_binary(mapped.label.attrs.value)
    assert attrs.command == command
    assert mapped.ended_at == 4
