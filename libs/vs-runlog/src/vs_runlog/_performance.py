"""Pure, writer-owned decisions for measured ideas; readers only render these facts."""

from vs_proto.api import (
    ComparisonAttrs,
    ComparisonVerdict,
    Direction,
    EpochStarted,
    GateAttrs,
    GateSubject,
    IdeaDecision,
    IdeaOutcome,
    IdeaPerformance,
    Keyframe,
    PerformanceRefusal,
    SeriesPerformance,
    Span,
    SpanStatus,
    WorkstreamAttrs,
)


def project_performance(state: Keyframe, epoch: EpochStarted) -> list[SeriesPerformance]:
    """Keep every reading, but advance the standing value only for a kept idea.

    A failed or unfinished attempt cannot win through a successful gate. Detached
    historical gates remain visible, explicitly unattributed rather than assumed passed.
    """
    spans = {
        key: history.versions[-1].span
        for key, history in state.spans.items()
        if history.versions and history.versions[-1].span is not None
    }
    owners, groups = _ideas(spans)
    comparisons = _comparisons(spans)
    run = epoch.run
    if run is None:
        return []
    result = []
    for objective in run.objectives:
        series = SeriesPerformance(series=objective.series)
        if objective.direction not in (Direction.MIN, Direction.MAX):
            series.refusal = PerformanceRefusal.UNKNOWN_DIRECTION
        input_value = _input_value(state, epoch, objective.series, spans)
        if input_value is not None:
            series.input_value = input_value
            series.best_value = input_value
        history = next((h for h in state.metrics if h.series == objective.series), None)
        points = [] if history is None else history.points
        for version in sorted(
            points, key=lambda v: (0 if v.point is None else v.point.clock, v.seq)
        ):
            point = version.point
            if point is None:
                continue
            owner = _owner(point.span, spans, owners)
            idea = owners.get(owner) if owner else None
            outcome = _outcome(point.span, spans, groups.get(idea, []), comparisons, owner)
            reading = IdeaPerformance(
                measurement_seq=version.seq, point=point, idea=idea, outcome=outcome
            )
            series.points.append(reading)
        selected = {
            p.idea: p.measurement_seq
            for p in series.points
            if p.idea
            and p.outcome
            in (
                IdeaOutcome.KEPT,
                IdeaOutcome.NOT_KEPT,
                IdeaOutcome.WITHIN_NOISE,
                IdeaOutcome.WORSE_THAN_INPUT,
            )
        }
        for reading in series.points:
            if (
                reading.idea
                and selected.get(reading.idea) != reading.measurement_seq
                and reading.outcome == IdeaOutcome.KEPT
            ):
                reading.outcome = IdeaOutcome.NOT_KEPT
            _decide(series, reading, objective.direction)
        series.ideas = _decisions(series, spans, groups, comparisons)
        result.append(series)
    return result


def _ideas(spans: dict[str, Span]) -> tuple[dict[str, str], dict[str, list[Span]]]:
    owners: dict[str, str] = {}
    groups: dict[str, list[Span]] = {}
    for key, span in spans.items():
        label = span.label
        if label is None or label.attrs is None or label.kind != "workstream":
            continue
        attrs = WorkstreamAttrs.from_binary(label.attrs.value)
        if attrs.probe:
            continue
        idea = "hypothesis:" + attrs.hypothesis_id if attrs.hypothesis_id else "span:" + key
        owners[key] = idea
        groups.setdefault(idea, []).append(span)
    return owners, groups


def _owner(at: str | None, spans: dict[str, Span], owners: dict[str, str]) -> str | None:
    seen: set[str] = set()
    while at and at not in seen:
        if at in owners:
            return at
        seen.add(at)
        span = spans.get(at)
        at = None if span is None else span.parent
    return None


def _comparisons(spans: dict[str, Span]) -> dict[str, ComparisonVerdict]:
    result = {}
    for span in spans.values():
        label = span.label
        if label and label.kind == "comparison" and label.attrs:
            attrs = ComparisonAttrs.from_binary(label.attrs.value)
            for link in label.links:
                if link.rel == "candidate":
                    result[link.to] = attrs.verdict
    return result


