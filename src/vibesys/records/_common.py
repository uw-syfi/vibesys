"""Shared projection mechanics: copying, content addressing and envelopes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING

from protobuf import Message
from protobuf.wkt.google.protobuf.any_pb import Any

import vs_proto.api as p
from vibesys.records._types import BlobWrite, MappedEvent, MappingState, StreamWrite

if TYPE_CHECKING:
    from vibesys.events import CoreEvent

INLINE_BYTES = 65536


def pack(message: Message) -> Any:
    """Pack an attrs value only after validating its own contract."""
    p.validate(message)
    return Any(
        type_url="type.googleapis.com/" + message.desc().type_name, value=message.to_binary()
    )


def enum_value[T: Enum](enum: type[T], value: str) -> T:
    """Translate matching vocabularies, rejecting missing members by name."""
    return enum[value.upper().replace("-", "_")]


class InputReboundError(ValueError):
    """A second, different measurement of the input in one epoch: the fact is written once."""


class MetricReboundError(ValueError):
    """A second, different reading at a reading's identity: a reading is immutable once written."""


def measured_input(fact: p.InputMeasurement | None) -> p.MeasuredInput | None:
    """The measurement, when the fact says the input was measured."""
    if fact is None or fact.state is None or fact.state.field != "measured":
        return None
    return fact.state.value


