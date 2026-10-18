"""What every host helper request and reply carries: the fields the channel itself reads.

A request names itself (``request_id``) and how long the client waits (``deadline_ms``); a reply
names the request it answers. Each message of the helper, in ``server.host_api`` and in the
contract modules it imports (``server.host_run_summary``), derives from these two bases, so a
feature's messages can live in a module of their own without reaching into ``host_api``.

Light: Pydantic and the standard library only.

Public surface: ``RequestId``, ``RunId``, ``SpanId``, ``MAX_DEADLINE_MS``, ``HelperRequestBase`` and
``HelperReplyBase``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
"""Chosen by the client, unique while the channel owns its request or watch worker."""

RunId = Annotated[str, StringConstraints(min_length=1)]
"""The durable run a server drives; ``vs_project`` owns its full syntax."""

SpanId = Annotated[str, StringConstraints(pattern=r"^(flow|op):\S*$")]
"""A run-trace span's identity, as the run protocol's ``SpanId``."""

MAX_DEADLINE_MS = 3_600_000


class HelperRequestBase(BaseModel):
    """The fields every helper request has, strict: an unknown field is rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    deadline_ms: Annotated[int, Field(gt=0, le=MAX_DEADLINE_MS)]
    """How long the client waits for the reply; the helper answers ``deadline_exceeded`` after it."""


class HelperReplyBase(BaseModel):
    """The field every helper reply has, strict: an unknown field is rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
