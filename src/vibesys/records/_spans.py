"""Whole runtime spans and descriptor-backed strategy attrs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.wrappers_pb import StringValue

import vibesys.search_spans.api as search
import vs_proto.api as p
from vibesys.records._agents import stream_id
from vibesys.records._common import Projection, enum_value
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from protobuf import Message
    from pydantic import BaseModel

    from vs_core.api import Span as CoreSpan

ATTRS_TYPES: dict[str, type[Message]] = {
    "planning_call": p.PlanningAttrs,
    "workstream": p.WorkstreamAttrs,
    "agent_turn": p.AgentTurnAttrs,
    "gate": p.GateAttrs,
    "adoption": p.AdoptionAttrs,
    "comparison": p.ComparisonAttrs,
    "baseline": p.BaselineAttrs,
    "environment_probe": p.EnvironmentProbeAttrs,
    "capacity_wait": p.CapacityWaitAttrs,
    "provider_switch": p.ProviderSwitchAttrs,
}
_MODELS: dict[str, type[BaseModel]] = {
    "planning_call": search.PlanningAttrs,
    "workstream": search.WorkstreamAttrs,
    "agent_turn": search.AgentTurnAttrs,
    "gate": search.GateAttrs,
    "adoption": search.AdoptionAttrs,
}


def gate_stage(stage: str) -> p.GateStage:
    """Categorize an evaluator operation while stages retains its exact ids."""
    stages = {
        "build": p.GateStage.BUILD,
        "accuracy": p.GateStage.ACCURACY,
        "correctness": p.GateStage.ACCURACY,
        "benchmark": p.GateStage.BENCHMARK,
        "validation": p.GateStage.VALIDATION,
    }
    return stages.get(stage, p.GateStage.MEASUREMENT)


def subject(projection: Projection, parent: str | None) -> p.GateSubject:
    """Use the owner chain, never arrival order, to identify an input gate."""
    seen: set[str] = set()
    while parent is not None and parent not in seen:
        seen.add(parent)
        span = projection.spans.get(parent)
        if span is None:
            break
        if span.label is not None and span.label.kind == "workstream":
            return p.GateSubject.CANDIDATE
        parent = span.parent
    return p.GateSubject.INPUT


def _known_attrs(projection: Projection, span: CoreSpan, model: BaseModel) -> Message:
    values = model.model_dump(mode="json")
    if isinstance(model, search.AgentTurnAttrs):
        return p.AgentTurnAttrs(
            **values,
            stage=span.name,
            started_at=p.Timestamp.from_seconds(span.started_at)
            if projection.state.wall_span_clock
            else None,
            ended_at=p.Timestamp.from_seconds(span.ended_at)
            if projection.state.wall_span_clock and span.ended_at is not None
            else None,
        )
    if isinstance(model, search.GateAttrs):
        failed = model.failed
        return p.GateAttrs(
            stage=gate_stage(model.stage),
            stages=[name for name in model.stage.split(",") if name],
            subject=subject(projection, span.parent),
            failed=None
            if failed is None
            else p.FailedCheck(
                check=enum_value(p.EvidenceKind, failed.check),
                completed=failed.completed,
                required=failed.required,
                unit=failed.unit,
            ),
        )
    if isinstance(model, search.AdoptionAttrs):
        return p.AdoptionAttrs(
            candidate=model.candidate,
            selection=p.Selection.BASELINE if model.candidate is None else p.Selection.CANDIDATE,
        )
    return (
        p.PlanningAttrs(**values)
        if isinstance(model, search.PlanningAttrs)
        else p.WorkstreamAttrs(**values)
    )


def _label_attrs(projection: Projection, span: CoreSpan) -> Message:
    label = span.label
    if label is None:
        message = f"{span.id}: labelled attrs require a label"
        raise TypeError(message)
    model_type = _MODELS.get(label.kind)
    raw = label.attrs_json or "{}"
    if model_type is None:
        # The old journal carries no schema for custom kinds. Preserve its exact
        # JSON, including large integers, in a declared StringValue. Builtins
        # always use their concrete attrs contract and never enter this path.
        return StringValue(value=raw)
    model = decode_json(raw, model_type, source=f"span {span.id} attrs", max_bytes=64 << 20)
    if isinstance(model, Unreadable):
        raise TypeError(model.details("span attrs"))
    return _known_attrs(projection, span, model)


def _metric_rows(projection: Projection, source: CoreSpan, span: p.Span) -> None:
    label = source.label
    if label is None or label.kind != "gate" or label.attrs_json is None:
        return
    attrs = decode_json(label.attrs_json, search.GateAttrs, source=source.id, max_bytes=64 << 20)
    if isinstance(attrs, Unreadable):
        raise TypeError(attrs.details("gate attrs"))
    for row in attrs.metrics:
        projection.metric(
            p.MetricPoint(
                series=row.name,
                span=source.id,
                value=row.value,
                clock=span.ended_at if span.has_field("ended_at") else span.started_at,
            ),
            unit=row.unit or "",
            direction=enum_value(p.Direction, row.direction),
        )


def _merge_prior(kind: str, attrs: Message, merged: Message) -> Message:
    """Fold a new label onto the recorded one, keeping a scripted turn free of a configured model."""
    authoritative = (set(_MODELS[kind].model_fields) if kind in _MODELS else set()).difference(
        {"model"} if kind == "agent_turn" else set()
    )
    for field in merged.desc().fields:
        if field.name in authoritative and not attrs.has_field(field.name):
            merged.clear_field(field.name)
    scripted = (
        kind == "agent_turn"
        and isinstance(merged, p.AgentTurnAttrs)
        and merged.client == p.AgentClient.SCRIPTED
    )
    for field in attrs:
        if scripted and field.name in {"model", "provider", "reasoning_effort"}:
            continue
        if (
            kind == "agent_turn"
            and field.name in {"stage", "model"}
            and merged.has_field(field.name)
        ):
            continue
        merged[field] = attrs[field]
    if scripted:
        for name in ("model", "provider", "reasoning_effort"):
            if merged.has_field(name):
                merged.clear_field(name)
    return merged


def _span_label(projection: Projection, source: CoreSpan) -> p.Label | None:
    previous = projection.spans.get(source.id)
    label = source.label
    mapped_label = previous.label if previous is not None else None
    if label is not None:
        attrs = _label_attrs(projection, source)
        if previous is not None and previous.label is not None:
            prior = previous.label.attrs
            if prior is not None and prior.type_url.endswith("/" + attrs.desc().type_name):
                attrs = _merge_prior(label.kind, attrs, type(attrs).from_binary(prior.value))
        projection.declare_kind(label.kind, attrs.desc().type_name)
        mapped_label = p.Label(
            kind=label.kind,
            title=label.title or "",
            verdict=label.verdict,
            reason=label.reason,
            links=[p.SpanLink(rel=link.rel, to=link.to) for link in label.links]
            + (
                []
                if previous is None or previous.label is None
                else [link for link in previous.label.links if link.rel == "steer"]
            ),
            attrs=projection.pack(attrs, source.id),
        )
    return _seal_transcript(projection, source, mapped_label)


def _seal_transcript(
    projection: Projection, source: CoreSpan, label: p.Label | None
) -> p.Label | None:
    if (
        label is None
        or label.kind != "agent_turn"
        or label.attrs is None
        or source.ended_at is None
    ):
        return label
    attrs = p.AgentTurnAttrs.from_binary(label.attrs.value)
    attrs.transcript_length = projection.lengths.get(stream_id(source.id), 0)
    result = p.Label.from_binary(label.to_binary())
    result.attrs = projection.pack(attrs, source.id)
    return result


def project_span(projection: Projection, source: CoreSpan, change: p.Change) -> None:
    """Copy every runtime field and merge supplemental facts into the whole version."""
    mapped_label = _span_label(projection, source)
    span = p.Span(
        id=source.id,
        parent=source.parent,
        runtime_type=enum_value(p.RuntimeType, source.type),
        name=source.name,
        status=enum_value(p.SpanStatus, source.status),
        waiting_on=[
            p.WaitingOn(kind=enum_value(p.WaitKind, wait.kind), ref=wait.ref, since=wait.since)
            for wait in source.waiting_on
        ],
        attempts=[
            p.Attempt(
                n=a.n,
                of=a.of,
                started_at=a.started_at,
                ended_at=a.ended_at,
                outcome=None if a.outcome is None else enum_value(p.AttemptOutcome, a.outcome),
                reason=a.reason,
            )
            for a in source.attempts
        ],
        started_at=source.started_at,
        ended_at=source.ended_at,
        failure=None
        if source.failure is None
        else p.Failure(
            kind=enum_value(p.SpanFailureKind, source.failure.kind), message=source.failure.message
        ),
        stop=None
        if source.stop is None
        else p.Stop(requested_at=source.stop.requested_at, landed=source.stop.landed),
        label=mapped_label,
    )
    if projection.state.wall_span_clock:
        _run_clock(projection, span)
    if source.invocation is not None:
        projection.invocations[source.invocation] = source.id
    # A task can start and end within one core commit; its first snapshot is terminal.
    if change == p.Change.STARTED and source.ended_at is not None:
        change = p.Change.ENDED
    projection.span(span, change)
    _metric_rows(projection, source, span)


def _run_clock(projection: Projection, span: p.Span) -> None:
    """Translate runtime wall times; retained versions own times from older epochs."""
    previous = projection.spans.get(span.id)

    def convert(value: float, prior: float | None = None) -> float:
        if prior is not None:
            return prior
        for wall, clock in reversed(projection.state.clock_epochs):
            if value >= wall:
                return max(0.0, clock + value - wall)
        return 0

    span.started_at = convert(span.started_at, None if previous is None else previous.started_at)
    if span.has_field("ended_at"):
        span.ended_at = convert(
            span.ended_at,
            None if previous is None or not previous.has_field("ended_at") else previous.ended_at,
        )
    if span.stop is not None:
        prior = None if previous is None else previous.stop
        span.stop.requested_at = convert(
            span.stop.requested_at, None if prior is None else prior.requested_at
        )
    attempts = {} if previous is None else {a.n: a for a in previous.attempts}
    for attempt in span.attempts:
        prior = attempts.get(attempt.n)
        attempt.started_at = convert(
            attempt.started_at, None if prior is None else prior.started_at
        )
        if attempt.has_field("ended_at"):
            attempt.ended_at = convert(
                attempt.ended_at,
                None if prior is None or not prior.has_field("ended_at") else prior.ended_at,
            )
    waits = {} if previous is None else {(w.kind, w.ref): w for w in previous.waiting_on}
    for wait in span.waiting_on:
        prior = waits.get((wait.kind, wait.ref))
        wait.since = convert(wait.since, None if prior is None else prior.since)
