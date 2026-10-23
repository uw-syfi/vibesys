"""Pure store-record projection; content and intents stay in the log."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from vs_proto.api import (
    EpochVersion,
    Keyframe,
    LifecycleVersion,
    MetricHistory,
    MetricPoint,
    MetricVersion,
    Record,
    Severity,
    Span,
    SpanHistory,
    SpanVersion,
    StreamRef,
)


def fold_skeleton(
    records: Iterable[Record], *, log_offset: int, seed: Keyframe | None = None
) -> Keyframe:
    """Fold dense records without mutating inputs; offset follows the last frame.

    Additive record bodies and legacy payloads advance the cursor without
    changing the skeleton. A seed includes all history through its sequence.
    """
    # A fresh fold has seen every record; one seeded from an older keyframe inherits its flag.
    result = (
        Keyframe.from_binary(seed.to_binary())
        if seed is not None
        else Keyframe(failures_tracked=True)
    )
    for record in records:
        append_skeleton(result, record)
    result.log_offset = log_offset
    return result


def append_skeleton(state: Keyframe, record: Record) -> None:
    """Append to an exclusively owned projection without copying its history."""
    if record.seq != state.through_seq + 1:
        message = f"record.seq: expected {state.through_seq + 1}, got {record.seq}"
        raise ValueError(message)
    owned = Record.from_binary(record.to_binary())
    body = owned.body
    if body is not None:
        match body.field:
            case "span":
                span = _required_span(body.value.span)
                history = state.spans.setdefault(span.id, SpanHistory())
                history.versions.append(SpanVersion(seq=record.seq, span=span))
            case "metric":
                _append_metric(state, record.seq, body.value)
            case "stream":
                _put_stream(state, body.value)
            case "epoch_started" | "declaration_amended":
                state.epochs.append(EpochVersion(seq=record.seq, epoch=body.value))
            case "lifecycle":
                state.lifecycles.append(LifecycleVersion(seq=record.seq, lifecycle=body.value))
            case "notice" if body.value.severity >= Severity.ERROR:
                # The latest error notice of each epoch stands for all of them.
                state.failures = [
                    *(kept for kept in state.failures if kept.epoch != owned.epoch),
                    owned,
                ]
    _append_input_fact(state, owned)
    state.through_seq = record.seq


def _append_metric(state: Keyframe, seq: int, point: MetricPoint) -> None:
    history = next((h for h in state.metrics if h.series == point.series), None)
    if history is None:
        history = MetricHistory(series=point.series)
        state.metrics.append(history)
    history.points.append(MetricVersion(seq=seq, point=point))


def _put_stream(state: Keyframe, stream: StreamRef) -> None:
    index = next((i for i, s in enumerate(state.streams) if s.id == stream.id), None)
    if index is None:
        state.streams.append(stream)
    else:
        state.streams[index] = stream


def _required_span(span: Span | None) -> Span:
    if span is None:
        message = "record.span.span is required"
        raise ValueError(message)
    return span


def _append_input_fact(state: Keyframe, record: Record) -> None:
    if record.input_measurement is None or not state.epochs:
        return
    latest = state.epochs[-1].epoch
    if latest is None or latest.run is None:
        return
    declaration = type(latest).from_binary(latest.to_binary())
    if declaration.run is not None:
        declaration.run.input_measurement = record.input_measurement
    state.epochs.append(EpochVersion(seq=record.seq, epoch=declaration))
