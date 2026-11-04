"""The semantic lifecycle of one provider invocation, shared by every executor that runs one.

A session executor (legacy or core) reports the start and the terminal outcome of each
provider invocation it dispatches as one of these values, and the product maps them to its own
event stream. Nothing here knows about a run, a journal or a renderer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class AgentExecutionStatus(StrEnum):
    """Provider-neutral outcome of one agent execution."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AgentExecutionStarted(BaseModel):
    """Semantic start of one provider invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    label: str
    execution_id: str
    system_prompt: str
    user_prompt: str
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    client: Literal["scripted", "provider"] | None = None


class AgentReply(BaseModel):
    """The typed reply a structured turn ended with, read through the role's own reply model.

    ``kind`` is that model's name and ``json_schema`` its JSON Schema, so a reader learns which
    fields are text, choices or lists from the model and keeps no list of its own. ``value`` is
    the validated reply as JSON.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    json_schema: dict[str, Any]
    value: dict[str, Any]


class AgentExecutionFinished(BaseModel):
    """Semantic terminal observation for one provider invocation.

    ``reply`` is set when the turn was structured and its text is a valid reply of the turn's
    model; ``result`` stays the raw text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    label: str
    execution_id: str
    status: AgentExecutionStatus
    result: Any = None
    error: str | None = None
    reply: AgentReply | None = None


type AgentExecutionLifecycleEvent = AgentExecutionStarted | AgentExecutionFinished


class AgentExecutionLifecycleSink(Protocol):
    """Record one semantic execution lifecycle observation synchronously."""

    def __call__(self, event: AgentExecutionLifecycleEvent) -> object: ...


__all__ = [
    "AgentExecutionFinished",
    "AgentExecutionLifecycleEvent",
    "AgentExecutionLifecycleSink",
    "AgentExecutionStarted",
    "AgentExecutionStatus",
    "AgentReply",
]
