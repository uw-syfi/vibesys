"""Turn enrichment and stream content, projected without filesystem access."""

from __future__ import annotations

import hashlib
import json

import vibesys.events as e
import vs_proto.api as p
from vibesys.records._common import INLINE_BYTES, Projection, enum_value
from vibesys.records._types import StreamWrite


def owner(projection: Projection) -> str:
    event = projection.event
    data = event.data
    if isinstance(data, e.TurnUsageData) and data.span_id is not None:
        return data.span_id
    identity = event.execution_id
    if identity is not None:
        return projection.invocations.get(identity, "turn:" + identity)
    if event.agent_kind is None and isinstance(
        data, (e.AgentOutputChunkData, e.ToolCallData, e.ToolResultData)
    ):
        return "run"
    # Old events have no invocation identity. Keep a distinct, explicit owner
    # rather than attributing a concurrent turn's content by arrival order.
    return f"unattributed:{event.agent_kind or 'run'}:{event.round_label or 'run'}"


def turn(projection: Projection) -> tuple[p.Span, p.AgentTurnAttrs]:
    span_id = owner(projection)
    existing = projection.spans.get(span_id)
    if existing is not None and existing.label is not None and existing.label.attrs is not None:
        attrs = p.AgentTurnAttrs.from_binary(existing.label.attrs.value)
        return existing, attrs
    role = projection.event.agent_kind or "unattributed"
    span = p.Span(
        id=span_id,
        runtime_type=p.RuntimeType.OPERATION,
        name=role,
        status=p.SpanStatus.RUNNING,
        started_at=projection.clock,
    )
    return span, p.AgentTurnAttrs(agent=role, role=role, stage=role)


def update_turn(projection: Projection, span: p.Span, attrs: p.AgentTurnAttrs) -> None:
    if span.label is None:
        span.label = p.Label(kind="agent_turn", attrs=projection.pack(attrs, span.id))
    else:
        span.label.attrs = projection.pack(attrs, span.id)
    projection.span(span)


def started(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.AgentExecutionStartedData):
        message = "agent_execution_started requires AgentExecutionStartedData"
        raise TypeError(message)
    span, attrs = turn(projection)
    attrs.started_at = p.Timestamp.from_datetime(projection.event.timestamp)
    for name in ("stage", "attempt", "provider", "model", "reasoning_effort", "harness_version"):
        value = getattr(data, name)
        if value is not None:
            setattr(attrs, name, value)
    update_turn(projection, span, attrs)
    for role in ("system_prompt", "user_prompt"):
        projection.attach(span.id, role, getattr(data, role))


def finished(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.AgentExecutionFinishedData):
        message = "agent_execution_finished requires AgentExecutionFinishedData"
        raise TypeError(message)
    span, attrs = turn(projection)
    if data.result is not None:
        projection.attach(
            span.id,
            "raw_result",
            data.result
            if isinstance(data.result, str)
            else json.dumps(e.json_value(data.result), ensure_ascii=False),
        )
    if data.reply is not None:
        text = data.reply.model_dump_json()
        if len(text.encode()) > INLINE_BYTES:
            projection.attach(span.id, "reply", text)
        else:
            attrs.reply_json = text
    if data.error is not None:
        span.failure = p.Failure(
            kind=p.SpanFailureKind.OUTCOME, message=projection.text(span.id, "error", data.error)
        )
    statuses = {
        e.EventStatus.FAILED: p.SpanStatus.FAILED,
        e.EventStatus.CANCELLED: p.SpanStatus.STOPPED,
        e.EventStatus.INTERRUPTED: p.SpanStatus.STOPPED,
    }
    span.status = statuses.get(
        projection.event.status, p.SpanStatus.FAILED if data.error else p.SpanStatus.DONE
    )
    span.ended_at = max(span.started_at, projection.clock)
    attrs.ended_at = p.Timestamp.from_datetime(projection.event.timestamp)
    attrs.transcript_length = projection.lengths.get(stream_id(span.id), 0)
    update_turn(projection, span, attrs)


def usage(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.TurnUsageData):
        message = "turn_usage_recorded requires TurnUsageData"
        raise TypeError(message)
    span, attrs = turn(projection)
    attrs.ended_at = p.Timestamp.from_datetime(projection.event.timestamp)
    attrs.usage = p.TokenUsage(
        input=data.input_tokens,
        cache_read=data.cache_read_input_tokens,
        cache_creation=data.cache_creation_input_tokens,
        output=data.output_tokens,
        reasoning=data.reasoning_output_tokens,
        cost_usd=data.total_cost_usd,
        duration_ms=data.duration_ms,
    )
    update_turn(projection, span, attrs)


