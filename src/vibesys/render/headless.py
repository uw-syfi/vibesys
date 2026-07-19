"""Terminal renderer for headless runs.

Subscribes to the process-global :class:`~vibesys.render.sink.OutputSink`
and renders the same information the TUI shows: streamed agent output,
tool calls and results, todo lists, and prompt/diagnostic lines. This is
the only component that writes presentation output to the terminal; the
backend itself emits events and plain log text only.

Selection happens once at composition time (``create_run_context``): the
renderer is subscribed only when no TUI supervisor is attached.
"""

from __future__ import annotations

import json
import re
import sys
from typing import TextIO

from vibesys.constants import DIM, GREEN, RESET, YELLOW
from vibesys.render.format import format_status_prefix
from vibesys.server.events import (
    AgentOutputChunkData,
    RunEvent,
    TodoItemData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

_COLOR_STATUS_INDICATORS = {
    "completed": f"{GREEN}✓{RESET}",
    "in_progress": f"{YELLOW}▶{RESET}",
    "pending": f"{DIM}○{RESET}",
}
_PLAIN_STATUS_INDICATORS = {
    "completed": "✓",
    "in_progress": "▶",
    "pending": "○",
}


class TodoDisplay:
    """Renders a persistent todo list box using ANSI cursor control."""

    def __init__(self, file: TextIO | None = None, *, color: bool = True):
        self._file = file
        self._color = color
        self._prev_lines = 0

    @property
    def _out(self) -> TextIO:
        return self._file if self._file is not None else sys.stdout

    def update(self, todos: list[TodoItemData]) -> None:
        if not todos:
            return
        indicators = _COLOR_STATUS_INDICATORS if self._color else _PLAIN_STATUS_INDICATORS
        items = [f"│ {indicators.get(t.status, '?')} {t.content}" for t in todos]
        width = max(len(self._strip_ansi(line)) for line in items) + 2
        width = max(width, len("─ Todo ─") + 4)

        top = f"┌─ Todo {'─' * (width - 8)}┐"
        bot = f"└{'─' * (width - 1)}┘"
        padded = [line + " " * (width - len(self._strip_ansi(line)) - 1) + "│" for line in items]
        box = [top] + padded + [bot]

        out = self._out
        # Clear previous block
        if self._prev_lines > 0:
            out.write(f"\033[{self._prev_lines}A")
            for _ in range(self._prev_lines):
                out.write("\033[2K\n")
            out.write(f"\033[{self._prev_lines}A")

        for line in box:
            out.write(line + "\n")
        out.flush()
        self._prev_lines = len(box)

    @staticmethod
    def _strip_ansi(s: str) -> str:
        return _ANSI_RE.sub("", s)


class HeadlessRenderer:
    """Render run events as colored terminal output for non-TUI runs."""

    # Maximum chars shown per tool result before truncation.
    DEFAULT_MAX_RESULT_LEN = 500
    # Maximum chars shown per prompt/diagnostic block before truncation;
    # the run log always holds the full text.
    DEFAULT_MAX_TEXT_LEN = 2000
    # Maximum chars shown per tool-call argument.
    _MAX_ARG_LEN = 80

    def __init__(
        self,
        out: TextIO | None = None,
        *,
        color: bool = True,
        max_result_len: int | None = DEFAULT_MAX_RESULT_LEN,
        max_text_len: int | None = DEFAULT_MAX_TEXT_LEN,
    ):
        self._explicit_out = out
        self._color = color
        self.max_result_len = max_result_len
        self.max_text_len = max_text_len
        self._at_line_start = True
        self._todo_display = TodoDisplay(file=out, color=color)

    @property
    def _out(self) -> TextIO:
        return self._explicit_out if self._explicit_out is not None else sys.stdout

    def handle(self, event: RunEvent) -> None:
        data = event.data
        if isinstance(data, AgentOutputChunkData):
            self._render_chunk(data)
        elif isinstance(data, ToolCallData):
            self._render_tool_call(data)
        elif isinstance(data, ToolResultData):
            self._render_tool_result(data)
        elif isinstance(data, TodoUpdateData):
            self._break_line()
            self._todo_display.update(data.todos)
        # Other event types (usage updates, lifecycle) carry no direct
        # terminal output; status readings surface through chunk prefixes.

    # -- channel rendering ---------------------------------------------------

    def _render_chunk(self, data: AgentOutputChunkData) -> None:
        if data.channel == "tool":
            # Tool traffic arrives as typed TOOL_CALL/TOOL_RESULT events;
            # tool-channel chunks only exist in event files recorded by
            # older backends and never reach a live renderer.
            return
        if data.channel == "assistant":
            self._render_assistant(data)
        elif data.channel == "analysis":
            self._render_line(format_status_prefix(data.status) + data.content)
        else:  # "prompt" / "diagnostic"
            self._render_block(data.content)

    def _render_assistant(self, data: AgentOutputChunkData) -> None:
        out = self._out
        if self._at_line_start:
            prefix = format_status_prefix(data.status)
            if prefix:
                out.write(prefix)
        out.write(data.content)
        out.flush()
        self._at_line_start = data.content.endswith("\n")

    def _render_line(self, text: str) -> None:
        self._break_line()
        out = self._out
        out.write(text)
        if not text.endswith("\n"):
            out.write("\n")
        out.flush()
        self._at_line_start = True

    def _render_block(self, content: str) -> None:
        body = content[:-1] if content.endswith("\n") else content
        if self.max_text_len is not None and len(body) > self.max_text_len:
            hidden = len(body) - self.max_text_len
            body = f"{body[: self.max_text_len]}\n... [{hidden} more chars, see log for full text]"
        self._render_line(body)

    def _render_tool_call(self, data: ToolCallData) -> None:
        parts: list[str] = []
        for k, v in data.args.items():
            s = v if isinstance(v, str) else json.dumps(v)
            if len(s) > self._MAX_ARG_LEN:
                s = s[: self._MAX_ARG_LEN] + "..."
            parts.append(f'{k}="{s}"' if isinstance(v, str) else f"{k}={s}")
        prefix = format_status_prefix(data.status)
        self._render_line(f"\n{prefix}→ {data.tool}({', '.join(parts)})")

    def _render_tool_result(self, data: ToolResultData) -> None:
        preview = data.content
        if self.max_result_len is not None and len(preview) > self.max_result_len:
            preview = preview[: self.max_result_len] + "..."
        self._render_line("\n".join(f"  {line}" for line in preview.split("\n")))

    def _break_line(self) -> None:
        if not self._at_line_start:
            self._out.write("\n")
            self._at_line_start = True
