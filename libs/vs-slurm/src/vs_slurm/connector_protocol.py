"""The versioned JSON contract spoken by a Slurm connector executable."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

MAX_CONNECTOR_REQUEST_BYTES = 1024 * 1024
"""Maximum UTF-8 size of one connector request document."""

MAX_TRANSPORT_STREAM_BYTES = 1024 * 1024
"""Maximum bytes retained from either stream of one brokered transport process.

The broker wraps both streams in a 16 MiB JSON frame. One MiB per stream leaves room even
when every byte needs JSON's six-byte ``\\u00xx`` spelling, as well as the reply envelope.
"""

MAX_CONNECTOR_RESPONSE_BYTES = MAX_TRANSPORT_STREAM_BYTES
"""Maximum UTF-8 size of one connector response document, including its envelope."""


def _canonical_local_path(value: str) -> str:
    path = Path(value)
    if "\x00" in value or not path.is_absolute() or str(path) != value or ".." in path.parts:
        message = "must be an absolute normalized local path"
        raise ValueError(message)
    return value


def _canonical_remote_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        "\x00" in value
        or not path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts[1:])
    ):
        message = "must be an absolute normalized POSIX path"
        raise ValueError(message)
    return value


class _ConnectorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]


class SlurmConnectorExecRequest(_ConnectorRequest):
    """Run one remote shell command through the operator's connector."""

    operation: Literal["exec"]
    command: Annotated[str, Field(min_length=1)]


class SlurmConnectorSyncToRequest(_ConnectorRequest):
    """Synchronize one local directory into the cluster namespace."""

    operation: Literal["sync_to"]
    local_dir: str
    remote_dir: str
    delete: bool
    excludes: tuple[str, ...]

    _local_is_canonical = field_validator("local_dir")(_canonical_local_path)
    _remote_is_canonical = field_validator("remote_dir")(_canonical_remote_path)

    @field_validator("excludes")
    @classmethod
    def _valid_excludes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            message = "excludes must contain non-empty strings"
            raise ValueError(message)
        return value


class SlurmConnectorPutRequest(_ConnectorRequest):
    """Copy one local file into the cluster namespace."""

    operation: Literal["put"]
    local_path: str
    remote_path: str

    _local_is_canonical = field_validator("local_path")(_canonical_local_path)
    _remote_is_canonical = field_validator("remote_path")(_canonical_remote_path)


class SlurmConnectorGetRequest(_ConnectorRequest):
    """Copy one remote file or directory into a run-owned local path."""

    operation: Literal["get"]
    remote_path: str
    local_path: str
    kind: Literal["file", "tree"]

    _remote_is_canonical = field_validator("remote_path")(_canonical_remote_path)
    _local_is_canonical = field_validator("local_path")(_canonical_local_path)


type SlurmConnectorRequest = Annotated[
    SlurmConnectorExecRequest
    | SlurmConnectorSyncToRequest
    | SlurmConnectorPutRequest
    | SlurmConnectorGetRequest,
    Field(discriminator="operation"),
]

SLURM_CONNECTOR_REQUEST: TypeAdapter[
    SlurmConnectorExecRequest
    | SlurmConnectorSyncToRequest
    | SlurmConnectorPutRequest
    | SlurmConnectorGetRequest
] = TypeAdapter(SlurmConnectorRequest)
"""Validate one complete connector request; unknown versions, operations and keys fail."""


class SlurmConnectorResponse(BaseModel):
    """The complete response from one connector invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1]
    returncode: int
    stdout: str
    stderr: str


__all__ = [
    "MAX_CONNECTOR_REQUEST_BYTES",
    "MAX_CONNECTOR_RESPONSE_BYTES",
    "MAX_TRANSPORT_STREAM_BYTES",
    "SLURM_CONNECTOR_REQUEST",
    "SlurmConnectorExecRequest",
    "SlurmConnectorGetRequest",
    "SlurmConnectorPutRequest",
    "SlurmConnectorRequest",
    "SlurmConnectorResponse",
    "SlurmConnectorSyncToRequest",
]
