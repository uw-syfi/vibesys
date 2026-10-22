"""Writer-computed dashboard projection from the authoritative skeleton."""

from vs_proto.api import (
    AgentTurnAttrs,
    Direction,
    HeadlineObjective,
    Keyframe,
    RunHead,
    RunProcess,
    TokenUsage,
)
from vs_runlog._epoch import current_epoch


def project_head(state: Keyframe, process: RunProcess | None = None) -> RunHead | None:
    """Legacy-only logs have no strategy declaration and retain phase 1 reads."""
    epoch = next((v.epoch for v in reversed(state.epochs) if v.epoch and v.epoch.run), None)
    current = current_epoch(state)
    lifecycle = current.lifecycle
    if epoch is None or epoch.run is None:
        return None
    head = RunHead(
        strategy=epoch.run.strategy,
        epoch=current.number,
        lifecycle=lifecycle,
        awaiting_lifecycle=lifecycle is None,
        committed_seq=state.through_seq,
        process=process,
    )
    if lifecycle and lifecycle.state and lifecycle.state.field == "running":
        hold = lifecycle.state.value.hold
        head.needs_user = bool(hold and hold.reason and hold.reason.field == "operator")
    for history in state.spans.values():
        span = history.versions[-1].span
        if span is None or span.label is None:
            continue
        kind = span.label.kind
        head.span_counts[kind] = head.span_counts.get(kind, 0) + 1
        attrs = span.label.attrs
        if attrs and attrs.type_url.rsplit("/", 1)[-1] == "vibesys.store.v1.AgentTurnAttrs":
            usage = AgentTurnAttrs.from_binary(attrs.value).usage
            if usage is not None:
                head.usage = _add_usage(head.usage, usage)
    if epoch.run.objectives:
        objective = epoch.run.objectives[0]
        declaration = next(s for s in epoch.series if s.name == objective.series)
        points = next((h.points for h in state.metrics if h.series == objective.series), [])
        readings = [v.point for v in points if v.point is not None]
        head.objective = HeadlineObjective(series=declaration)
        if readings:
            values = [p.value for p in readings]
            head.objective.best_value = (
                max(values) if objective.direction == Direction.MAX else min(values)
            )
            input_value = next((p.value for p in readings if not p.has_field("span")), None)
            if input_value is not None:
                head.objective.input_value = input_value
            head.chart_points = readings[-256:]
    return head


def _add_usage(total: TokenUsage | None, usage: TokenUsage) -> TokenUsage:
    result = total if total is not None else TokenUsage()
    for field in (
        "input",
        "cache_read",
        "cache_creation",
        "output",
        "reasoning",
        "cost_usd",
        "duration_ms",
    ):
        value = getattr(usage, field)
        if usage.has_field(field):
            setattr(result, field, (getattr(result, field) or 0) + value)
    return result
