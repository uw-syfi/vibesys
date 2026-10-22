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
    MetricVersion,
    Record,
    Span,
    SpanHistory,
    SpanVersion,
)


def fold_skeleton(
    records: Iterable[Record], *, log_offset: int, seed: Keyframe | None = None
) -> Keyframe:
    """Fold dense records without mutating inputs; offset follows the last frame.

    Additive record bodies and legacy payloads advance the cursor without
    changing the skeleton. A seed includes all history through its sequence.
    """
    result = Keyframe.from_binary(seed.to_binary()) if seed is not None else Keyframe()
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
                point = body.value
                history = next((h for h in state.metrics if h.series == point.series), None)
                if history is None:
                    history = MetricHistory(series=point.series)
                    state.metrics.append(history)
                history.points.append(MetricVersion(seq=record.seq, point=point))
            case "stream":
                stream = body.value
                index = next((i for i, s in enumerate(state.streams) if s.id == stream.id), None)
                if index is None:
                    state.streams.append(stream)
                else:
                    state.streams[index] = stream
            case "epoch_started" | "declaration_amended":
                state.epochs.append(EpochVersion(seq=record.seq, epoch=body.value))
            case "lifecycle":
                state.lifecycles.append(LifecycleVersion(seq=record.seq, lifecycle=body.value))
    state.through_seq = record.seq


def _required_span(span: Span | None) -> Span:
    if span is None:
        message = "record.span.span is required"
        raise ValueError(message)
    return span
