"""Generate cross-language skeleton fixtures with independently specified projections."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from vs_proto.api import (
    RUN_LOG_MAGIC,
    BlobRef,
    Change,
    Direction,
    Ended,
    EpochStarted,
    EpochVersion,
    Keyframe,
    LegacyEvent,
    Lifecycle,
    LifecycleVersion,
    MetricHistory,
    MetricPoint,
    MetricSeriesDecl,
    MetricVersion,
    Objective,
    Oneof,
    Outcome,
    Record,
    RunDescription,
    RunHead,
    Running,
    RuntimeType,
    SeriesRole,
    Span,
    SpanHistory,
    SpanRecord,
    SpanStatus,
    SpanVersion,
    StreamKind,
    StreamRef,
    StreamState,
    Timestamp,
)
from vs_runlog.api import FileRunLogWriter, encode_frame


def scenarios() -> dict[str, tuple[list[Record], Keyframe]]:
    """Small streams cover replacement, histories, ignored bodies and epoch gaps."""
    series = MetricSeriesDecl(name="score", direction=Direction.MAX, role=SeriesRole.OBJECTIVE)
    epoch = EpochStarted(
        run_id="run-fixture",
        epoch=1,
        tree_hash="tree-fixture",
        vibesys_version="test",
        api_schema_hash="a" * 64,
        store_schema_hash="b" * 64,
        run=RunDescription(
            strategy="example",
            input="input-fixture",
            objective=BlobRef(sha256="a" * 64, media_type="text/plain"),
            objectives=[Objective(series="score", direction=Direction.MAX)],
        ),
        series=[series],
    )
    running = Lifecycle(state=Oneof("running", Running()))
    ended = Lifecycle(state=Oneof("ended", Ended(outcome=Outcome.FINISHED)))
    opened = Span(id="work", name="work", runtime_type=RuntimeType.FLOW, status=SpanStatus.RUNNING)
    closed = Span(
        id="work", name="work", runtime_type=RuntimeType.FLOW, status=SpanStatus.DONE, ended_at=5
    )
    baseline = MetricPoint(series="score", value=2)
    improved = MetricPoint(series="score", span="work", value=4, clock=5)
    stream = StreamRef(id="log", kind=StreamKind.LOG, state=StreamState.OPENED)
    sealed = StreamRef(id="log", kind=StreamKind.LOG, state=StreamState.ENDED, length=100)
    bodies = [
        Oneof("epoch_started", epoch),
        Oneof("lifecycle", running),
        Oneof("span", SpanRecord(change=Change.STARTED, span=opened)),
        Oneof("metric", baseline),
        Oneof("stream", stream),
        Oneof("metric", improved),
        Oneof("span", SpanRecord(change=Change.ENDED, span=closed)),
        Oneof("stream", sealed),
        Oneof("legacy_event", LegacyEvent(json='{"ignored":true}')),
        Oneof("lifecycle", ended),
    ]
    records = [
        Record(seq=i, epoch=1, at=Timestamp(seconds=i), body=b) for i, b in enumerate(bodies, 1)
    ]
    expected = Keyframe(
        through_seq=10,
        spans={
            "work": SpanHistory(
                versions=[SpanVersion(seq=3, span=opened), SpanVersion(seq=7, span=closed)]
            )
        },
        metrics=[
            MetricHistory(
                series="score",
                points=[MetricVersion(seq=4, point=baseline), MetricVersion(seq=6, point=improved)],
            )
        ],
        streams=[sealed],
        epochs=[EpochVersion(seq=1, epoch=epoch)],
        lifecycles=[
            LifecycleVersion(seq=2, lifecycle=running),
            LifecycleVersion(seq=10, lifecycle=ended),
        ],
    )
    next_epoch = EpochStarted.from_binary(epoch.to_binary())
    next_epoch.epoch = 2
    resumed = [
        *records,
        Record(seq=11, epoch=2, at=Timestamp(seconds=100), body=Oneof("epoch_started", next_epoch)),
        Record(seq=12, epoch=2, at=Timestamp(seconds=101), body=Oneof("lifecycle", running)),
    ]
    expected_resumed = Keyframe.from_binary(expected.to_binary())
    expected_resumed.through_seq = 12
    expected_resumed.epochs.append(EpochVersion(seq=11, epoch=next_epoch))
    expected_resumed.lifecycles.append(LifecycleVersion(seq=12, lifecycle=running))
    legacy = [Record(seq=1, epoch=1, body=Oneof("legacy_event", LegacyEvent(json="legacy")))]
    cases = {
        "core": (records, expected),
        "epochs": (resumed, expected_resumed),
        "legacy": (legacy, Keyframe(through_seq=1)),
    }
    for stream_records, keyframe in cases.values():
        keyframe.log_offset = len(RUN_LOG_MAGIC) + sum(len(encode_frame(r)) for r in stream_records)
    return cases


def fixtures() -> dict[str, bytes]:
    """The directory for each stream is also a gateway-readable Python writer output."""
    output: dict[str, bytes] = {}
    for name, (records, expected) in scenarios().items():
        output[f"{name}/records.json"] = (
            json.dumps([json.loads(r.to_json()) for r in records], indent=2).encode() + b"\n"
        )
        output[f"{name}/expected.pb"] = expected.to_binary()
        output[f"{name}/expected.json"] = expected.to_json().encode() + b"\n"
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with FileRunLogWriter(root / "run.log") as writer:
                for record in records:
                    writer.append(record)
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.name != "run.lock":
                    payload = path.read_bytes()
                    if path.name == "run.head":
                        head = RunHead.from_binary(payload)
                        head.clear_field("process")
                        payload = head.to_binary()
                    output[f"{name}/{path.relative_to(root)}"] = payload
    return output


if __name__ == "__main__":
    for name, payload in fixtures().items():
        path = Path(__file__).parent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
