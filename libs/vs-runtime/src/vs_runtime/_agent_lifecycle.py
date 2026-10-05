"""The semantic lifecycle of one provider invocation, shared by every executor that runs one.

A session executor (legacy or core) reports the start and the terminal outcome of each
provider invocation it dispatches as one of these values, and the product maps them to its own
event stream. Nothing here knows about a run, a journal or a renderer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class AgentExecutionStatus(StrEnum):
    """Driver-neutral outcome of one agent execution."""

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
    driver: str | None = None
    provider: str | None = None
    model: str | None = None


class AgentExecutionFinished(BaseModel):
    """Semantic terminal observation for one provider invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    label: str
    execution_id: str
    status: AgentExecutionStatus
    result: Any = None
    error: str | None = None


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
]
