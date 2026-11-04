"""Checkpoint facts preserve every semantic record fold at arbitrary writer boundaries."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from protobuf.wkt.google.protobuf.any_pb import Any

from vs_proto.api import (
    AgentTurnAttrs,
    BlobAttached,
    BlobRef,
    Change,
    ChatMessage,
    ChatRecord,
    ChatRole,
    EpochStarted,
    IntentAction,
    IntentState,
    IntentStep,
    Keyframe,
    Label,
    MetricPoint,
    Oneof,
    Record,
    RuntimeType,
    Span,
    SpanLink,
    SpanRecord,
    SpanStatus,
    Timestamp,
    ValidationError,
    validate,
)
from vs_runlog.api import fold_skeleton


def _fact(kind: str, seq: int) -> Oneof:
    if kind == "blob":
        return Oneof(
            "blob",
            BlobAttached(
                span="turn", role="reply", blob=BlobRef(sha256="a" * 64, media_type="text/plain")
            ),
        )
    if kind == "chat":
        return Oneof(
            "chat",
            ChatRecord(
                event=Oneof(
                    "message",
                    ChatMessage(
                        request_id=f"request-{seq}",
                        role=ChatRole.USER,
                        content=Oneof("text", "question"),
                    ),
                )
            ),
        )
    return Oneof(
        "span",
        SpanRecord(
            change=Change.STARTED,
            span=Span(
                id="turn",
                name="turn",
                runtime_type=RuntimeType.OPERATION,
                status=SpanStatus.RUNNING,
                label=Label(
                    kind="agent_turn",
                    links=[SpanLink(rel="steer", to="intent-1")],
                    attrs=Any(
                        type_url="type.googleapis.com/vibesys.store.v1.AgentTurnAttrs",
                        value=AgentTurnAttrs(
                            agent="agent-a", role="executor", stage="execute"
                        ).to_binary(),
                    ),
                ),
            ),
        ),
    )


def _records(kinds: list[str]) -> list[Record]:
    records = []
    epoch = 1
    for seq, kind in enumerate(kinds, 1):
        if kind == "epoch":
            epoch += 1
            body = Oneof("epoch_started", EpochStarted(run_id="run-a", epoch=epoch))
        elif kind == "amendment":
            body = Oneof("declaration_amended", EpochStarted(run_id="run-a", epoch=epoch))
        elif kind in {"blob", "chat", "link"}:
            body = _fact(kind, seq)
        elif kind == "point":
            body = Oneof("metric", MetricPoint(series="work", value=float(seq)))
        else:
            refused = kind == "refused"
            body = Oneof(
                "intent",
                IntentState(
                    control_id=f"intent-{seq % 3}",
                    action=IntentAction(1 + seq % 10),
                    step=IntentStep.REFUSED if refused else IntentStep.APPLIED,
                    reason="epoch closed" if refused else None,
                ),
            )
        records.append(Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), body=body))
    return records


@pytest.mark.regression
@example(kinds=["link"], cut=0)
@example(kinds=["applied", "refused", "epoch", *(["point"] * 600)], cut=600)
@given(
    kinds=st.lists(
        st.sampled_from(
            ["applied", "refused", "epoch", "amendment", "point", "blob", "chat", "link"]
        ),
        min_size=1,
        max_size=800,
    ),
    cut=st.integers(min_value=0, max_value=800),
)
def test_every_checkpoint_cut_retains_intents_and_original_epoch_starts(
    kinds: list[str], cut: int
) -> None:
    records = _records(kinds)
    cut = min(cut, len(records))
    full = fold_skeleton(records, log_offset=8)
    prefix = fold_skeleton(records[:cut], log_offset=8)
    restored = fold_skeleton(records[cut:], log_offset=8, seed=prefix)
    assert restored == full
    assert full.records_tracked
    assert full.record_facts == [
        r for r in records if r.body is not None and r.body.field in {"blob", "chat", "span"}
    ]
    assert full.intents == [r for r in records if r.body is not None and r.body.field == "intent"]
    assert [
        (v.seq, v.epoch.epoch, v.started_at)
        for v in full.epochs
        if v.starts_epoch and v.epoch is not None
    ] == [
        (r.seq, r.epoch, r.at)
        for r in records
        if r.body is not None and r.body.field == "epoch_started"
    ]
    validate(full)


def test_an_old_checkpoint_never_claims_complete_intent_history_after_a_seeded_fold() -> None:
    records = _records(["applied", "refused"])
    restored = fold_skeleton(records[1:], log_offset=8, seed=Keyframe(through_seq=1, log_offset=8))
    assert not restored.records_tracked
    assert restored.intents == records[1:]


@pytest.mark.parametrize("kind", ["future", "wrong_body", "duplicate"])
def test_invalid_checkpoint_record_facts_are_rejected(kind: str) -> None:
    record = _records(["applied"])[0]
    frame = Keyframe(through_seq=1, log_offset=8, records_tracked=True, intents=[record])
    if kind == "future":
        frame.through_seq = 1
        record.seq = 2
    elif kind == "wrong_body":
        record.body = Oneof("metric", MetricPoint(series="work", value=1.0))
    else:
        frame.intents.append(record)
    with pytest.raises(ValidationError):
        validate(frame)