def stream_id(span: str) -> str:
    """Opaque, path-safe id accepted by the gateway, independent of span syntax."""
    return "transcript-" + hashlib.sha256(span.encode()).hexdigest()


def content(projection: Projection, span: str, role: str, text: str) -> p.TranscriptContent:
    if len(text.encode()) <= INLINE_BYTES:
        return p.TranscriptContent(value=p.Oneof("inline", text))
    blob = projection.blob(text.encode())
    projection.emit("blob", p.BlobAttached(span=span, role=role, blob=blob))
    return p.TranscriptContent(value=p.Oneof("blob", blob))


def transcript(projection: Projection) -> None:
    data = projection.event.data
    span_id = owner(projection)
    if span_id == "run":
        stream(
            projection,
            p.StreamRef(
                id="run-output",
                kind=p.StreamKind.LOG,
                state=p.StreamState.OPENED,
                title="Run output",
            ),
            (
                data.content
                if isinstance(data, e.AgentOutputChunkData)
                else data.model_dump_json() + "\n"
            ).encode(),
        )
        return
    if isinstance(data, e.AgentOutputChunkData):
        if data.channel == "tool":
            return  # OQ13: typed tool_call/tool_result entries own tool content.
        entry = p.TranscriptEntry(
            entry=p.Oneof(
                "text",
                p.TranscriptText(
                    channel=enum_value(p.TranscriptChannel, data.channel),
                    content=content(projection, span_id, "transcript.content", data.content),
                ),
            )
        )
    elif isinstance(data, e.ToolCallData):
        entry = p.TranscriptEntry(
            entry=p.Oneof(
                "tool_call",
                p.TranscriptToolCall(
                    tool=data.tool,
                    call_id=data.call_id,
                    arguments_json=content(
                        projection,
                        span_id,
                        "tool.arguments",
                        json.dumps(data.args, ensure_ascii=False),
                    ),
                ),
            )
        )
    elif isinstance(data, e.ToolResultData):
        entry = p.TranscriptEntry(
            entry=p.Oneof(
                "tool_result",
                p.TranscriptToolResult(
                    tool=data.tool,
                    call_id=data.call_id,
                    is_error=data.is_error,
                    content=content(projection, span_id, "tool.content", data.content),
                    payload_json=None
                    if data.payload is None
                    else content(
                        projection, span_id, "tool.payload", data.payload.model_dump_json()
                    ),
                ),
            )
        )
    else:
        message = "transcript event requires output, tool call or result payload"
        raise TypeError(message)
    projection.bound(entry, span_id)
    p.validate(entry)
    if span_id not in projection.spans:
        span, attrs = turn(projection)
        update_turn(projection, span, attrs)
    wire = entry.to_binary()
    length = len(wire)
    prefix = bytearray()
    while length >= 1 << 7:
        prefix.append((length & 127) | 128)
        length >>= 7
    prefix.append(length)
    stream(
        projection,
        p.StreamRef(
            id=stream_id(span_id),
            span=span_id,
            kind=p.StreamKind.TRANSCRIPT,
            state=p.StreamState.OPENED,
        ),
        bytes(prefix) + wire,
    )


def stream(projection: Projection, reference: p.StreamRef, content: bytes) -> None:
    first = reference.id not in projection.lengths
    offset = projection.lengths.get(reference.id, 0)
    projection.streams.append(StreamWrite(reference.id, offset, content))
    projection.lengths[reference.id] = offset + len(content)
    if first:
        projection.emit("stream", reference)


def subprocess(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, e.SubprocessOutputData):
        message = "subprocess_output requires SubprocessOutputData"
        raise TypeError(message)
    reference = p.StreamRef(
        id="process-" + hashlib.sha256((data.process_id + ":" + data.stream).encode()).hexdigest(),
        span=data.span_id,
        kind=p.StreamKind.LOG,
        state=p.StreamState.OPENED,
        title=data.process_kind + ":" + data.stream,
    )
    stream(projection, reference, data.content.encode())


MAPPERS = {
    e.CoreEventType.AGENT_EXECUTION_STARTED: started,
    e.CoreEventType.AGENT_EXECUTION_FINISHED: finished,
    e.CoreEventType.TURN_USAGE_RECORDED: usage,
    e.CoreEventType.AGENT_OUTPUT_CHUNK: transcript,
    e.CoreEventType.TOOL_CALL: transcript,
    e.CoreEventType.TOOL_RESULT: transcript,
    e.CoreEventType.SUBPROCESS_OUTPUT: subprocess,
}
