"""Search-only observations become descriptor-backed search attrs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._common import Projection, enum_value, measured_input
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
    if not span.has_field("ended_at"):
        span.status = (
            p.SpanStatus.FAILED
            if projection.event.status is e.EventStatus.FAILED
            else p.SpanStatus.DONE
        )
        span.ended_at = max(projection.clock, span.started_at)
    update_label(projection, span, kind, attrs)
    projection.span(span)


def update_label(projection: Projection, span: p.Span, kind: str, attrs: Message) -> None:
    """Supplemental observations enrich the runtime label, never replace its other facts."""
    label = span.label
    if (
        label is not None
        and label.kind == kind
        and label.attrs is not None
        and label.attrs.type_url.endswith("/" + attrs.desc().type_name)
    ):
        merged = type(attrs).from_binary(label.attrs.value)
        for field in attrs:
            merged[field] = attrs[field]
        attrs = merged
    if label is None or label.kind != kind:
        span.label = p.Label(kind=kind, attrs=projection.pack(attrs, span.id))
    else:
        label.attrs = projection.pack(attrs, span.id)


def unattributed(projection: Projection, attrs: Message) -> None:
    """Keep historical strategy facts without inventing a second work unit."""
    projection.emit(
        "notice",
        p.Notice(
            severity=p.Severity.WARNING,
            code=p.NoticeCode.UNATTRIBUTED_EVENT,
            message=f"{projection.event.type}: journal observation has no trace owner",
            detail=projection.pack(attrs),
        ),
    )


def _workstream_owner(projection: Projection) -> tuple[p.Span, p.WorkstreamAttrs] | None:
    label = projection.event.round_label
    for span in projection.spans.values():
        if span.label is None or span.label.kind != "workstream" or span.label.attrs is None:
            continue
        attrs = p.WorkstreamAttrs.from_binary(span.label.attrs.value)
        ordinals = [getattr(attrs, name) for name in ("round", "sequence") if attrs.has_field(name)]
        if label in {f"round-{ordinal}" for ordinal in ordinals}:
            return span, attrs
    return None


def workstream(projection: Projection) -> None:
    event = projection.event
    found = _workstream_owner(projection)
    if found is None and not projection.final:
        projection.pending.append(event.model_dump_json().encode())
        return
    selected, attrs = (None, p.WorkstreamAttrs()) if found is None else found
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
                text=projection.text(
                    "run" if selected is None else selected.id, "workstream.failure", failure.text
                ),
            )
    else:
        message = "workstream projection requires a round or workstream result"
        raise TypeError(message)
    if selected is None:
        unattributed(projection, attrs)
    else:
        finish(projection, selected, "workstream", attrs)


def _checkout_status(status: e.CheckoutStatus | None) -> p.CheckoutStatus | None:
    """The core checkout status as the store enum, or None when the run recorded none."""
    if status is None:
        return None
    return enum_value(p.CheckoutStatus, status.value)


def _adoption_checkout(span: p.Span) -> p.CheckoutStatus | None:
    """The checkout status an adoption span already recorded."""
    label = span.label
    if label is None or label.attrs is None:
        return None
    attrs = p.AdoptionAttrs.from_binary(label.attrs.value)
    return attrs.checkout_status if attrs.has_field("checkout_status") else None


def _adoption_candidate(span: p.Span) -> str | None:
    """The candidate an adoption span names, or None for the baseline."""
    label = span.label
    if label is None or label.attrs is None:
        return None
    attrs = p.AdoptionAttrs.from_binary(label.attrs.value)
    return attrs.candidate if attrs.has_field("candidate") else None


def adoption(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.WinnerSelectedData):
        message = "winner_selected requires WinnerSelectedData"
        raise TypeError(message)
    wanted = data.revision if data.selection == "candidate" else None
    matches = [
        span
        for span in projection.spans.values()
        if span.label is not None
        and span.label.kind == "adoption"
        and span.label.attrs is not None
        and _adoption_candidate(span) == wanted
    ]
    if not matches and not projection.final:
        projection.pending.append(projection.event.model_dump_json().encode())
        return
    recorded = _checkout_status(data.checkout_status)
    if recorded is None and matches:
        recorded = _adoption_checkout(matches[-1])
    attrs = p.AdoptionAttrs(
        candidate=data.revision if data.selection == "candidate" else None,
        selection=enum_value(p.Selection, data.selection),
        settlement_id=data.settlement_id,
        checkout_status=recorded,
    )
    if not matches:
        unattributed(projection, attrs)
    else:
        span = matches[-1]
        update_label(projection, span, "adoption", attrs)
        projection.span(span)


def _legacy_round_benchmark_repeat(
    projection: Projection, span_id: str, metric: str | None
) -> bool:
    """The named rule for the old duplicate benchmark: its repeat is dropped, not remapped.

    A journal of an older checkout reports each round's benchmark twice: first with no execution
    and no span (so it reuses the input gate's identity, ``gate:input:benchmark:default``), then
    with its own execution (the reading that belongs to the candidate). The executionless repeat
    carries the same value as the candidate event that follows it, so it adds no fact and is
    dropped here, by name. The input's own measurement is the first reading at that identity;
    the typed ``InputMeasurement`` is written from it alone.
    """
    return (metric, span_id) in projection.metric_values


def _spanless_subject(projection: Projection) -> p.GateSubject:
    """A gate event of an older journal names no span: the input is measured before any candidate exists.

    The run's own execution runs the input's gates, so an execution id does not tell them from a
    candidate's; whether a candidate has started does. A workstream span says so. The oldest
    multi-agent journals record a workstream only when its round ends, yet an implementer's
    candidate is evaluated in the round, so a candidate also exists once an agent has run after
    the input was measured (the input is measured before any agent runs). The input's first
    committed measurement stays the run's input; later readings are ordinary readings.
    """
    if projection.event.execution_id is None:
        return p.GateSubject.INPUT
    labels = [span.label.kind for span in projection.spans.values() if span.label is not None]
    measured = measured_input(projection.input_measurement) is not None
    started = "workstream" in labels or (measured and "agent_turn" in labels)
    return p.GateSubject.CANDIDATE if started else p.GateSubject.INPUT


def gate(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, (e.GateStartedData, e.GateFinishedData)):
        message = "gate projection requires GateStartedData or GateFinishedData"
        raise TypeError(message)
    identity = ":".join(
        ("gate", projection.event.execution_id or "input", data.gate, data.recipe or "default")
    )
    known = data.span_id is not None and data.span_id in projection.spans
    span = labelled(projection, "gate", data.span_id or identity)
    attrs = p.GateAttrs(
        stage=enum_value(p.GateStage, data.gate),
        subject=subject(projection, span.parent) if known else _spanless_subject(projection),
        recipe=data.recipe,
        source=enum_value(p.FrameworkSource, data.source),
        source_label=data.source_label,
    )
    if isinstance(data, e.GateStartedData):
        if data.command is not None:
            attrs.command = data.command
        update_label(projection, span, "gate", attrs)
        projection.span(span)
        return
    attrs.reused = data.reused
    repeated = not known and _legacy_round_benchmark_repeat(projection, span.id, data.metric)
    if data.value is not None and data.metric is not None and not repeated:
        if known and not span.has_field("ended_at") and not projection.final:
            projection.pending.append(projection.event.model_dump_json().encode())
        else:
            projection.metric(
                p.MetricPoint(
                    series=data.metric,
                    span=span.id,
                    value=data.value,
                    clock=span.ended_at if span.has_field("ended_at") else projection.clock,
                ),
                unit=data.unit or "",
                input_gate=attrs.subject == p.GateSubject.INPUT,
            )
    if data.span_id is not None:
        update_label(projection, span, "gate", attrs)
        projection.span(span)
    else:
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
    projection.give_up_input()
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
