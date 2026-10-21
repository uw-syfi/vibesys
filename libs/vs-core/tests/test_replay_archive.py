"""Prefix validation is pure and independent of the recorder or storage implementation."""

from copy import deepcopy
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayArchive, ReplayError, ReplayScheduler
from vs_proto.api import (
    Change,
    EpochStarted,
    Record,
    ReplayStore,
    RuntimeType,
    Span,
    SpanRecord,
    SpanStatus,
    replay,
)

from .test_keyed_replay import Reply, accepted, settings
from .test_replay_boundaries import one


def records() -> list[Record]:
    scheduler = ReplayScheduler(one, (), settings())
    (operation,) = scheduler.dispatches()
    header = Record(
        seq=1,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof(
            "epoch_started",
            EpochStarted(
                run_id="run-a",
                epoch=1,
                identity_encoding_version=1,
                initial_allowance=replay.ReplayAllowance(iterations=10),
                replay_acknowledgement=replay.ReplayAcknowledgement(launch_request_id="launch-a"),
            ),
        ),
    )
    span = Span(
        id="operation-a",
        runtime_type=RuntimeType.OPERATION,
        name="one",
        status=SpanStatus.RUNNING,
        started_at=0,
        replay=operation,
    )
    started = Record(
        seq=2,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof("span", SpanRecord(change=Change.STARTED, span=deepcopy(span))),
    )
    outcome = accepted(deepcopy(operation), 4, "answer", clock=0).operation()
    span.replay_observation = replay.ReplayObservation(
        outcome=deepcopy(outcome.outcome), paid=True, cost_usd=0.5
    )
    observed = Record(
        seq=3,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof("span", SpanRecord(change=Change.UPDATED, span=deepcopy(span))),
    )
    span.replay = outcome
    span.status, span.ended_at = SpanStatus.DONE, 0
    completed = Record(
        seq=4,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof("span", SpanRecord(change=Change.ENDED, span=span)),
    )
    scheduler.close()
    return [header, started, observed, completed]


def decode(values: list[Record]) -> ReplayArchive:
    return ReplayArchive.decode(ReplayStore(records=values).to_binary())


def span_record(record: Record) -> SpanRecord:
    body = record.body
    assert body is not None
    assert isinstance(body.value, SpanRecord)
    return body.value


def span_of(record: Record) -> Span:
    span = span_record(record).span
    assert span is not None
    return span


def operation_of(record: Record) -> replay.OperationReplay:
    operation = span_of(record).replay
    assert operation is not None
    return operation


def epoch_of(record: Record) -> EpochStarted:
    body = record.body
    assert body is not None
    assert isinstance(body.value, EpochStarted)
    return body.value


@given(length=st.integers(min_value=0, max_value=4))
def test_all_complete_prefixes_roundtrip_with_disposable_indexes(length: int) -> None:
    prefix = records()[:length]
    archive = decode(prefix)
    assert archive.sequence == length
    assert archive.encode() == ReplayStore(records=prefix).to_binary()
    assert len(archive.history) == int(length == 4)
    assert len(archive.unresolved) == int(length in {2, 3})
    fork = archive.fork()
    if length < 4:
        fork.append(records()[length])
        assert archive.sequence == length
        assert fork.sequence == length + 1


@pytest.mark.parametrize(
    "change", ["identity", "parent", "start", "observation", "cost", "paid", "acceptance"]
)
def test_execution_facts_cannot_be_rewritten(change: str) -> None:
    prefix = records()
    record = prefix[-1]
    span = span_of(record)
    assert span is not None
    assert span.replay is not None
    if change == "identity":
        span.replay.identity += b"changed"
    elif change == "parent":
        span.parent = "another-parent"
    elif change == "start":
        span_record(record).change = Change.STARTED
        span.status = SpanStatus.RUNNING
        span.clear_field("ended_at")
        span.replay.clear_field("outcome")
        span.replay.clear_field("accepted_sequence")
    elif change == "observation":
        span.replay_observation = None
    elif change == "cost":
        span.replay.cost_usd = 10
    elif change == "paid":
        span.replay.paid = False
    elif change == "acceptance":
        span.replay.accepted_sequence = 3
    with pytest.raises((ReplayError, ValueError)):
        decode(prefix)


