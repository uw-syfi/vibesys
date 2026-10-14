"""The run protocol's control envelope, light enough for a host command.

A client that only stops a run (``vibesys instances stop``, the host helper's
``run.stop``) needs the request and response framing of the run protocol and
the ``command.stop`` message, not the run's payload types, which import the
engine. This module holds that framing; ``server.api.protocol`` builds the
full protocol on it and re-exports every name, so the wire format and the
published JSON Schema are unchanged. It imports only Pydantic, the standard
library, and light ``server`` modules; ``tests/architecture/test_light_entrypoints.py``
keeps it that way.

Public surface: ``ProtocolModel``, ``Request``, ``StopCommand``, ``CommandAck``,
``ResponseEnvelope``, and ``ResponseEnvelopeReader``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from server.diagnostics import Diagnostic
from server.host_api import PROTOCOL_VERSION


class ProtocolModel(BaseModel):
    """Base model for versioned transport messages."""

    model_config = ConfigDict(extra="forbid")


class Request(ProtocolModel):
    """Common version, identity, and timestamp fields for client requests."""

    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # Stable for one frontend client across its control, subscription, and
    # dedicated chat connections. Empty preserves compatibility with clients
    # and servers that predate connection attribution.
    client_id: str = ""
    # When the sender made the request, by the sender's clock. Required so no
    # request is stamped by a clock the sender did not inject.
    timestamp: datetime


class StopCommand(Request):
    """Request stopping after the active agent call."""

    type: Literal["command.stop"] = "command.stop"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class CommandAck(ProtocolModel):
    """Acknowledgment of a requested run command."""

    action: Literal["pause", "resume", "steer", "stop"]
    status: Literal["pending", "consumed"]


class ResponseEnvelope(ProtocolModel):
    """The fields every response carries before its payload.

    ``server.api.protocol.Response`` extends this with the payload fields, in
    this order, so the published schema is the one the full model always had.
    """

    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str
    client_id: str = ""
    timestamp: datetime
    ok: bool = True
    error: str | None = None
    diagnostic: Diagnostic | None = None
    ack: CommandAck | None = None


class ResponseEnvelopeReader(ResponseEnvelope):
    """Read the envelope of a full ``Response`` without its payload types.

    A server writes every ``Response`` field, payloads included. A light client
    validates the envelope fields strictly and leaves the payload fields, which
    belong to ``server.api.protocol``, unread. This is the one model that does
    not reject unknown keys, and only a reader of server responses uses it.
    """

    model_config = ConfigDict(extra="ignore")
    unknown_keys_reason: ClassVar[str] = (
        "Reads the envelope of a full Response and leaves its payload fields unread."
    )
