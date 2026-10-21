"""Whole runtime spans and descriptor-backed strategy attrs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from protobuf.wkt.google.protobuf.struct_pb import Struct

import vibesys.search_spans.api as search
import vs_proto.api as p
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
        return p.AgentTurnAttrs(**values, stage=span.name)
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
        # Extensible user-defined kinds have no Python model in this checkout.
        # Struct explicitly describes their JSON attrs; builtin kinds never use it.
        return Struct.from_json(raw)
    model = decode_json(raw, model_type, source=f"span {span.id} attrs", max_bytes=64 << 20)
    if isinstance(model, Unreadable):
        raise TypeError(model.details("span attrs"))
    return _known_attrs(projection, span, model)


def _metric_rows(projection: Projection, source: CoreSpan) -> None:
    label = source.label
    if label is None or label.kind != "gate" or label.attrs_json is None:
        return
    attrs = decode_json(label.attrs_json, search.GateAttrs, source=source.id, max_bytes=64 << 20)
    if isinstance(attrs, Unreadable):
        raise TypeError(attrs.details("gate attrs"))
    for row in attrs.metrics:
        projection.emit(
            "metric",
            p.MetricPoint(
                series=row.name,
                span=source.id,
                value=row.value,
                clock=source.ended_at if source.ended_at is not None else source.started_at,
            ),
        )


def project_span(projection: Projection, source: CoreSpan, change: p.Change) -> None:
    """Copy every runtime field and merge supplemental facts into the whole version."""
    previous = projection.spans.get(source.id)
    label = source.label
    mapped_label = None
    if label is not None:
        attrs = _label_attrs(projection, source)
        if previous is not None and previous.label is not None:
            prior = previous.label.attrs
            if prior is not None and prior.type_url.endswith("/" + attrs.desc().type_name):
                merged = type(attrs).from_binary(prior.value)
                authoritative = (
                    set(_MODELS[label.kind].model_fields) if label.kind in _MODELS else set()
                )
                for field in merged.desc().fields:
                    if field.name in authoritative and not attrs.has_field(field.name):
                        merged.clear_field(field.name)
                for field in attrs:
                    if (
                        label.kind == "agent_turn"
                        and field.name == "stage"
                        and merged.has_field("stage")
                    ):
                        continue
                    merged[field] = attrs[field]
                attrs = merged
        mapped_label = p.Label(
            kind=label.kind,
            title=label.title or "",
            verdict=label.verdict,
            reason=label.reason,
            links=[p.SpanLink(rel=link.rel, to=link.to) for link in label.links],
            attrs=projection.pack(attrs, source.id),
        )
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
    if source.invocation is not None:
        projection.invocations[source.invocation] = source.id
    # A task can start and end within one core commit; its first snapshot is terminal.
    if change == p.Change.STARTED and source.ended_at is not None:
        change = p.Change.ENDED
    projection.span(span, change)
    _metric_rows(projection, source)
