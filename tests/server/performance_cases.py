"""Field-shaped run generators, stripped of project, host and path identities."""

import io
import json
from pathlib import Path

from protobuf.wkt.google.protobuf.any_pb import Any

from vs_proto.api import (
    BlobRef,
    Change,
    ComparisonAttrs,
    ComparisonVerdict,
    Direction,
    Ended,
    EpochStarted,
    GateAttrs,
    GateStage,
    GateSubject,
    InputMeasurement,
    Keyframe,
    Label,
    Lifecycle,
    MeasuredInput,
    MetricPoint,
    MetricSeriesDecl,
    Objective,
    Oneof,
    Outcome,
    Record,
    RunDescription,
    RunFailure,
    RunFailureKind,
    RuntimeType,
    SeriesRole,
    Span,
    SpanLink,
    SpanRecord,
    SpanStatus,
    Timestamp,
    WorkstreamAttrs,
)
from vs_runlog.api import RunLogReader, fold_skeleton

ROOT = Path(__file__).resolve().parents[2] / "proto/testdata/performance"
FIELD = json.loads((ROOT / "field-shapes.json").read_text())


def generated_run(shape: int, seed: int, direction: Direction) -> tuple[Keyframe, float]:
    """Vary field counts, missing readings, detached gates, failures, noise and worse results."""
    counts = FIELD["shapes"][shape]
    baseline = FIELD["inputs"][seed % len(FIELD["inputs"])]
    records: list[Record] = []

    def append(record: Record) -> None:
        record.seq = len(records) + 1
        record.epoch = 1
        record.at = Timestamp(seconds=record.seq)
        records.append(record)

    epoch = EpochStarted(
        run_id="run-a",
        epoch=1,
        tree_hash="tree",
        vibesys_version="test",
        api_schema_hash="a" * 64,
        store_schema_hash="b" * 64,
        run=RunDescription(
            strategy="example",
            input="input",
            objective=BlobRef(sha256="a" * 64, media_type="text/plain"),
            objectives=[Objective(series="score", direction=direction)],
            input_measurement=InputMeasurement(
                state=Oneof("measured", MeasuredInput(series="score", value=baseline, span="input"))
            ),
        ),
        series=[
            MetricSeriesDecl(
                name="score", unit="ops/s", direction=direction, role=SeriesRole.OBJECTIVE
            )
        ],
    )
    source = ROOT.parent / "retained/64c1a8b7/run.log"
    template = next(
        record.body.value
        for record in RunLogReader(io.BytesIO(source.read_bytes()))
        if record.body and record.body.field == "epoch_started" and record.body.value.run
    )
    epoch.descriptors = template.descriptors
    epoch.span_kinds = template.span_kinds
    append(Record(body=Oneof("epoch_started", epoch)))

    def span(
        key: str,
        attrs: GateAttrs | ComparisonAttrs | WorkstreamAttrs,
        *,
        parent: str | None = None,
        verdict: str = "passed",
        links: tuple[SpanLink, ...] = (),
    ) -> None:
        kind = attrs.desc().name.removesuffix("Attrs").lower()
        label = Label(
            kind=kind,
            title=key,
            verdict=verdict,
            links=list(links),
            attrs=Any(
                type_url="type.googleapis.com/" + attrs.desc().type_name, value=attrs.to_binary()
            ),
        )
        item = Span(
            id=key,
            name=key,
            runtime_type=RuntimeType.FLOW,
            status=SpanStatus.DONE,
            parent=parent,
            started_at=len(records),
            ended_at=len(records) + 1,
            label=label,
        )
        append(Record(body=Oneof("span", SpanRecord(change=Change.ENDED, span=item))))

    span("input", GateAttrs(stage=GateStage.MEASUREMENT, subject=GateSubject.INPUT))
    append(
        Record(
            body=Oneof("metric", MetricPoint(series="score", span="input", value=baseline, clock=1))
        )
    )
    ideas = counts["ideas"]
    for index in range(ideas):
        failed = index < counts["failed"] or (seed % 3 == 1 and index == 0)
        span(
            f"idea-{index}",
            WorkstreamAttrs(
                hypothesis_id=f"h-{index // 2 if seed % 5 == 0 else index}", revision=f"r-{index}"
            ),
            verdict="failed" if failed else "passed",
        )
        span(
            f"gate-{index}",
            GateAttrs(stage=GateStage.MEASUREMENT, subject=GateSubject.CANDIDATE),
            parent=f"idea-{index}",
        )
        verdict = (
            ComparisonVerdict.WITHIN_NOISE
            if index % 4 == 1
            else ComparisonVerdict.WORSE
            if index % 4 == 2
            else ComparisonVerdict.BETTER
        )
        span(
            f"comparison-{index}",
            ComparisonAttrs(metric="score", direction=direction, ratio=1.1, verdict=verdict),
            links=(SpanLink(rel="candidate", to=f"idea-{index}"),),
        )
    for index in range(counts["readings"]):
        owner = index % ideas if ideas else None
        if seed % 3 == 2 and owner is not None and (owner % 5 == 0):
            continue
        fraction = 1 + (index + 1) / 20
        if owner is not None and owner < counts["failed"]:
            fraction += 10
        if owner is not None and owner % 4 == 3:
            fraction = 0.5
        value = baseline / fraction if direction == Direction.MIN else baseline * fraction
        if seed == 4 and owner is not None and owner % 4 == 0:
            value = 0
        append(
            Record(
                body=Oneof(
                    "metric",
                    MetricPoint(
                        series="score",
                        span=f"gate-{owner}" if owner is not None else None,
                        value=value,
                        clock=index + 2,
                    ),
                )
            )
        )
    outcome = [Outcome.FINISHED, Outcome.FAILED, Outcome.STOPPED, Outcome.INTERRUPTED][seed % 4]
    append(
        Record(
            body=Oneof(
                "lifecycle",
                Lifecycle(
                    state=Oneof(
                        "ended",
                        Ended(
                            outcome=outcome,
                            summary="Recorded ending",
                            failure=RunFailure(kind=RunFailureKind.NO_RESULT, reason="No result")
                            if outcome == Outcome.FAILED
                            else None,
                        ),
                    )
                ),
            )
        )
    )
    return (fold_skeleton(records, log_offset=8), baseline)
