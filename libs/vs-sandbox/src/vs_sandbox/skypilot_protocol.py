"""Versioned JSON-lines contract for the SkyPilot evaluator bridge."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

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


class OutputFrame(_Message):
    """One streamed evaluator output fragment."""

    type: Literal["stdout", "stderr"]
    data: str


class ResultFrame(_Message):
    """Terminal remote job result."""

    type: Literal["result"] = "result"
    status: Literal["COMPLETED", "APPLICATION_FAILED", "CANCELLED"]
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


def decode_request(payload: bytes) -> EvaluationRequest:
    """Decode and strictly validate one request line."""
    return EvaluationRequest.model_validate_json(payload, strict=True)


def decode_ack(payload: bytes) -> AckRequest:
    """Decode and strictly validate a terminal acknowledgement."""
    return AckRequest.model_validate_json(payload, strict=True)


def decode_response(payload: bytes) -> ResponseFrame:
    """Decode and strictly validate one response line."""
    return _RESPONSE_ADAPTER.validate_json(payload, strict=True)


def compact_json(value: object) -> str:
    """Render deterministic compact JSON for protocol diagnostics and tests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
