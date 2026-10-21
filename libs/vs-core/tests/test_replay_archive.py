"""Prefix validation is pure and independent of the recorder or storage implementation."""

from copy import deepcopy

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

from .test_keyed_replay import accepted, settings
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
    "change", ["identity", "name", "parent", "start", "observation", "cost", "paid", "acceptance"]
)
def test_execution_facts_cannot_be_rewritten(change: str) -> None:
    prefix = records()
    record = prefix[-1]
    span = record.body.value.span
    assert span is not None
    assert span.replay is not None
    if change == "identity":
        span.replay.identity += b"changed"
    elif change == "name":
        span.name = "changed"
    elif change == "parent":
        span.parent = "another-parent"
    elif change == "start":
        record.body.value.change = Change.STARTED
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
    completed.body.value.span.replay.accepted_sequence = 3
    with pytest.raises(ReplayError, match="previously durable observation"):
        decode([header, started, completed])


def test_repeated_acceptance_and_sequence_gaps_are_rejected() -> None:
    prefix = records()
    repeated = deepcopy(prefix[-1])
    repeated.seq = 5
    repeated.body.value.span.replay.accepted_sequence = 5
    with pytest.raises(ReplayError, match="complete twice"):
        decode([*prefix, repeated])
    prefix[-1].seq = 5
    with pytest.raises(ReplayError, match="contiguous sequence"):
        decode(prefix)


@pytest.mark.parametrize(
    "field", ["initial_allowance", "replay_acknowledgement", "identity_encoding_version"]
)
def test_missing_epoch_reconstruction_metadata_is_rejected(field: str) -> None:
    prefix = records()[:1]
    prefix[0].body.value.clear_field(field)
    with pytest.raises(ReplayError, match="keyed epoch needs"):
        decode(prefix)


def test_resume_cannot_replace_the_run_identity_or_repeat_its_launch() -> None:
    prefix = records()
    epoch = deepcopy(prefix[0])
    epoch.seq, epoch.epoch, epoch.body.value.epoch = 5, 2, 2
    with pytest.raises(ReplayError, match="repeats a launch"):
        decode([*prefix, epoch])
    epoch.body.value.run_id = "run-b"
    epoch.body.value.replay_acknowledgement.launch_request_id = "launch-b"
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
