"""Versioned JSON-lines contract for the SkyPilot evaluator bridge."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vs_decode.api import Unreadable, ValidationOptions, decode_json
from vs_sandbox.skypilot_states import JobStatus

PROTOCOL_VERSION = 2


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[2] = PROTOCOL_VERSION


class EvaluationRequest(_Message):
    """Request one server-selected trusted evaluator."""

    kind: Literal["accuracy", "benchmark"]
    invocation_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    arguments: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    step_tag: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    """The step whose agent turn asked for this evaluation, from the mark its process carries.

    A step that is run again cancels the jobs its earlier attempt left by this tag. It is not
    part of the request's identity: a retried invocation of another attempt is the same request.
    """


class OutputFrame(_Message):
    """One streamed evaluator output fragment."""

    type: Literal["stdout", "stderr"]
    data: str


class ResultFrame(_Message):
    """Terminal remote job result."""

    type: Literal["result"] = "result"
    status: JobStatus
    sky_exit_code: int
    remote_job_id: int


class ArtifactFrame(_Message):
    """One bounded requested result artifact."""

    type: Literal["artifact"] = "artifact"
    path: str
    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    data_base64: str


class AckRequest(_Message):
    """Confirm atomic client delivery of one terminal result."""

    type: Literal["ack"] = "ack"
    invocation_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class AckedFrame(_Message):
    """Confirm that acknowledgement is durable."""

    type: Literal["acked"] = "acked"
    invocation_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class ErrorFrame(_Message):
    """Terminal bridge error without process output or credentials."""

    type: Literal["error"] = "error"
    error: str


ResponseFrame = Annotated[
    OutputFrame | ArtifactFrame | ResultFrame | AckedFrame | ErrorFrame,
    Field(discriminator="type"),
]
_RESPONSE_ADAPTER = TypeAdapter(ResponseFrame)


def encode_message(message: _Message) -> bytes:
    """Encode one bounded JSON-lines message."""
    return message.model_dump_json().encode() + b"\n"


MAX_FRAME_BYTES = 16 * 1024 * 1024
"""The largest protocol line decoded; framing limits in the bridge are tighter."""
_STRICT = ValidationOptions(strict=True)


class FrameDecodeError(ValueError):
    """A protocol line that is not a valid frame; names the problems, never the input."""

    def __init__(self, unreadable: Unreadable) -> None:
        """Describe the unreadable line."""
        super().__init__(str(unreadable))


def decode_request(payload: bytes) -> EvaluationRequest:
    """Decode and strictly validate one request line; ``FrameDecodeError`` if it is not one."""
    return _decoded(
        decode_json(
            payload,
            EvaluationRequest,
            source="bridge request",
            max_bytes=MAX_FRAME_BYTES,
            options=_STRICT,
        )
    )


def decode_ack(payload: bytes) -> AckRequest:
    """Decode and strictly validate a terminal acknowledgement; ``FrameDecodeError`` if not one."""
    return _decoded(
        decode_json(
            payload,
            AckRequest,
            source="bridge acknowledgement",
            max_bytes=MAX_FRAME_BYTES,
            options=_STRICT,
        )
    )


def decode_response(payload: bytes) -> ResponseFrame:
    """Decode and strictly validate one response line; ``FrameDecodeError`` if it is not one."""
    return _decoded(
        decode_json(
            payload,
            _RESPONSE_ADAPTER,
            source="bridge response",
            max_bytes=MAX_FRAME_BYTES,
            options=_STRICT,
        )
    )


def _decoded[FrameT](frame: FrameT | Unreadable) -> FrameT:
    if isinstance(frame, Unreadable):
        raise FrameDecodeError(frame)
    return frame


def compact_json(value: object) -> str:
    """Render deterministic compact JSON for protocol diagnostics and tests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
