"""Journal source-of-truth coverage and pure typed-record projection."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.event_models import payload_models, payloads

import vibesys.events as e
import vs_proto.api as p
from vibesys.records.api import DISPOSITIONS, MappedEvent, MappingState, map_event

_AT = datetime(2026, 1, 1, tzinfo=UTC)
_ROOT = Path(__file__).resolve().parents[3]


def test_every_event_kind_has_exactly_one_documented_disposition() -> None:
    assert set(DISPOSITIONS) == set(e.CoreEvent.model_fields["type"].annotation)
    assert all(DISPOSITIONS.values())
    payload_kinds = {
        get_args(model.model_fields["kind"].annotation)[0]
        for model in get_args(get_args(e.CoreEventData)[0])
    }
    assert payload_kinds - {"phase", "steer_delivery"} <= {kind.value for kind in DISPOSITIONS}


def event(
    kind: e.CoreEventType, data: e.CoreEventData | None = None, **fields: object
) -> e.CoreEvent:
    return e.CoreEvent.model_validate(
        dict(type=kind, data=data, timestamp=_AT, run_id="run-a", sequence=1, **fields)
    )


def assert_valid(result: MappedEvent) -> None:
    for record in result.records:
        p.validate(record)
        decoded = p.Record.from_binary(record.to_binary())
        p.validate(decoded)
        assert decoded == record


@pytest.mark.parametrize(
    "path", sorted((_ROOT / "tests/conformance/events").glob("*.json")), ids=lambda p: p.stem
)
def test_current_event_corpus_maps_to_typed_records(path: Path) -> None:
    raw = json.loads(path.read_text())
    if raw["type"] not in e.CoreEventType:
        pytest.skip("server-only event, never written by the core journal")
    fields = {name: value for name, value in raw.items() if name in e.CoreEvent.model_fields}
    observation = e.CoreEvent.model_validate(fields)
    if (
        observation.data is None
        and DISPOSITIONS[observation.type] == "typed record"
        and observation.type is not e.CoreEventType.RUN_FINISHED
    ):
        pytest.skip("wire corpus omits semantic payload; generated union test covers it")
    state = MappingState("run-a")
    if isinstance(
        observation.data,
        (e.QuotaWaitingData, e.QuotaWaitCancelledData, e.QuotaResumedData, e.QuotaAbandonedData),
    ):
        before = observation.model_copy(
            update={
                "type": e.CoreEventType.QUOTA_PAUSED,
                "data": e.QuotaPausedData(
                    provider=observation.data.provider,
                    condition="quota_exhausted",
                    detail="capacity",
                ),
            }
        )
        state = map_event(state, before).state
    assert_valid(map_event(state, observation))


@given(text=st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=100))
def test_transcript_entries_round_trip_and_state_is_pure(text: str) -> None:
    state = MappingState("run-a")
    observation = event(
        e.CoreEventType.AGENT_OUTPUT_CHUNK,
        e.AgentOutputChunkData(channel="assistant", content=text),
        execution_id="turn-a",
    )
    first = map_event(state, observation)
    second = map_event(state, observation)
    assert first == second
    assert state.sequence == 0
    assert state.streams == ()
    assert_valid(first)
    assert len(first.streams) == 1
    wire = first.streams[0].content
    size = 0
    shift = 0
    for index, byte in enumerate(wire):
        size |= (byte & 127) << shift
        if byte < 128:
            entry = p.TranscriptEntry.from_binary(wire[index + 1 :])
            break
        shift += 7
    assert size == len(wire) - index - 1
    p.validate(entry)
    assert entry.entry is not None
    assert entry.entry.field == "text"
    assert entry.entry.value.content is not None
    assert entry.entry.value.content.value is not None
    assert entry.entry.value.content.value.value == text


_TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), min_size=1, max_size=16)
_COUNT = st.integers(min_value=1, max_value=100)
_OVERRIDES = {
    **dict.fromkeys(
        (
            "outer_loop",
            "input",
            "provider",
            "from_provider",
            "to_provider",
            "to_model",
            "tool",
            "revision",
            "summary",
            "metric",
            "unit",
            "candidate_commit",
            "base_commit",
        ),
        _TEXT,
    ),
    **dict.fromkeys(("attempts", "candidate_samples", "base_samples"), _COUNT),
    "noise": st.floats(min_value=0, max_value=100, allow_nan=False),
}


def generated_payload(model: type[e.EventPayload]) -> st.SearchStrategy[e.EventPayload]:
    if model is e.WorkspaceRootProbeData:
        return st.one_of(
            st.builds(
                model,
                state=st.just("queued"),
                job_id=_TEXT,
                waited_seconds=st.floats(min_value=0, max_value=100, allow_nan=False),
            ),
            st.builds(
                model,
                state=st.just("retrying"),
                failure=_TEXT,
                attempt=st.integers(min_value=2, max_value=100),
                retry_in_seconds=st.just(0.0),
            ),
            st.builds(model, state=st.just("proven")),
            st.builds(model, state=st.just("failed"), failure=_TEXT),
        )
    overrides: dict[str, st.SearchStrategy[object]] = {
        name: strategy
        for name, strategy in _OVERRIDES.items()
        if name in model.model_fields and model.model_fields[name].is_required()
    }
    if model is e.QuotaAbandonedData:
        overrides["reason"] = _TEXT
    if model is e.AgentExecutionStartedData:
        overrides["stage"] = _TEXT
    if model is e.ComparisonsReadData:
        overrides["comparisons"] = st.lists(
            st.builds(
                e.ComparisonReadingData,
                name=_TEXT,
                label=_TEXT,
                status=st.sampled_from(e.ComparisonStatus),
            ),
            max_size=4,
        ).map(tuple)
    if model is e.SpanEndedData:
        overrides["span"] = st.just(
            e.SpanEndedData.model_validate(
                {
                    "span": {
                        "id": "flow:#1",
                        "parent": None,
                        "type": "flow",
                        "name": "root",
                        "status": "done",
                        "started_at": 0,
                        "ended_at": 1,
                    }
                }
            ).span
        )
    return payloads(model, **overrides)


@pytest.mark.parametrize("model", payload_models(e.CoreEventData), ids=lambda model: model.__name__)
@given(data=st.data())
def test_mapping_is_total_over_generated_semantic_payload_union(
    model: type[e.EventPayload], data: st.DataObject
) -> None:
    payload = data.draw(generated_payload(model))
    payload_kind = get_args(model.model_fields["kind"].annotation)[0]
    kind = {"phase": "phase_started", "steer_delivery": "steer_queued"}.get(
        payload_kind, payload_kind
    )
    observation = event(
        e.CoreEventType(kind),
        e.CoreEvent.model_validate({"type": kind, "timestamp": _AT, "data": payload}).data,
        execution_id="turn-a",
        round_label="round-1",
    )
    state = MappingState("run-a")
    if isinstance(
        payload,
        (e.QuotaResumedData, e.QuotaWaitingData, e.QuotaWaitCancelledData, e.QuotaAbandonedData),
    ):
        before = observation.model_copy(
            update={
                "type": e.CoreEventType.QUOTA_PAUSED,
                "data": e.QuotaPausedData(
                    provider=payload.provider, condition="quota_exhausted", detail="wait"
                ),
            }
        )
        state = map_event(state, before).state
    result = map_event(state, observation)
    assert_valid(result)
    assert [record.seq for record in result.records] == list(
        range(state.sequence + 1, result.state.sequence + 1)
    )


def native_span(
    kind: str, attrs: dict[str, object], *, invocation: str | None = None, ended: bool = False
) -> e.CoreEvent:
    source = e.SpanStartedData.model_validate(
        {
            "span": {
                "id": "op:work",
                "parent": None,
                "type": "operation",
                "name": "ask",
                "status": "done" if ended else "running",
                "started_at": 0,
                "ended_at": 3 if ended else None,
                "invocation": invocation,
                "label": {"kind": kind, "attrs_json": json.dumps(attrs)},
            }
        }
    ).span
    return event(
        e.CoreEventType.SPAN_ENDED if ended else e.CoreEventType.SPAN_STARTED,
        e.SpanEndedData(span=source) if ended else e.SpanStartedData(span=source),
    )


def last_span(result: MappedEvent) -> p.Span:
    body = next(
        record.body
        for record in reversed(result.records)
        if record.body is not None and record.body.field == "span"
    )
    assert body is not None
    assert body.field == "span"
    assert body.value.span is not None
    return body.value.span


@given(tokens=st.integers(min_value=0, max_value=100000), stage=_TEXT)
def test_whole_span_updates_keep_usage_and_runtime_enrichment(tokens: int, stage: str) -> None:
    source = native_span(
        "agent_turn", {"agent": "agent-a", "role": "implementer"}, invocation="turn-a"
    )
    state = map_event(MappingState("run-a"), source).state
    started = event(
        e.CoreEventType.AGENT_EXECUTION_STARTED,
        e.AgentExecutionStartedData(
            stage=stage, activity=e.AgentExecutionActivityData(mode="thinking", summary="working")
        ),
        execution_id="turn-a",
    )
    state = map_event(state, started).state
    state = map_event(
        state,
        event(
            e.CoreEventType.TURN_USAGE_RECORDED,
            e.TurnUsageData(input_tokens=tokens),
            execution_id="turn-a",
        ),
    ).state
    ended = native_span(
        "agent_turn", {"agent": "agent-a", "role": "implementer"}, invocation="turn-a", ended=True
    )
    result = map_event(state, ended)
    span = last_span(result)
    assert len(result.state.spans) == 1
    assert span.id == "op:work"
    assert span.ended_at == 3
    assert span.label is not None
    assert span.label.attrs is not None
    attrs = p.AgentTurnAttrs.from_binary(span.label.attrs.value)
    p.validate(attrs)
    assert attrs.stage == stage
    assert attrs.usage is not None
    assert attrs.usage.has_field("input")
    assert attrs.usage.input == tokens


@given(attempts=_COUNT)
def test_projected_round_facts_wait_for_and_replace_the_original_span(attempts: int) -> None:
    observation = event(
        e.CoreEventType.ROUND_FINISHED,
        e.RoundFinishedData(attempts=attempts, judge_verdict="pass", profile_skipped=False),
        round_label="round-1",
    )
    deferred = map_event(MappingState("run-a"), observation)
    assert not deferred.state.spans
    result = map_event(deferred.state, native_span("workstream", {"round": 1}, ended=True))
    snapshots = [
        record.body.value.span
        for record in result.records
        if record.body is not None and record.body.field == "span"
    ]
    assert snapshots[0] is not None
    assert snapshots[0].label is not None
    assert snapshots[0].label.attrs is not None
    assert not p.WorkstreamAttrs.from_binary(snapshots[0].label.attrs.value).has_field("attempts")
    assert not result.state.pending
    assert len(result.state.spans) == 1
    span = last_span(result)
    assert span.label is not None
    assert span.label.attrs is not None
    assert p.WorkstreamAttrs.from_binary(span.label.attrs.value).attempts == attempts
    assert span.ended_at == 3


@given(
    size=st.sampled_from([65535, 65536, 65537, 2**20 + 1]),
    character=st.sampled_from(["a", "\u2603"]),
)
def test_large_transcript_content_is_lossless_and_byte_bounded(size: int, character: str) -> None:
    text = character * (size // len(character.encode())) + "a" * (size % len(character.encode()))
    result = map_event(
        MappingState("run-a"),
        event(
            e.CoreEventType.AGENT_OUTPUT_CHUNK,
            e.AgentOutputChunkData(channel="assistant", content=text),
            execution_id="turn-a",
        ),
    )
    assert_valid(result)
    if size > 65536:
        assert any(blob.content == text.encode() for blob in result.blobs)
        assert all(
            len(record.to_binary()) <= p.RUN_LOG_MAX_RECORD_BYTES for record in result.records
        )
    else:
        assert not result.blobs


@given(size=st.sampled_from([65537, 2**20 + 1]))
def test_large_strategy_content_is_a_blob_and_never_truncated(size: int) -> None:
    text = "x" * size
    result = map_event(MappingState("run-a"), native_span("workstream", {"summary": text}))
    assert_valid(result)
    assert any(blob.content == text.encode() for blob in result.blobs)
    span = last_span(result)
    assert span.label is not None
    assert span.label.attrs is not None
    attrs = p.WorkstreamAttrs.from_binary(span.label.attrs.value)
    p.validate(attrs)
    assert attrs.summary == "blob:" + result.blobs[0].sha256


def test_a_record_above_the_cap_raises_instead_of_truncating() -> None:
    source = native_span("workstream", {})
    assert isinstance(source.data, e.SpanStartedData)
    raw = source.data.span.model_dump(mode="json")
    raw["waiting_on"] = [{"kind": "time", "ref": "x" * 1024, "since": 0} for _ in range(1100)]
    source = event(e.CoreEventType.SPAN_UPDATED, e.SpanUpdatedData.model_validate({"span": raw}))
    with pytest.raises(ValueError, match="exceeds"):
        map_event(MappingState("run-a"), source)
