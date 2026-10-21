"""Search-only observations become descriptor-backed search attrs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._common import Projection, enum_value
from vibesys.records._spans import gate_stage, subject

if TYPE_CHECKING:
    from protobuf import Message


def labelled(projection: Projection, kind: str, identity: str) -> p.Span:
    existing = projection.spans.get(identity)
    if existing is not None:
        return existing
    return p.Span(
        id=identity,
        name=kind,
        runtime_type=p.RuntimeType.OPERATION,
        status=p.SpanStatus.RUNNING,
        started_at=projection.clock,
    )


def finish(projection: Projection, span: p.Span, kind: str, attrs: Message) -> None:
    if span.ended_at is None:
        span.status = (
            p.SpanStatus.FAILED
            if projection.event.status is e.EventStatus.FAILED
            else p.SpanStatus.DONE
        )
        span.ended_at = max(projection.clock, span.started_at)
    if span.label is None:
        span.label = p.Label(kind=kind, attrs=projection.pack(attrs, span.id))
    else:
        span.label.attrs = projection.pack(attrs, span.id)
    projection.span(span)


def workstream(projection: Projection) -> None:
    event = projection.event
    matches = [
        s for s in projection.spans.values() if s.label is not None and s.label.kind == "workstream"
    ]
    # Serial round labels are stable attributes, not a nearest-event heuristic.
    selected = None
    for span in matches:
        if span.label is None or span.label.attrs is None:
            continue
        attrs = p.WorkstreamAttrs.from_binary(span.label.attrs.value)
        if event.round_label == f"round-{attrs.round}":
            selected = span
    if selected is None and not projection.final:
        projection.pending.append(event.model_dump_json().encode())
        return
    if selected is None:
        selected = labelled(
            projection, "workstream", "workstream:" + (event.round_label or str(event.sequence))
        )
        attrs = p.WorkstreamAttrs()
    else:
        if selected.label is None or selected.label.attrs is None:
            message = "workstream label has no attrs"
            raise TypeError(message)
        attrs = p.WorkstreamAttrs.from_binary(selected.label.attrs.value)
    data = event.data
    if isinstance(data, e.RoundFinishedData):
        attrs.attempts = data.attempts
        attrs.profile_skipped = data.profile_skipped
        attrs.judge_verdict = data.judge_verdict
    elif isinstance(data, e.WorkstreamFinishedData):
        attrs.repeated_error = data.repeated_error
        failure = data.last_error
        if failure is not None:
            attrs.failure = p.WorkstreamFailure(
                kind=enum_value(p.WorkstreamFailureKind, failure.kind),
                role=failure.role,
                turns=failure.turns,
                limit=failure.limit,
                text=projection.text(selected.id, "workstream.failure", failure.text),
            )
    else:
        message = "workstream projection requires a round or workstream result"
        raise TypeError(message)
    finish(projection, selected, "workstream", attrs)


def adoption(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.WinnerSelectedData):
        message = "winner_selected requires WinnerSelectedData"
        raise TypeError(message)
    matches = [
        span
        for span in projection.spans.values()
        if span.label is not None
        and span.label.kind == "adoption"
        and span.label.attrs is not None
        and p.AdoptionAttrs.from_binary(span.label.attrs.value).candidate
        == (data.revision if data.selection == "candidate" else None)
    ]
    if not matches and not projection.final:
        projection.pending.append(projection.event.model_dump_json().encode())
        return
    span = (
        matches[-1]
        if matches
        else labelled(projection, "adoption", "adoption:" + (data.settlement_id or "input"))
    )
    finish(
        projection,
        span,
        "adoption",
        p.AdoptionAttrs(
            candidate=data.revision if data.selection == "candidate" else None,
            selection=enum_value(p.Selection, data.selection),
            settlement_id=data.settlement_id,
        ),
    )


def gate(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, (e.GateStartedData, e.GateFinishedData)):
        message = "gate projection requires GateStartedData or GateFinishedData"
        raise TypeError(message)
    identity = ":".join(
        ("gate", projection.event.execution_id or "input", data.gate, data.recipe or "default")
    )
    span = labelled(projection, "gate", identity)
    attrs = p.GateAttrs(
        stage=enum_value(p.GateStage, data.gate),
        subject=subject(projection, span.parent),
        recipe=data.recipe,
        source=enum_value(p.FrameworkSource, data.source),
        source_label=data.source_label,
    )
    if isinstance(data, e.GateStartedData):
        if data.command is not None:
            attrs.command = data.command
        span.label = p.Label(kind="gate", attrs=projection.pack(attrs, span.id))
        projection.span(span)
        return
    attrs.reused = data.reused
    if data.value is not None and data.metric is not None:
        projection.emit(
            "metric",
            p.MetricPoint(
                series=data.metric, span=span.id, value=data.value, clock=projection.clock
            ),
        )
    finish(projection, span, "gate", attrs)


def input_failure(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.InputMeasurementFailedData):
        message = "input_measurement_failed requires InputMeasurementFailedData"
        raise TypeError(message)
    stage = p.GateStage.MEASUREMENT if data.stage is None else gate_stage(data.stage)
    span = labelled(projection, "gate", "input-failure")
    attrs = p.GateAttrs(
        stage=stage,
        subject=p.GateSubject.INPUT,
        input_failure=p.InputFailure(
            kind=enum_value(p.InputFailureKind, data.failure),
            stage=None if data.stage is None else stage,
            attempts=data.attempts,
            compared=data.compared,
            stage_id=data.stage,
        ),
    )
    finish(projection, span, "gate", attrs)
    projection.attach(span.id, "summary", data.summary)
    projection.emit(
        "notice",
        p.Notice(
            severity=p.Severity.ERROR,
            code=p.NoticeCode.INPUT_MEASUREMENT_FAILED,
            span=span.id,
            message=projection.text(span.id, "notice", data.summary or data.failure.value),
        ),
    )


def comparison(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.MeasurementComparedData):
        message = "measurement_compared requires MeasurementComparedData"
        raise TypeError(message)
    identity = f"comparison:{data.candidate_commit}:{data.base_commit}:{data.metric}"
    span = labelled(projection, "comparison", identity)
    attrs = p.ComparisonAttrs(
        metric=data.metric,
        unit=data.unit or "",
        direction=enum_value(p.Direction, data.direction),
        ratio=data.ratio,
        noise=data.noise,
        verdict=enum_value(p.ComparisonVerdict, data.verdict),
        candidate_value=data.candidate_value,
        base_value=data.base_value,
        candidate_samples=data.candidate_samples,
        base_samples=data.base_samples,
    )
    links = []
    for rel, revision in (("candidate", data.candidate_commit), ("base", data.base_commit)):
        for candidate in projection.spans.values():
            label = candidate.label
            if (
                label is not None
                and label.kind == "workstream"
                and label.attrs is not None
                and p.WorkstreamAttrs.from_binary(label.attrs.value).revision == revision
            ):
                links.append(p.SpanLink(rel=rel, to=candidate.id))
    span.label = p.Label(kind="comparison", links=links, attrs=projection.pack(attrs, span.id))
    finish(projection, span, "comparison", attrs)


def baselines(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.ComparisonsReadData):
        message = "comparisons_read requires ComparisonsReadData"
        raise TypeError(message)
    for baseline in data.comparisons:
        span = labelled(projection, "baseline", "baseline:" + baseline.name)
        attrs = p.BaselineAttrs(
            label=baseline.label,
            status=enum_value(p.BaselineStatus, baseline.status),
            metric=baseline.metric,
            unit=baseline.unit,
            direction=None
            if baseline.direction is None
            else enum_value(p.Direction, baseline.direction),
            value=baseline.value,
            spread=baseline.spread,
            samples=baseline.samples,
            hosts=list(baseline.hosts),
            reason=baseline.reason,
        )
        finish(projection, span, "baseline", attrs)


MAPPERS = {
    e.CoreEventType.ROUND_FINISHED: workstream,
    e.CoreEventType.WORKSTREAM_FINISHED: workstream,
    e.CoreEventType.WINNER_SELECTED: adoption,
    e.CoreEventType.GATE_STARTED: gate,
    e.CoreEventType.GATE_FINISHED: gate,
    e.CoreEventType.INPUT_MEASUREMENT_FAILED: input_failure,
    e.CoreEventType.MEASUREMENT_COMPARED: comparison,
    e.CoreEventType.COMPARISONS_READ: baselines,
}
