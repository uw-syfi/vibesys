"""Prefix validation is pure and independent of the recorder or storage implementation."""

from copy import deepcopy
from hashlib import sha256
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf import Oneof
from protobuf.wkt.google.protobuf.any_pb import Any
from protobuf.wkt.google.protobuf.descriptor_pb import FileDescriptorProto, FileDescriptorSet
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp
from scripts.generate_record_goldens import record_examples

from vs_core.api.replay import (
    AcceptedReplay,
    PolicyDrift,
    ReplayArchive,
    ReplayError,
    ReplayFrontier,
    ReplayIdentityError,
    ReplayScheduler,
    SemanticKey,
    replay_descriptors,
)
from vs_proto.api import (
    Change,
    Ended,
    EpochStarted,
    Lifecycle,
    Notice,
    NoticeCode,
    Outcome,
    Record,
    ReplayStore,
    Running,
    RuntimeType,
    Severity,
    Span,
    SpanRecord,
    SpanStatus,
    Stop,
    ValidationError,
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
                replay_acknowledgements=[
                    replay.ReplayAcknowledgement(launch_request_id="launch-a")
                ],
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
    "field", ["initial_allowance", "replay_acknowledgements", "identity_encoding_version"]
)
def test_missing_epoch_reconstruction_metadata_is_rejected(
    field: Literal["initial_allowance", "replay_acknowledgements", "identity_encoding_version"],
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
    (ack,) = epoch_of(epoch).replay_acknowledgements
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


def test_confirmed_withdrawal_closes_an_unanswered_start_without_an_acceptance() -> None:
    header, started, _, _ = records()
    stopped = deepcopy(started)
    stopped.seq = 3
    span_record(stopped).change = Change.ENDED
    span = span_of(stopped)
    span.status, span.ended_at = SpanStatus.STOPPED, 0
    span.stop = Stop(requested_at=0, landed=True)
    archive = decode([header, started, stopped])
    assert archive.unresolved == ()
    assert archive.history == ()
    late = deepcopy(stopped)
    late.seq = 4
    span_record(late).change = Change.UPDATED
    span_of(late).stop = Stop(requested_at=0, landed=False)
    with pytest.raises(ReplayError, match="execution metadata"):
        archive.append(late)


@pytest.mark.parametrize("change", [Change.UPDATED, Change.ENDED])
@pytest.mark.parametrize("landed", [None, False])
def test_unanswered_start_cannot_close_without_confirmed_withdrawal(
    change: Change, *, landed: bool | None
) -> None:
    header, started, _, _ = records()
    stopped = deepcopy(started)
    stopped.seq = 3
    span_record(stopped).change = change
    span = span_of(stopped)
    span.status, span.ended_at = SpanStatus.STOPPED, 0
    if landed is not None:
        span.stop = Stop(requested_at=0, landed=landed)
    with pytest.raises(ReplayError, match="confirmed withdrawal"):
        decode([header, started, stopped])


def test_epoch_declarations_grow_without_opening_an_epoch_or_acknowledging_again() -> None:
    header = records()[0]
    amended = deepcopy(header)
    amended.seq = 2
    schemas = replay_descriptors(Reply)
    epoch_of(amended).descriptors = schemas
    archive = decode([header, amended])
    assert archive.epoch == 1
    assert len(archive.epochs) == 1
    assert len(archive.acknowledgements.acknowledgements) == 1
    assert archive.current_epoch.descriptors == schemas
    assert archive.initial_allowance.iterations == 10
    roundtrip = decode(ReplayStore.from_binary(archive.encode()).records)
    assert roundtrip.current_epoch.descriptors == schemas
    assert roundtrip.encode() == archive.encode()


@pytest.mark.parametrize("edit", ["allowance", "remove", "replace"])
def test_epoch_amendments_cannot_replace_execution_or_retained_schemas(edit: str) -> None:
    header = records()[0]
    epoch_of(header).descriptors = replay_descriptors(Reply)
    amended = deepcopy(header)
    amended.seq = 2
    epoch = epoch_of(amended)
    if edit == "allowance":
        assert epoch.initial_allowance is not None
        epoch.initial_allowance.iterations += 1
    elif edit == "remove":
        epoch.descriptors = None
    else:
        assert epoch.descriptors is not None
        epoch.descriptors.file[0].package = "changed"
    with pytest.raises(ReplayError, match=r"execution metadata|cannot be replaced or removed"):
        decode([header, amended])


def test_descriptor_annotation_extensions_survive_but_unknown_record_metadata_is_rejected() -> None:
    header = records()[0]
    wire = FileDescriptorProto(name="historical.proto").to_binary() + b"\xa0\x06\x01"
    epoch_of(header).descriptors = FileDescriptorSet(file=[FileDescriptorProto.from_binary(wire)])
    archive = decode([header])
    retained = archive.current_epoch.descriptors
    assert retained is not None
    assert retained.file[0].to_binary() == wire
    unknown = Record.from_binary(header.to_binary() + b"\xa0\x06\x01")
    with pytest.raises(ReplayError, match="unknown envelope fields"):
        decode([unknown])


def test_duplicate_epoch_descriptor_names_fail_validation() -> None:
    header = records()[0]
    epoch_of(header).descriptors = FileDescriptorSet(
        file=[FileDescriptorProto(name="same.proto"), FileDescriptorProto(name="same.proto")]
    )
    with pytest.raises(ValidationError, match="invalid ReplayStore"):
        decode([header])


_NATIVE_RECORDS = record_examples()
_CORE_RECORDS = frozenset(
    {"core_input", "step_started", "step_finished", "attempt_interrupted", "run_ended"}
)
"""The records of the recovery rule: a keyed replay log never holds them, and the archive refuses them."""
_NON_POLICY_RECORDS = tuple(
    name
    for name, record in _NATIVE_RECORDS.items()
    if record.body is not None
    and record.body.field not in {"epoch_started", "legacy_event"} | _CORE_RECORDS
)


def test_shared_record_corpus_covers_every_archive_body_family() -> None:
    descriptor = Record.desc().proto
    index = next(i for i, oneof in enumerate(descriptor.oneof_decl) if oneof.name == "body")
    declared = {
        field.name
        for field in descriptor.field
        if field.has_field("oneof_index") and field.oneof_index == index
    }
    assert declared == {record.body.field for record in _NATIVE_RECORDS.values() if record.body}
    for name in _CORE_RECORDS:
        refused = deepcopy(_NATIVE_RECORDS[name])
        refused.seq, refused.clock = 2, 0
        with pytest.raises(ReplayError, match="unsupported record"):
            decode([records()[0]]).append(refused)
    archive = decode([records()[0]])
    for sequence, name in enumerate(_NON_POLICY_RECORDS, 2):
        record = deepcopy(_NATIVE_RECORDS[name])
        record.seq, record.clock = sequence, 0
        if record.body is not None and record.body.field == "declaration_amended":
            # A declaration belongs to this archive's immutable epoch identity;
            # the shared standalone corpus header names a different run.
            record.body = Oneof("declaration_amended", archive.current_epoch)
        archive.append(record)
    assert archive.history == ()
    assert archive.unresolved == ()
    assert decode(ReplayStore.from_binary(archive.encode()).records).encode() == archive.encode()


@pytest.mark.regression
@given(names=st.lists(st.sampled_from(_NON_POLICY_RECORDS), min_size=1, max_size=12))
@example(names=["chat_message"])
def test_non_policy_native_histories_never_become_replay_operations(names: list[str]) -> None:
    archive = decode([records()[0]])
    for sequence, name in enumerate(names, 2):
        record = deepcopy(_NATIVE_RECORDS[name])
        record.seq, record.clock = sequence, 0
        if record.body is not None and record.body.field == "declaration_amended":
            # A declaration belongs to this archive's immutable epoch identity;
            # the shared standalone corpus header names a different run.
            record.body = Oneof("declaration_amended", archive.current_epoch)
        archive.append(record)
        assert archive.history == ()
        assert archive.unresolved == ()
    assert archive.sequence == len(names) + 1


def closed_prefix(length: int) -> list[Record]:
    prefix = records()[:length]
    prefix.append(
        Record(
            seq=length + 1,
            epoch=1,
            at=Timestamp(seconds=1),
            clock=0,
            body=Oneof(
                "lifecycle",
                Lifecycle(
                    state=Oneof(
                        "ended",
                        Ended(outcome=Outcome.INTERRUPTED, resumable=True, summary="Interrupted."),
                    )
                ),
            ),
        )
    )
    return prefix


@pytest.mark.parametrize("length", [1, 2, 3])
def test_ended_epoch_rejects_new_execution_facts(length: int) -> None:
    prefix = closed_prefix(length)
    late = records()[length]
    late.seq += 1
    with pytest.raises(ReplayError, match="closed epoch"):
        decode([*prefix, late])


def test_running_presentation_cannot_reopen_an_ended_epoch() -> None:
    prefix = closed_prefix(4)
    prefix.append(
        Record(
            seq=6,
            epoch=1,
            at=Timestamp(seconds=1),
            clock=0,
            body=Oneof("lifecycle", Lifecycle(state=Oneof("running", Running()))),
        )
    )
    label = deepcopy(prefix[3])
    label.seq = 7
    span_record(label).change = Change.UPDATED
    span_of(label).name = "A later presentation label"
    archive = decode([*prefix, label])
    assert archive.epoch_closed
    assert len(archive.history) == 1


@pytest.mark.parametrize("length", [2, 3])
def test_clock_and_operation_observations_cannot_share_a_record(length: int) -> None:
    prefix = records()[: length + 1]
    prefix[-1].replay_clock = replay.ReplayClock(allowance=replay.ReplayAllowance(iterations=10))
    with pytest.raises(ReplayError, match=r"policy observation|pending observation"):
        decode(prefix)


def notice_record(pending: str = "future", seq: int = 5) -> Record:
    notice = ReplayFrontier(
        (AcceptedReplay.from_record(records()[-1]),),
        (SemanticKey(pending),),
        PolicyDrift(0, (), ()),
    ).notice()
    assert notice is not None
    return Record(
        seq=seq,
        epoch=1,
        at=Timestamp(seconds=1),
        clock=0,
        body=Oneof(
            "notice",
            Notice(
                severity=Severity.WARNING,
                code=NoticeCode.FRAMEWORK_WARNING,
                message="Unused paid results.",
                detail=Any(type_url=notice.desc().type_name, value=notice.to_binary()),
            ),
        ),
    )


def test_notice_retains_the_exact_accepted_outcomes() -> None:
    record = notice_record()
    archive = decode([*records(), record])
    assert len(archive.acknowledgements.notices) == 1
    assert len(archive.acknowledgements.resume("next").disclosed()) == 1


def resume_epoch(
    seq: int,
    batch: list[replay.ReplayAcknowledgement],
    legacy: replay.ReplayAcknowledgement | None = None,
) -> Record:
    epoch = deepcopy(records()[0])
    epoch.seq, epoch.epoch, epoch_of(epoch).epoch = seq, 2, 2
    epoch_of(epoch).replay_acknowledgements = batch
    epoch_of(epoch).replay_acknowledgement = legacy
    return epoch


@pytest.mark.regression
@given(count=st.integers(min_value=0, max_value=3))
@example(count=2)  # The earlier rule acknowledged only the newest outstanding notice.
def test_resume_acknowledges_every_outstanding_notice_in_one_batch(count: int) -> None:
    prefix = [*records(), *(notice_record(f"future-{n}", 5 + n) for n in range(count))]
    archive = decode(prefix)
    ids = [replay.ReplayNotice.from_binary(value).id for value in archive.acknowledgements.notices]
    resumed = archive.acknowledgements.resume("launch-b")
    batch = list(resumed.batch("launch-b"))
    assert [ack.notice_id if ack.has_field("notice_id") else None for ack in batch] == (
        ids or [None]
    )
    assert resumed.resume("launch-b") == resumed
    assert resumed.resume("launch-c").batch("launch-c") == (
        replay.ReplayAcknowledgement(launch_request_id="launch-c"),
    )
    # Every notice here discloses the same accepted result, so one occurrence is disclosed.
    assert len(resumed.disclosed()) == min(count, 1)
    assert decode([*prefix, resume_epoch(5 + count, batch)]).acknowledgements == resumed


@pytest.mark.regression
@given(count=st.integers(min_value=2, max_value=4), data=st.data())
@example(count=2, data=None)
def test_an_epoch_cannot_acknowledge_part_or_a_reordering_of_its_notices(
    count: int, data: st.DataObject | None
) -> None:
    prefix = [*records(), *(notice_record(f"future-{n}", 5 + n) for n in range(count))]
    batch = list(decode(prefix).acknowledgements.resume("launch-b").batch("launch-b"))
    if data is None:
        changed = batch[-1:]
    else:
        changed = data.draw(
            st.permutations(batch).flatmap(
                lambda order: st.lists(
                    st.sampled_from(order), min_size=1, max_size=len(order), unique_by=id
                )
            )
        )
    if [ack.notice_id for ack in changed] == [ack.notice_id for ack in batch]:
        return
    with pytest.raises(ReplayError, match="contradicts its outstanding notice acknowledgements"):
        decode([*prefix, resume_epoch(5 + count, changed)])


def test_an_epoch_records_its_batch_in_one_field_and_still_decodes_the_single_one() -> None:
    prefix = [*records(), notice_record()]
    (ack,) = decode(prefix).acknowledgements.resume("launch-b").batch("launch-b")
    legacy = decode([*prefix, resume_epoch(6, [], legacy=ack)])
    assert legacy.acknowledgements.batch("launch-b") == (ack,)
    with pytest.raises(ValidationError) as raised:
        decode([*prefix, resume_epoch(6, [ack], legacy=ack)])
    assert [item.proto.rule_id for item in raised.value.violations] == [
        "epoch.replay_acknowledgements"
    ]


@pytest.mark.regression
@pytest.mark.parametrize("change", ["unknown", "duplicate", "id", "cost"])
@given(value=st.integers(min_value=0, max_value=100))
@example(value=1)
def test_notice_cannot_invent_or_change_its_accepted_history(change: str, value: int) -> None:
    record = notice_record()
    assert record.body is not None
    assert record.body.field == "notice"
    detail = record.body.value.detail
    assert detail is not None
    notice = replay.ReplayNotice.from_binary(detail.value)
    if change == "unknown":
        notice.unused[0].accepted_sequence = value + 5
    elif change == "duplicate":
        notice.unused.append(deepcopy(notice.unused[0]))
    elif change == "id":
        notice.id = str(value)
    else:
        notice.unused[0].cost_usd = float(value)
    detail.value = notice.to_binary()
    with pytest.raises(ReplayError, match="replay notice"):
        decode([*records(), record])


@pytest.mark.parametrize("kind", ["disclosure", "drift"])
def test_unknown_framework_notice_metadata_is_rejected(kind: str) -> None:
    record = notice_record()
    assert record.body is not None
    assert record.body.field == "notice"
    detail = record.body.value.detail
    assert detail is not None
    if kind == "drift":
        drift = replay.ReplayPolicyDrift(results=1, roles=["implementer"])
        detail.type_url, detail.value = drift.desc().type_name, drift.to_binary()
    detail.value += b"\xa0\x06\x01"
    with pytest.raises(ReplayError, match="unknown replay metadata"):
        decode([*records(), record])


@pytest.mark.regression
@given(content=st.binary(max_size=64))
@example(content=b"x")
def test_unanswered_start_requires_a_well_formed_identity(content: bytes) -> None:
    prefix = records()[:2]
    operation = operation_of(prefix[-1])
    operation.identity = b"invalid" + content
    operation.identity_digest = sha256(operation.identity).digest()
    with pytest.raises(ReplayIdentityError, match="identity encoding"):
        decode(prefix)


@pytest.mark.parametrize("field", ["identity_digest", "dependencies"])
def test_unanswered_start_rejects_contradictory_identity_proofs(field: str) -> None:
    prefix = records()[:2]
    operation = operation_of(prefix[-1])
    setattr(operation, field, b"x" * 32)
    with pytest.raises((ReplayError, ReplayIdentityError), match="contradicts"):
        decode(prefix)
