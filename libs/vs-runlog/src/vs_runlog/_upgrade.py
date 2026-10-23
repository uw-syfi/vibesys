"""Read-time semantic upgrades for retained store.v1 snapshots."""

from vs_proto.api import (
    EpochStarted,
    EpochVersion,
    GateAttrs,
    GateSubject,
    InputMeasurement,
    Keyframe,
    MeasuredInput,
    MetricPoint,
    Oneof,
    Record,
    Span,
)


def _same_input(left: EpochStarted | None, right: EpochStarted) -> bool:
    previous, current = None if left is None else left.run, right.run
    return bool(
        previous is not None
        and current is not None
        and previous.input == current.input
        and previous.objectives
        and current.objectives
        and previous.objectives[0].series == current.objectives[0].series
    )


class InputUpgrade:
    """The store's minimal input context, advanced one record at a time."""

    def __init__(self) -> None:
        self.epoch: EpochStarted | None = None
        self.inputs: set[str] = set()

    def declaration(self, epoch: EpochStarted) -> None:
        previous = None if self.epoch is None else self.epoch.run
        if (
            epoch.run is not None
            and epoch.run.input_measurement is None
            and _same_input(self.epoch, epoch)
            and previous is not None
        ):
            epoch.run.input_measurement = previous.input_measurement
        self.epoch = EpochStarted.from_binary(epoch.to_binary())

    def span(self, span: Span) -> None:
        self.inputs.discard(span.id)
        label = span.label
        if label is None or label.kind != "gate" or label.attrs is None:
            return
        if (
            label.attrs.type_url == "type.googleapis.com/vibesys.search.v1.GateAttrs"
            and GateAttrs.from_binary(label.attrs.value).subject == GateSubject.INPUT
        ):
            self.inputs.add(span.id)

    def apply(self, record: Record) -> None:
        """Amend ``record`` in place with the fact a retained encoding leaves implicit."""
        body = record.body
        if body is None:
            return
        match body.field:
            case "epoch_started" | "declaration_amended":
                self.declaration(body.value)
            case "span":
                if body.value.span is not None:
                    self.span(body.value.span)
            case "metric":
                if (amended := self.measurement(body.value)) is not None and amended.run:
                    record.input_measurement = amended.run.input_measurement
        run = None if self.epoch is None else self.epoch.run
        if record.input_measurement is not None and run is not None:
            run.input_measurement = record.input_measurement

    def observe(self, record: Record) -> None:
        """Advance over ``record`` without amending it."""
        body = record.body
        if body is not None and body.field in ("epoch_started", "declaration_amended", "metric"):
            record = Record.from_binary(record.to_binary())
        self.apply(record)

    @property
    def measured(self) -> float | None:
        """The latest declaration's measured input value after the records applied."""
        run = None if self.epoch is None else self.epoch.run
        fact = None if run is None else run.input_measurement
        if fact is None or fact.state is None or fact.state.field != "measured":
            return None
        return fact.state.value.value

    def measurement(self, point: MetricPoint) -> EpochStarted | None:
        epoch = self.epoch
        run = None if epoch is None else epoch.run
        if epoch is None or run is None or run.input_measurement is not None or not run.objectives:
            return None
        if point.series != run.objectives[0].series or (
            point.span and point.span not in self.inputs
        ):
            return None
        run.input_measurement = InputMeasurement(
            state=Oneof(
                "measured", MeasuredInput(series=point.series, value=point.value, span=point.span)
            )
        )
        return EpochStarted.from_binary(epoch.to_binary())


def upgrade_skeleton(state: Keyframe) -> Keyframe:
    """Restore pre-field readings at their original sequence; explicit facts always win.

    Reuse the input fact through resumptions of the same input and objective. A successor's
    lifecycle remains independent. Neither retained files nor the caller's snapshot change.
    """
    result = Keyframe.from_binary(state.to_binary())
    events: list[tuple[int, EpochStarted | Span | MetricPoint]] = [
        (version.seq, version.epoch) for version in result.epochs if version.epoch is not None
    ]
    events.extend(
        (version.seq, version.span)
        for history in result.spans.values()
        for version in history.versions
        if version.span is not None
    )
    events.extend(
        (version.seq, version.point)
        for history in result.metrics
        for version in history.points
        if version.point is not None
    )
    upgrade = InputUpgrade()
    for seq, value in sorted(events, key=lambda row: row[0]):
        if isinstance(value, EpochStarted):
            upgrade.declaration(value)
        elif isinstance(value, Span):
            upgrade.span(value)
        elif (amended := upgrade.measurement(value)) is not None:
            result.epochs.append(EpochVersion(seq=seq, epoch=amended))
    result.epochs.sort(key=lambda version: version.seq)
    return result
