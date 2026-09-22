"""Agent-facing event payload types shared by core and the server.

These are wire types for the ``core-events.jsonl`` / server event protocol:
the ``kind`` discriminator literals (``"command"``, ``"json"``) are frozen
format and must not change. ``EventPayload`` here is ``vs_agent``'s own copy
of the immutable payload base, kept separate from ``vibesys.events`` and
``server.events`` (each of which also has other, non-agent payloads that
still need their own base) so this leaf library never imports ``vibesys.*``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventPayload(BaseModel):
    """Immutable base for structured agent event payloads."""

    model_config = ConfigDict(frozen=True)


AgentOutputChannel = Literal["assistant", "analysis", "tool", "diagnostic", "prompt"]


class AgentStatusData(EventPayload):
    """Structured progress readings for one agent invocation.

    Carried on presentation events so renderers can format their own status
    prefix (e.g. ``[Round 3/24 | Implementer | 12.3s | 20k/1.0M]``) without
    the server baking any layout or styling into the payload.
    """

    progress: str | None = None
    agent_label: str | None = None
    elapsed_seconds: float = 0.0
    input_tokens: int = 0
    context_window: int | None = None


class CommandResultPayload(EventPayload):
    """Structured result of a command-style tool execution."""

    kind: Literal["command"] = "command"
    stdout: str
    stderr: str
    exit_code: int | None = None
    duration: float | None = None


class JsonResultPayload(EventPayload):
    """A tool result that is a JSON object or array, already parsed."""

    kind: Literal["json"] = "json"
    value: dict[str, Any] | list[Any]


ToolResultPayload = Annotated[
    CommandResultPayload | JsonResultPayload,
    Field(discriminator="kind"),
]


class TodoItemData(EventPayload):  # noqa: D101  # tracked: #288
    content: str
    status: str