class Projection:
    """A local builder; no mutable value is shared with its input state."""

    def __init__(self, state: MappingState, event: CoreEvent) -> None:
        self.state = state
        self.event = event
        self.origin = event.timestamp.timestamp() if state.origin is None else state.origin
        self.clock = max(
            state.clock, state.clock_offset + event.timestamp.timestamp() - self.origin
        )
        self.spans = {key: p.Span.from_binary(value) for key, value in state.spans}
        self.invocations = dict(state.invocations)
        self.lengths = dict(state.streams)
        self.waits = dict(state.waits)
        self.intents = dict(state.intents)
        self.primary_series = state.primary_series
        self.objective_inferred = state.objective_inferred
        self.input_measurement = (
            None
            if state.input_measurement is None
            else p.InputMeasurement.from_binary(state.input_measurement)
        )
        self.input_inherited = state.input_inherited
        self.metric_values = dict(state.metric_values)
        self.records: list[p.Record] = []
        self.blobs: list[BlobWrite] = []
        self.streams: list[StreamWrite] = []
        self.ending = state.ending
        self.pending = list(state.pending)
        self.activities = dict(state.activities)
        self.final = False
        self.epoch_ended = state.epoch_ended
        self.header = (
            None if state.epoch_header is None else p.EpochStarted.from_binary(state.epoch_header)
        )

    def bound(self, message: Message, owner: str, path: str = "") -> None:
        """Externalize text losslessly; attachment roles name the original field path."""
        for field in message:
            value = message[field]
            name = path + "." + field.name if path else field.name
            if isinstance(value, str) and len(value.encode()) > INLINE_BYTES:
                message[field] = self.text(owner, name, value)
            elif isinstance(value, Message):
                self.bound(value, owner, name)
            elif isinstance(value, list):
                self._bound_items(value, owner, name)

    def _bound_items(self, values: list[object], owner: str, path: str) -> None:
        for index, value in enumerate(values):
            name = f"{path}[{index}]"
            if isinstance(value, str) and len(value.encode()) > INLINE_BYTES:
                values[index] = self.text(owner, name, value)
            elif isinstance(value, Message):
                self.bound(value, owner, name)

    def pack(self, message: Message, owner: str = "run") -> Any:
        self.bound(message, owner)
        return pack(message)

    def declare_kind(self, kind: str, attrs_type: str) -> None:
        if self.header is None or any(k.kind == kind for k in self.header.span_kinds):
            return
        header = p.EpochStarted.from_binary(self.header.to_binary())
        header.span_kinds.append(p.SpanKindDecl(kind=kind, attrs_type=attrs_type, timeline=True))
        self.emit("declaration_amended", header)

    def metric(
        self,
        point: p.MetricPoint,
        *,
        unit: str = "",
        direction: p.Direction = p.Direction.NONE,
        role: p.SeriesRole = p.SeriesRole.REFERENCE,
        input_gate: bool = False,
    ) -> None:
        """Record a reading; ``input_gate`` marks one taken by the input's gate.

        A reading at a span is immutable at its identity (series and span; a spanless series is a time series): the same value again is nothing,
        a different one is :class:`MetricReboundError`. The first objective reading of the input's gate
        is the input's measurement, written once as a typed fact (:meth:`measure_input`).
        """
        self._declare_metric(point.series, unit, direction, role)
        key = (point.series, point.span)
        value = (point.value, point.clock)
        known = self.metric_values.get(key)
        if point.span and known is not None and known[0] != point.value:
            message = f"{point.series} at {point.span}: {known[0]} cannot become {point.value}"
            raise MetricReboundError(message)
        if input_gate and point.span is not None and self._is_objective(point.series):
            self.measure_input(point.series, point.value, point.span)
        if known != value:
            self.metric_values[key] = value
            self.emit("metric", point)

    def _is_objective(self, series: str) -> bool:
        run = None if self.header is None else self.header.run
        return run is not None and bool(run.objectives) and run.objectives[0].series == series

    def measure_input(self, series: str, value: float, span: str) -> None:
        """Write the input's objective measurement into the epoch declaration, once."""
        current = self.input_measurement
        measured = measured_input(current)
        if measured is not None and not self.input_inherited:
            if measured.value != value or measured.series != series:
                message = f"the input measured {measured.value}, not {value} ({series} at {span})"
                raise InputReboundError(message)
            return
        self.input_inherited = False
        self._set_input(
            p.InputMeasurement(
                state=p.Oneof("measured", p.MeasuredInput(series=series, value=value, span=span))
            )
        )

    def give_up_input(self) -> None:
        """Say that the input was not measured, unless it already was."""
        current = self.input_measurement
        if measured_input(current) is not None:
            return
        self._set_input(p.InputMeasurement(state=p.Oneof("unmeasured", p.UnmeasuredInput())))

    def _set_input(self, measurement: p.InputMeasurement) -> None:
        self.input_measurement = measurement
        if self.header is None or self.header.run is None:
            return
        header = p.EpochStarted.from_binary(self.header.to_binary())
        if header.run is not None:
            header.run.input_measurement = measurement
        self.emit("declaration_amended", header)

    def _declare_metric(
        self, series: str, unit: str, direction: p.Direction, role: p.SeriesRole
    ) -> None:
        if self.header is None:
            return
        header = p.EpochStarted.from_binary(self.header.to_binary())
        run = header.run
        if run is not None and self.primary_series == series:
            if not run.objectives:
                # The oldest journals report readings with no direction. The v1 reader reads an
                # undirected objective as "maximize", so the converted run does too, until a
                # reading names its direction.
                self.objective_inferred = direction is p.Direction.NONE
                direction = p.Direction.MAX if self.objective_inferred else direction
                run.objectives.append(p.Objective(series=series, direction=direction))
                role = p.SeriesRole.OBJECTIVE
            elif self.objective_inferred and direction in {p.Direction.MIN, p.Direction.MAX}:
                self.objective_inferred = False
                run.objectives[0].direction = direction
                role = p.SeriesRole.OBJECTIVE
        existing = next((s for s in header.series if s.name == series), None)
        if existing is None:
            header.series.append(
                p.MetricSeriesDecl(name=series, unit=unit, direction=direction, role=role)
            )
        elif role is p.SeriesRole.OBJECTIVE:
            existing.role = role
            existing.direction = direction
            existing.unit = unit
        if header != self.header:
            self.emit("declaration_amended", header)

    def emit(self, field: str, value: Message) -> None:
        if (
            isinstance(value, p.Lifecycle)
            and value.state is not None
            and value.state.field == "ended"
        ):
            ending = value.state.value
            previous = None if self.ending is None else p.Ended.from_binary(self.ending)
            if self.epoch_ended and (
                self.ending == ending.to_binary()
                or (
                    previous is not None
                    and ending.outcome not in {previous.outcome, p.Outcome.FAILED}
                )
            ):
                return
            self.epoch_ended = True
            self.ending = ending.to_binary()
        self.bound(value, "run")
        if isinstance(value, p.EpochStarted):
            if self.header is not None:
                value.span_kinds = list(
                    {k.kind: k for k in [*self.header.span_kinds, *value.span_kinds]}.values()
                )
                value.series = list(
                    {s.name: s for s in [*self.header.series, *value.series]}.values()
                )
                if value.run is None:
                    value.run = self.header.run
            self.header = value

        record = p.Record(
            seq=self.state.sequence + len(self.records) + 1,
            epoch=self.state.epoch,
            clock=self.clock,
            at=p.Timestamp.from_datetime(self.event.timestamp),
        )
        descriptor = next(f for f in record.desc().fields if f.name == field)
        record[descriptor] = type(value).from_binary(value.to_binary())
        p.validate(record)
        if len(record.to_binary()) > p.RUN_LOG_MAX_RECORD_BYTES:
            message = f"{self.event.type}: record exceeds {p.RUN_LOG_MAX_RECORD_BYTES} bytes"
            raise ValueError(message)
        self.records.append(record)

    def blob(self, content: bytes, media_type: str = "text/plain; charset=utf-8") -> p.BlobRef:
        digest = hashlib.sha256(content).hexdigest()
        self.blobs.append(BlobWrite(digest, content))
        return p.BlobRef(sha256=digest, size=len(content), media_type=media_type)

    def attach(self, span: str, role: str, content: str) -> None:
        self.emit("blob", p.BlobAttached(span=span, role=role, blob=self.blob(content.encode())))

    def text(self, span: str, role: str, value: str) -> str:
        if len(value.encode()) <= INLINE_BYTES:
            return value
        blob = self.blob(value.encode())
        self.emit("blob", p.BlobAttached(span=span, role=role, blob=blob))
        return "blob:" + blob.sha256

    def span(self, span: p.Span, change: p.Change = p.Change.UPDATED) -> None:
        self.bound(span, span.id)
        self.spans[span.id] = span
        self.emit("span", p.SpanRecord(change=change, span=span))

    def result(self) -> MappedEvent:
        through = self.state.source_through
        pending = self.state.source_pending
        sequence = self.event.sequence
        if sequence > through:
            if sequence == through + 1:
                through = sequence
                while through + 1 in pending:
                    through += 1
                    pending = pending.discard(through)
            else:
                pending = pending.insert(sequence)
        state = replace(
            self.state,
            sequence=self.state.sequence + len(self.records),
            origin=self.origin,
            clock=self.clock,
            epoch_ended=self.epoch_ended,
            header_written=True,
            epoch_header=None if self.header is None else self.header.to_binary(),
            spans=tuple((key, value.to_binary()) for key, value in self.spans.items()),
            invocations=tuple(self.invocations.items()),
            streams=tuple(self.lengths.items()),
            waits=tuple(self.waits.items()),
            intents=tuple(self.intents.items()),
            primary_series=self.primary_series,
            input_measurement=None
            if self.input_measurement is None
            else self.input_measurement.to_binary(),
            input_inherited=self.input_inherited,
            objective_inferred=self.objective_inferred,
            metric_values=tuple(self.metric_values.items()),
            source_through=through,
            source_pending=pending,
            ending=self.ending,
            pending=tuple(self.pending),
            activities=tuple(self.activities.items()),
        )
        return MappedEvent(state, tuple(self.records), tuple(self.blobs), tuple(self.streams))