def _outcome(
    point_span: str | None,
    spans: dict[str, Span],
    attempts: list[Span],
    comparisons: dict[str, ComparisonVerdict],
    owner: str | None,
) -> IdeaOutcome:
    span = spans.get(point_span or "")
    label = None if span is None else span.label
    if (
        label
        and label.kind == "gate"
        and label.attrs
        and GateAttrs.from_binary(label.attrs.value).subject == GateSubject.INPUT
    ):
        return IdeaOutcome.INPUT
    if not attempts:
        return IdeaOutcome.UNATTRIBUTED
    if any(
        s.status in (SpanStatus.PENDING, SpanStatus.RUNNING, SpanStatus.WAITING) for s in attempts
    ):
        return IdeaOutcome.PENDING
    passed = [s for s in attempts if s.label and s.label.verdict in ("passed", "accepted")]
    if not passed or (owner is not None and spans[owner] not in passed):
        return IdeaOutcome.FAILED
    verdicts = [comparisons.get(owner)] if owner else []
    return (
        IdeaOutcome.WITHIN_NOISE
        if ComparisonVerdict.WITHIN_NOISE in verdicts
        else IdeaOutcome.NOT_KEPT
        if ComparisonVerdict.WORSE in verdicts
        else IdeaOutcome.KEPT
    )


def _decide(series: SeriesPerformance, reading: IdeaPerformance, direction: Direction) -> None:
    if reading.outcome != IdeaOutcome.KEPT or series.has_field("refusal"):
        if reading.outcome == IdeaOutcome.KEPT:
            reading.outcome = IdeaOutcome.NOT_KEPT
        return
    point = reading.point
    if point is None:
        return
    value = point.value
    baseline = series.input_value if series.has_field("input_value") else None
    if baseline is not None and (
        value > baseline if direction == Direction.MIN else value < baseline
    ):
        reading.outcome = IdeaOutcome.WORSE_THAN_INPUT
        return
    standing = series.best_value if series.has_field("best_value") else None
    if standing is not None and not (
        value < standing if direction == Direction.MIN else value > standing
    ):
        reading.outcome = IdeaOutcome.NOT_KEPT
        return
    reading.new_best = True
    series.best_value = value


def _input_value(
    state: Keyframe, epoch: EpochStarted, metric: str, spans: dict[str, Span]
) -> float | None:
    run = epoch.run
    fact = None if run is None else run.input_measurement
    if fact is not None:
        if fact.state is None or fact.state.field != "measured":
            return None
        if fact.state.value.series == metric:
            return fact.state.value.value
    # The retained encoding recorded the input on its gate, before input_measurement existed.
    inputs = {
        key
        for key, span in spans.items()
        if span.label
        and span.label.kind == "gate"
        and span.label.attrs
        and GateAttrs.from_binary(span.label.attrs.value).subject == GateSubject.INPUT
    }
    return next(
        (
            v.point.value
            for history in state.metrics
            if history.series == metric
            for v in history.points
            if v.point and (not v.point.span or v.point.span in inputs)
        ),
        None,
    )


def _decisions(
    series: SeriesPerformance,
    spans: dict[str, Span],
    groups: dict[str, list[Span]],
    comparisons: dict[str, ComparisonVerdict],
) -> list[IdeaDecision]:
    decisions = []
    for idea, attempts in groups.items():
        readings = [point for point in series.points if point.idea == idea]
        selected = next(
            (
                point
                for point in reversed(readings)
                if point.outcome not in (IdeaOutcome.FAILED, IdeaOutcome.PENDING)
            ),
            None,
        )
        selected = selected or (readings[-1] if readings else None)
        passed = [
            span for span in attempts if span.label and span.label.verdict in ("passed", "accepted")
        ]
        owner = passed[-1].id if passed else attempts[-1].id
        outcome = _outcome(None, spans, attempts, comparisons, owner)
        if outcome == IdeaOutcome.KEPT:
            outcome = IdeaOutcome.NOT_KEPT if selected is None else selected.outcome
        decision = IdeaDecision(
            idea=idea, outcome=outcome, new_best=any(p.new_best for p in readings)
        )
        if decision.new_best:
            decision.outcome = IdeaOutcome.KEPT
        if selected is not None:
            decision.measurement_seq = selected.measurement_seq
        decisions.append(decision)
    return decisions
