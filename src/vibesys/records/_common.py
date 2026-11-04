"""Shared projection mechanics: copying, content addressing and envelopes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING

from protobuf import Message
from protobuf.wkt.google.protobuf.any_pb import Any

import vs_proto.api as p
from vibesys.records._draft import MapDraft, MessageDraft
from vibesys.records._header import EpochHeader, SeriesFacts
from vibesys.records._span_table import SpanDraft
from vibesys.records._types import BlobWrite, MappedEvent, MappingState, StreamWrite

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _add_objective(header: p.EpochStarted, *, series: str, direction: p.Direction) -> None:
    if header.run is not None:
        header.run.objectives.append(p.Objective(series=series, direction=direction))


def _direct_objective(header: p.EpochStarted, *, direction: p.Direction) -> None:
    if header.run is not None:
        header.run.objectives[0].direction = direction


def _add_series(header: p.EpochStarted, *, row: p.MetricSeriesDecl) -> None:
    header.series.append(row)


def _restate_series(
    header: p.EpochStarted, *, name: str, unit: str, direction: p.Direction, role: p.SeriesRole
) -> None:
    row = next(s for s in header.series if s.name == name)
    row.role = role
    row.direction = direction
    row.unit = unit


class Projection:
    """A local builder; no mutable value is shared with its input state.

    Everything it holds of the state is read through a draft that decodes on first use and
    encodes only what the event changed, so an event costs what it touches, not the run so far.
    """

    def __init__(self, state: MappingState, event: CoreEvent) -> None:
        self.state = state
        self.event = event
        self.origin = event.timestamp.timestamp() if state.origin is None else state.origin
        self.clock = max(
            state.clock, state.clock_offset + event.timestamp.timestamp() - self.origin
        )
        self.spans = SpanDraft(state.span_table)
        self.invocations = MapDraft(state.invocations)
        self.lengths = MapDraft(state.streams)
        self.waits = MapDraft(state.waits)
        self.intents = MapDraft(state.intents)
        self.primary_series = state.primary_series
        self.objective_inferred = state.objective_inferred
        self._input = MessageDraft(p.InputMeasurement, state.input_measurement)
        self.input_inherited = state.input_inherited
        self.metric_values = MapDraft(state.metric_values)
        self.records: list[p.Record] = []
        self.blobs: list[BlobWrite] = []
        self.streams: list[StreamWrite] = []
        self.ending = state.ending
        self.pending = list(state.pending)
        self.final = False
        self.epoch_ended = state.epoch_ended
        self.header = state.epoch_header

    @property
    def input_measurement(self) -> p.InputMeasurement | None:
        """The input's measurement fact; replace it to change it."""
        return self._input.value

    @input_measurement.setter
    def input_measurement(self, measurement: p.InputMeasurement | None) -> None:
        self._input.value = measurement

    def series_declaration(self, name: str) -> SeriesFacts | None:
        """The epoch declaration's first row for the metric series ``name``."""
        return None if self.header is None else self.header.declared.series.get(name)

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
        if self.header is None or kind in self.header.declared.span_kinds:
            return
        header = self.header.decode()
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
        objectives = () if self.header is None else self.header.declared.objectives
        return bool(objectives) and objectives[0][0] == series

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
        if self.header is None or not self.header.declared.has_run:
            return
        header = self.header.decode()
        if header.run is not None:
            header.run.input_measurement = measurement
        self.emit("declaration_amended", header)

    def _declare_metric(
        self, series: str, unit: str, direction: p.Direction, role: p.SeriesRole
    ) -> None:
        """Declare ``series`` as the reading needs, amending the epoch declaration only if it changes.

        The declaration is read from its declared facts and decoded, once, only when an amendment
        is due: a reading of a series already declared as it needs costs nothing here.
        """
        if self.header is None:
            return
        declared = self.header.declared
        edits: list[Callable[[p.EpochStarted], None]] = []
        if declared.has_run and self.primary_series == series:
            if not declared.objectives:
                # The oldest journals report readings with no direction. The v1 reader reads an
                # undirected objective as "maximize", so the converted run does too, until a
                # reading names its direction.
                self.objective_inferred = direction is p.Direction.NONE
                direction = p.Direction.MAX if self.objective_inferred else direction
                edits.append(partial(_add_objective, series=series, direction=direction))
                role = p.SeriesRole.OBJECTIVE
            elif self.objective_inferred and direction in {p.Direction.MIN, p.Direction.MAX}:
                self.objective_inferred = False
                if declared.objectives[0][1] != direction:
                    edits.append(partial(_direct_objective, direction=direction))
                role = p.SeriesRole.OBJECTIVE
        existing = declared.series.get(series)
        if existing is None:
            row = p.MetricSeriesDecl(name=series, unit=unit, direction=direction, role=role)
            edits.append(partial(_add_series, row=row))
        elif role is p.SeriesRole.OBJECTIVE and existing != SeriesFacts(unit, direction, role):
            edits.append(
                partial(_restate_series, name=series, unit=unit, direction=direction, role=role)
            )
        if edits:
            amended = self.header.decode()
            for edit in edits:
                edit(amended)
            self.emit("declaration_amended", amended)

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
        if isinstance(value, p.EpochStarted) and self.header is not None:
            previous = self.header.decode()
            value.span_kinds = list(
                {k.kind: k for k in [*previous.span_kinds, *value.span_kinds]}.values()
            )
            value.series = list({s.name: s for s in [*previous.series, *value.series]}.values())
            if value.run is None:
                value.run = previous.run
        wire = value.to_binary()
        if isinstance(value, p.EpochStarted):
            self.header = EpochHeader.of(value, wire)

        record = p.Record(
            seq=self.state.sequence + len(self.records) + 1,
            epoch=self.state.epoch,
            clock=self.clock,
            at=p.Timestamp.from_datetime(self.event.timestamp),
        )
        descriptor = next(f for f in record.desc().fields if f.name == field)
        record[descriptor] = type(value).from_binary(wire)
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
            epoch_header=self.header,
            span_table=self.spans.commit(),
            invocations=self.invocations.commit(),
            streams=self.lengths.commit(),
            waits=self.waits.commit(),
            intents=self.intents.commit(),
            primary_series=self.primary_series,
            input_measurement=self._input.wire,
            input_inherited=self.input_inherited,
            objective_inferred=self.objective_inferred,
            metric_values=self.metric_values.commit(),
            source_through=through,
            source_pending=pending,
            ending=self.ending,
            pending=tuple(self.pending),
        )
        return MappedEvent(state, tuple(self.records), tuple(self.blobs), tuple(self.streams))
