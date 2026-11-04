"""Runtime probe and provider-capacity spans."""

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._agents import owner
from vibesys.records._common import Projection, enum_value
from vibesys.records._span_table import SpanKey
from vibesys.records._strategy import labelled


def probe(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.WorkspaceRootProbeData):
        message = "workspace_root_probe requires WorkspaceRootProbeData"
        raise TypeError(message)
    span = labelled(projection, "environment_probe", f"environment_probe:{projection.state.epoch}")
    values = data.model_dump(exclude={"kind", "state"})
    attrs = p.EnvironmentProbeAttrs(state=enum_value(p.ProbeState, data.state), **values)
    span.label = p.Label(kind="environment_probe", attrs=projection.pack(attrs, span.id))
    if data.state in {"proven", "failed"}:
        span.ended_at = max(span.started_at, projection.clock)
        span.status = p.SpanStatus.FAILED if data.state == "failed" else p.SpanStatus.DONE
    projection.span(span)
    if data.state == "failed":
        projection.emit(
            "lifecycle",
            p.Lifecycle(
                state=p.Oneof(
                    "ended",
                    p.Ended(
                        outcome=p.Outcome.FAILED,
                        failure=p.RunFailure(
                            kind=p.RunFailureKind.ENVIRONMENT_UNAVAILABLE,
                            reason=projection.text(span.id, "failure", data.failure or ""),
                        ),
                    ),
                )
            ),
        )


def _timestamp(value: float | None) -> p.Timestamp | None:
    if value is None:
        return None
    seconds = int(value)
    return p.Timestamp(seconds=seconds, nanos=int((value - seconds) * 1_000_000_000))


def capacity(projection: Projection) -> None:
    event = projection.event
    data = event.data
    if not isinstance(
        data,
        (
            e.QuotaPausedData,
            e.QuotaResumedData,
            e.QuotaAbandonedData,
            e.QuotaWaitingData,
            e.QuotaWaitCancelledData,
        ),
    ):
        message = "quota event requires a quota payload"
        raise TypeError(message)
    key = owner(projection) + ":" + data.provider
    if isinstance(data, e.QuotaPausedData):
        identities = [f"capacity:{key}:{event.sequence}"]
        projection.waits[key] = identities[0]
    elif key in projection.waits:
        identities = [projection.waits[key]]
    else:
        identities = _provider_waits(projection, data.provider)
    if not identities:
        message = f"{event.type}: no preceding quota_paused for {data.provider}"
        raise ValueError(message)
    for identity in identities:
        _capacity_span(projection, identity)


def _provider_waits(projection: Projection, provider: str) -> list[str]:
    return [
        span.id
        for span in projection.spans.matching(SpanKey.capacity_on(provider))
        if span.label is not None
        and span.label.kind == "capacity_wait"
        and span.label.attrs is not None
        and span.status == p.SpanStatus.RUNNING
        and p.CapacityWaitAttrs.from_binary(span.label.attrs.value).provider == provider
    ]


def _capacity_span(projection: Projection, identity: str) -> None:
    data = projection.event.data
    span = labelled(projection, "capacity_wait", identity)
    if isinstance(data, e.QuotaPausedData):
        span.parent = owner(projection)
        attrs = p.CapacityWaitAttrs(
            provider=data.provider,
            condition=enum_value(p.CapacityCondition, data.condition),
            policy=enum_value(p.CapacityPolicy, data.policy),
            detail=projection.text(span.id, "detail", data.detail),
            resets_at=_timestamp(data.resets_at),
            resumes_at=_timestamp(data.resumes_at),
            fallback_provider=data.fallback_provider,
            fallback_model=data.fallback_model,
        )
        if data.condition != "unavailable":
            projection.emit(
                "lifecycle",
                p.Lifecycle(
                    state=p.Oneof(
                        "running", p.Running(hold=p.Hold(reason=p.Oneof("capacity_span", identity)))
                    )
                ),
            )
    else:
        if span.label is None or span.label.attrs is None:
            message = f"{projection.event.type}: no preceding quota_paused for {identity}"
            raise ValueError(message)
        attrs = p.CapacityWaitAttrs.from_binary(span.label.attrs.value)
        _capacity_update(projection, span, attrs)
    verdict = (
        "resumed"
        if isinstance(data, e.QuotaResumedData)
        else "abandoned"
        if isinstance(data, e.QuotaAbandonedData)
        else None
    )
    span.label = p.Label(
        kind="capacity_wait", attrs=projection.pack(attrs, span.id), verdict=verdict
    )
    projection.span(span)


def _capacity_update(projection: Projection, span: p.Span, attrs: p.CapacityWaitAttrs) -> None:
    data = projection.event.data
    if isinstance(data, e.QuotaWaitingData):
        attrs.wait_resumes_at = _timestamp(data.resumes_at)
    elif isinstance(data, e.QuotaWaitCancelledData):
        attrs.wait_resumes_at = None
    elif isinstance(data, (e.QuotaResumedData, e.QuotaAbandonedData)):
        attrs.reason = data.reason
        span.status = (
            p.SpanStatus.FAILED if isinstance(data, e.QuotaAbandonedData) else p.SpanStatus.DONE
        )
        span.ended_at = max(span.started_at, projection.clock)
        if attrs.condition != p.CapacityCondition.UNAVAILABLE:
            projection.emit("lifecycle", p.Lifecycle(state=p.Oneof("running", p.Running())))


def switched(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.ProviderSwitchedData):
        message = "provider_switched requires ProviderSwitchedData"
        raise TypeError(message)
    span = labelled(projection, "provider_switch", f"provider_switch:{projection.event.sequence}")
    span.ended_at = span.started_at
    span.status = p.SpanStatus.DONE
    attrs = p.ProviderSwitchAttrs(
        from_provider=data.from_provider,
        to_provider=data.to_provider,
        to_model=data.to_model,
        reason=enum_value(p.ProviderSwitchReason, data.reason),
        detail=projection.text(span.id, "detail", data.detail),
    )
    key = owner(projection) + ":" + data.from_provider
    target = projection.waits.get(key, "capacity:" + key)
    span.label = p.Label(
        kind="provider_switch",
        attrs=projection.pack(attrs, span.id),
        links=[p.SpanLink(rel="settles", to=target)],
    )
    projection.span(span)


def rate_limit(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.RateLimitUpdateData):
        message = "rate_limit_update requires RateLimitUpdateData"
        raise TypeError(message)
    # OQ13 drops limit, reset time and window duration, retaining only the reading.
    if data.used_fraction is not None:
        projection.metric(
            p.MetricPoint(
                series=f"rate_limit.{data.provider or 'unknown'}.{data.window or 'unknown'}",
                value=data.used_fraction,
                clock=projection.clock,
            ),
            role=p.SeriesRole.RESOURCE,
        )


MAPPERS = {
    e.CoreEventType.WORKSPACE_ROOT_PROBE: probe,
    e.CoreEventType.QUOTA_PAUSED: capacity,
    e.CoreEventType.QUOTA_RESUMED: capacity,
    e.CoreEventType.QUOTA_ABANDONED: capacity,
    e.CoreEventType.QUOTA_WAITING: capacity,
    e.CoreEventType.QUOTA_WAIT_CANCELLED: capacity,
    e.CoreEventType.PROVIDER_SWITCHED: switched,
    e.CoreEventType.RATE_LIMIT_UPDATE: rate_limit,
}
