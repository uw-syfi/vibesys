"""The refusals an agent's evaluation tool call can get, as a closed set with template text.

Every failure of a tool call is a ``ToolRefusal``; the template owns the wording and the
bridge passes facts only. A refused call changes no run state and never halts the run.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_prompts.api import RenderedPrompt

_RENDERER = TemplateRenderer(Path(__file__).with_name("prompts"))


class ToolRefusal(StrEnum):
    """Why a tool call was refused; the value is the template's branch."""

    RUN_STOPPING = "run_stopping"
    NOT_ADMITTED = "not_admitted"
    INVALID_PLAN = "invalid_plan"
    NOT_ALLOWED = "not_allowed"
    WORKSPACE_GONE = "workspace_gone"
    UNKNOWN_HANDLES = "unknown_handles"
    WAIT_OPEN = "wait_open"
    WAIT_NOT_ACTIVE = "wait_not_active"
    WAIT_NOT_RESUMABLE = "wait_not_resumable"
    WAIT_OVERLAPPED = "wait_overlapped"
    BUSY = "busy"
    RUN_FAILING = "run_failing"
    MALFORMED = "malformed"
    UNKNOWN_CALLER = "unknown_caller"
    REFUSED = "refused"


class ToolRefusedError(ValueError):
    """A tool call the run refused, carrying its rendered text for the agent."""

    def __init__(self, reason: ToolRefusal, handles: Sequence[str] = ()) -> None:
        """Render the agent-facing text for ``reason`` and the unknown ``handles``."""
        self.reason = reason
        super().__init__(render_tool_refusal(reason, handles))


def render_tool_refusal(reason: ToolRefusal, handles: Sequence[str] = ()) -> RenderedPrompt:
    """The text an agent reads for a refused call."""
    return _RENDERER.render_template(
        "agent_tool_error.j2", reason=reason.value, handles=list(handles)
    )
