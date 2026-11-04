"""JSON-lines contract between the Slurm process broker and its confined client.

The client sends one ``ProcessCall`` line; the broker answers with one ``BrokerReply``
line and hangs up. Both sides read the same models, so neither can drift from the other.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vs_slurm.api import MAX_TRANSPORT_STREAM_BYTES

MAX_FRAME_BYTES = 16 * 1024 * 1024
"""The longest line either side reads, newline included."""


class ProcessCall(BaseModel):
    """Run one transport process with the host's credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    token: str
    argv: tuple[str, ...] = Field(min_length=1)
    stdin: str | None
    timeout: float = Field(gt=0)
    max_output_bytes: int = Field(ge=0, le=MAX_TRANSPORT_STREAM_BYTES)


class ProcessReply(BaseModel):
    """How the transport process ended."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    returncode: int
    stdout: str
    stderr: str


class BrokerRan(BaseModel):
    """The broker ran the call."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ok: Literal[True]
    result: ProcessReply


class BrokerRefused(BaseModel):
    """The broker refused the call, or running it failed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    ok: Literal[False]
    error: str


type BrokerReply = Annotated[BrokerRan | BrokerRefused, Field(discriminator="ok")]

BROKER_REPLY: TypeAdapter[BrokerRan | BrokerRefused] = TypeAdapter(BrokerReply)
"""Decodes one reply line; ``pydantic.ValidationError`` for anything else."""


__all__ = [
    "BROKER_REPLY",
    "MAX_FRAME_BYTES",
    "BrokerRan",
    "BrokerRefused",
    "BrokerReply",
    "ProcessCall",
    "ProcessReply",
]