def test_an_acceptance_without_its_observation_is_rejected() -> None:
    header, started, _, completed = records()
    completed.seq = 3
    operation_of(completed).accepted_sequence = 3
    with pytest.raises(ReplayError, match="previously durable observation"):
        decode([header, started, completed])


def test_repeated_acceptance_and_sequence_gaps_are_rejected() -> None:
    prefix = records()
    repeated = deepcopy(prefix[-1])
    repeated.seq = 5
    operation_of(repeated).accepted_sequence = 5
    with pytest.raises(ReplayError, match="complete twice"):
        decode([*prefix, repeated])
    prefix[-1].seq = 5
    with pytest.raises(ReplayError, match="contiguous sequence"):
        decode(prefix)


@pytest.mark.parametrize(
    "field", ["initial_allowance", "replay_acknowledgement", "identity_encoding_version"]
)
def test_missing_epoch_reconstruction_metadata_is_rejected(
    field: Literal["initial_allowance", "replay_acknowledgement", "identity_encoding_version"],
) -> None:
    prefix = records()[:1]
    epoch_of(prefix[0]).clear_field(field)
    with pytest.raises(ReplayError, match="keyed epoch needs"):
        decode(prefix)


def test_resume_cannot_replace_the_run_identity_or_repeat_its_launch() -> None:
    prefix = records()
    epoch = deepcopy(prefix[0])
    epoch.seq, epoch.epoch, epoch_of(epoch).epoch = 5, 2, 2
    with pytest.raises(ReplayError, match="repeats a launch"):
        decode([*prefix, epoch])
    epoch_of(epoch).run_id = "run-b"
    ack = epoch_of(epoch).replay_acknowledgement
    assert ack is not None
    ack.launch_request_id = "launch-b"
    with pytest.raises(ReplayError, match="original run identity"):
        decode([*prefix, epoch])


def test_missing_and_retrograde_record_clocks_are_rejected() -> None:
    prefix = records()
    prefix[1].clear_field("clock")
    with pytest.raises(ReplayError, match=r"Record\.clock"):
        decode(prefix)
    prefix = records()
    prefix[1].clock = 2
    with pytest.raises(ReplayError, match="monotone"):
        decode(prefix)


def test_newer_envelope_fields_are_not_silently_destroyed_by_an_older_writer() -> None:
    wire = ReplayStore(records=records()).to_binary() + b"\xa0\x06\x01"
    with pytest.raises(ReplayError, match="newer reader"):
        ReplayArchive.decode(wire)


def test_presentation_updates_retain_the_original_acceptance_sequence() -> None:
    prefix = records()
    updated = deepcopy(prefix[-1])
    updated.seq = 5
    span_record(updated).change = Change.UPDATED
    span_of(updated).name = "An edited presentation label"
    archive = decode([*prefix, updated])
    assert len(archive.history) == 1
    assert archive.history[0].sequence == 4
    scheduler = ReplayScheduler(one, archive.history, settings())
    result = scheduler.result
    assert isinstance(result, Reply)
    assert result.root == "answer"
    scheduler.close()
    operation_of(updated).cost_usd = 50
    with pytest.raises(ReplayError, match="changed accepted execution metadata"):
        decode([*prefix, updated])


def test_flow_spans_are_retained_without_becoming_operations() -> None:
    prefix = records()
    flow = Record(
        seq=5,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof(
            "span",
            SpanRecord(
                change=Change.STARTED,
                span=Span(
                    id="root",
                    runtime_type=RuntimeType.FLOW,
                    name="root",
                    status=SpanStatus.RUNNING,
                    started_at=0,
                ),
            ),
        ),
    )
    archive = decode([*prefix, flow])
    assert archive.sequence == 5
    assert len(archive.history) == 1
    assert archive.unresolved == ()
    assert len(archive.spans) == 2


def test_a_presentation_update_cannot_erase_execution_metadata() -> None:
    prefix = records()
    updated = deepcopy(prefix[-1])
    updated.seq = 5
    span_record(updated).change = Change.UPDATED
    span_of(updated).replay = None
    with pytest.raises(ReplayError, match="execution metadata cannot be removed"):
        decode([*prefix, updated])
