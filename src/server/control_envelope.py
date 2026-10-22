"""The run protocol's control envelope, light enough for a host command.

A client that only stops a run (``vibesys instances stop``, the host helper's
``run.stop``) needs the request and response framing of the run protocol and
the ``command.stop`` message, not the run's payload types, which import the
engine. This module holds that framing; ``server.api.protocol`` builds the
full protocol on it and re-exports every name, so the wire format and the
published JSON Schema are unchanged. It imports only Pydantic, the standard
library, and light ``server`` modules; ``tests/architecture/test_light_entrypoints.py``
keeps it that way.

Public surface: ``ProtocolModel``, ``Request``, ``StopCommand``, ``CommandAckEnvelope``,
``ResponseEnvelope``, and ``ResponseEnvelopeReader``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
    """Internal detached-host shutdown at its cooperative boundary."""

    type: Literal["command.stop"] = "command.stop"
    mode: Literal["after_current_agent_call"] = "after_current_agent_call"


class CommandAckEnvelope(ProtocolModel):
    """Acknowledgment of a requested run command, as a light client reads it.

    ``server.api.protocol.CommandAck`` narrows ``action`` to the control actions
    (``vibesys.api.ControlAction``), which this light module cannot import; the full
    ``Response`` declares that one, so the published schema names every action.
    """

    action: str
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
    ack: CommandAckEnvelope | None = None


class ResponseEnvelopeReader(ResponseEnvelope):
    """Read the envelope of a full ``Response`` without its payload types.

    A server writes every ``Response`` field, payloads included. A light client
    validates the envelope fields strictly and accepts each payload field
    ``server.api.protocol.Response`` declares as any JSON value, unread. Like
    every protocol model it rejects a key the full ``Response`` does not have;
    ``tests/server/test_control_envelope.py`` keeps the payload names below
    equal to ``Response``'s.
    """

    chat: JsonValue = None
    chat_thread: JsonValue = None
    snapshot: JsonValue = None
    events: JsonValue = None
    performance: JsonValue = None
    performance_context: JsonValue = None
    experiments: JsonValue = None
    experiment_update: JsonValue = None
    experiments_ready: JsonValue = None
    iterations: JsonValue = None
    iterations_ready: JsonValue = None
    design: JsonValue = None
    design_ready: JsonValue = None
    design_patch: JsonValue = None
