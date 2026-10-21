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
        self.metric_values = dict(state.metric_values)
        self.records: list[p.Record] = []
        self.blobs: list[BlobWrite] = []
        self.streams: list[StreamWrite] = []
        self.ending = state.ending
        self.pending = list(state.pending)
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
        self.emit("epoch_started", header)

    def metric(
        self,
        point: p.MetricPoint,
        *,
        unit: str = "",
        direction: p.Direction = p.Direction.NONE,
        role: p.SeriesRole = p.SeriesRole.REFERENCE,
    ) -> None:
        if self.header is not None and not any(s.name == point.series for s in self.header.series):
            header = p.EpochStarted.from_binary(self.header.to_binary())
            header.series.append(
                p.MetricSeriesDecl(name=point.series, unit=unit, direction=direction, role=role)
            )
            self.emit("epoch_started", header)
        key = (point.series, point.span)
        if self.metric_values.get(key) != point.value:
            self.metric_values[key] = point.value
            self.emit("metric", point)

    def emit(self, field: str, value: Message) -> None:
        if (
            isinstance(value, p.Lifecycle)
            and value.state is not None
            and value.state.field == "ended"
        ):
            if self.epoch_ended:
                return
            self.epoch_ended = True
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
            metric_values=tuple(self.metric_values.items()),
            ending=self.ending,
            pending=tuple(self.pending),
        )
        return MappedEvent(state, tuple(self.records), tuple(self.blobs), tuple(self.streams))
