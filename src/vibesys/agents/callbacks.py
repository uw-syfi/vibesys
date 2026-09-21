import json  # noqa: D100  # tracked: #288
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, TextIO

from vibesys.agents.progress import AgentProgress
from vibesys.agents.todos import todos_from_tool_call
from vibesys.events import AgentOutputChannel, AgentStatusData, ToolResultPayload
from vibesys.render.format import format_status_prefix
from vibesys.render.sink import output_sink

ContextWindowLookup = Callable[[str | None], int | None]
"""Resolves a model name to its context window size in tokens.

Injected into ``AgentLogger`` so the static prefix table can be stubbed in
tests today and replaced in the future (e.g. live ``client.models.list()``
queries against the OpenAI/Anthropic models API, or values loaded from
``agent.toml``).
"""


# Ordered most-specific first: linear scan returns the first prefix match,
# so longer prefixes must come before their shorter parents (e.g. ``gpt-5.4``
# before ``gpt-5``, ``claude-sonnet-4-6`` before ``claude-``).
#
# Values verified against vendor docs as of 2026-04-08.
_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    # OpenAI — https://developers.openai.com/api/docs/models/
    ("gpt-5.4", 1_050_000),  # gpt-5.4, gpt-5.4-pro
    ("gpt-5", 400_000),  # gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.2
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),
    ("o3", 200_000),
    ("o4", 200_000),
    # Anthropic — https://platform.claude.com/docs/en/docs/about-claude/models/overview
    # Opus 4.6 and Sonnet 4.6 default to 1M; older 4.x and Haiku 4.5 are 200k.
    # (Pre-4.6 models had a context-1m-2025-08-07 beta header that swapped them
    # to 1M per request, but that's enabled at request time, not by model ID.)
    ("claude-opus-4-6", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-", 200_000),
    # Google
    ("gemini-", 1_048_576),  # Gemini 2.5 / 3.x families default to 1M
    ("gemma-", 8_192),
)


def _default_context_window_lookup(model_name: str | None) -> int | None:
    """Default ``ContextWindowLookup`` — prefix match against the static table."""
    if not model_name:
        return None
    for prefix, ctx in _MODEL_CONTEXT_WINDOWS:
        if model_name.startswith(prefix):
            return ctx
    return None


class AgentLogger:
    """Single event adapter for all agent activity: token streaming, tool calls, and tool results.

    Every observation is published as typed events through the process-global
    :func:`~vibesys.render.sink.output_sink` (rendered by whichever surface is
    composed, such as a headless renderer or application subscriber) and, when ``log_file``
    is provided, written untruncated as plain text to the durable run log.
    ``AgentLogger`` itself never writes to the terminal.
    """

    def __init__(  # noqa: ANN204, D107, PLR0913  # tracked: #288
        self,
        log_file: TextIO | None = None,
        model_name: str | None = None,
        agent_label: str | None = None,
        progress: AgentProgress | None = None,
        context_window_lookup: ContextWindowLookup | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ):
        self._external_text_streaming = False
        # Sticky for the logger's lifetime, which is one turn: whether any
        # assistant text reached the assistant channel from a driver's stream.
        self._streamed_external_text = False
        self._external_text_ends_with_newline = False
        self._log_file = log_file
        self._model_name = model_name
        self._agent_label = agent_label
        self._progress = progress
        self._start_time = time.monotonic()
        self._input_tokens = 0
        # Most recent usage dict from the cli backend (see ``update_usage``).
        self._latest_usage: dict[str, Any] | None = None
        self._context_window_lookup = context_window_lookup or _default_context_window_lookup
        self._context_window = self._context_window_lookup(model_name)
        self._pending_tool_calls: dict[str, deque[str]] = defaultdict(deque)
        self._agent_kind = agent_kind
        self._round_label = round_label
        self._invocation_id = invocation_id

    def _status(self) -> AgentStatusData:
        """Snapshot the ``[progress | label | elapsed | tokens/max]`` readings.

        Computed lazily on each use so the elapsed-time and token-count
        readings reflect the latest state when the event is emitted.
        """
        return AgentStatusData(
            progress=self._progress.label() if self._progress is not None else None,
            agent_label=self._agent_label,
            elapsed_seconds=time.monotonic() - self._start_time,
            input_tokens=self._input_tokens,
            context_window=self._context_window,
        )

    def _format_prefix(self) -> str:
        """Render the status snapshot as the plain-text log prefix."""
        return format_status_prefix(self._status())

    # --- Event emission + log formatting ---

    # Tool names whose args contain code content that is already tracked in
    # git — no need to duplicate it in the run log.
    _CODE_CHANGE_TOOLS = frozenset(
        {
            "Write",
            "Edit",  # Claude Code tools
            "write_file",
            "edit_file",  # Gemini and other CLI providers
        }
    )
    # Args that carry bulk code content and should be omitted from the log.
    _CODE_CONTENT_ARGS = frozenset(
        {
            "content",
            "old_string",
            "new_string",  # Write/Edit
            "old_text",
            "new_text",  # edit variants
        }
    )

    def _emit_tool_call(
        self, name: str, args: dict[str, Any], *, call_id: str | None = None
    ) -> None:
        resolved_call_id = call_id or uuid.uuid4().hex
        self._pending_tool_calls[name].append(resolved_call_id)
        output_sink().tool_call(
            name,
            args,
            call_id=resolved_call_id,
            status=self._status(),
            agent_kind=self._agent_kind,
            round_label=self._round_label,
            invocation_id=self._invocation_id,
        )
        todos = todos_from_tool_call(name, args)
        if todos is not None:
            output_sink().todo_update(
                todos,
                agent_kind=self._agent_kind,
                round_label=self._round_label,
                invocation_id=self._invocation_id,
            )
        is_code_tool = name in self._CODE_CHANGE_TOOLS

        # Log file: skip code content args for file-writing tools
        if self._log_file:
            full_parts = []
            for k, v in args.items():
                if is_code_tool and k in self._CODE_CONTENT_ARGS:
                    full_parts.append(f'{k}="<{len(str(v))} chars, see git>"')
                    continue
                s = json.dumps(v) if not isinstance(v, str) else v
                full_parts.append(f'{k}="{s}"' if isinstance(v, str) else f"{k}={s}")
            self._log_line(f"\n→ {name}({', '.join(full_parts)})")

    def _emit_thinking(self, text: str):  # noqa: ANN202  # tracked: #288
        self._publish(text, "analysis")
        self._log_line("\n[thinking]")
        for line in text.split("\n"):
            self._log_line(line)

    # Maximum chars to write per tool result in the log file.  Keeps logs
    # readable while still capturing enough output for debugging.
    _LOG_MAX_RESULT_LEN = 2000

    def _emit_tool_result(
        self,
        name: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
    ) -> None:
        full_text = str(content)
        pending = self._pending_tool_calls[name]
        resolved_call_id = call_id
        if resolved_call_id is None and pending:
            resolved_call_id = pending.popleft()
        elif resolved_call_id is not None:
            try:  # noqa: SIM105  # tracked: #288
                pending.remove(resolved_call_id)
            except ValueError:
                pass
        output_sink().tool_result(
            name,
            full_text,
            call_id=resolved_call_id,
            is_error=is_error,
            payload=payload,
            agent_kind=self._agent_kind,
            round_label=self._round_label,
            invocation_id=self._invocation_id,
        )

        # Truncated to log file
        if self._log_file:
            log_preview = full_text
            if len(log_preview) > self._LOG_MAX_RESULT_LEN:
                log_preview = (
                    log_preview[: self._LOG_MAX_RESULT_LEN]
                    + f"... [{len(full_text) - self._LOG_MAX_RESULT_LEN} more chars]"
                )
            for line in log_preview.split("\n"):
                self._log_line(f"  {line}")

    def _log_line(self, text: str = "") -> None:
        """Write one line to the log file only."""
        self._log_write(text + "\n")

    def _log_write(self, text: str) -> None:
        """Write raw text to the log file.

        Flushes the log file after each write so operators tailing
        ``run-*.log`` see agent events as they arrive (e.g. codex reasoning
        and command executions streamed during a long turn). Python opens
        regular files with block buffering, so without the explicit flush
        streamed events can sit in the buffer for minutes.
        """
        if self._log_file and text:
            self._log_file.write(text)
            self._log_file.flush()

    # --- Public hooks for external-agent drivers ---
    #
    # Drivers publish normalized ``AgentEvent``s, which ``_LoggerObserver``
    # calls through to on this object.

    def _publish_channel(self, text: str, channel: AgentOutputChannel) -> None:
        """Publish ``text`` verbatim on ``channel`` and mirror it to the log."""
        if not text:
            return
        status = self._status()
        self._publish(text, channel, status=status)
        self._log_line(f"{format_status_prefix(status)}{text}")

    def on_thinking(self, text: str) -> None:
        """Publish agent reasoning on the analysis channel.

        Every driver marks its own plumbing with
        ``payload={"channel": "diagnostic"}``, which the observer routes to
        :meth:`on_diagnostic`, so nothing that reaches here is inspected.
        """
        self._publish_channel(text, "analysis")

    def on_diagnostic(self, text: str) -> None:
        """Publish driver plumbing on the diagnostic channel.

        The caller has already classified ``text`` as plumbing rather than
        agent reasoning, so nothing here inspects it. The text is published
        verbatim; only the channel differs from :meth:`on_thinking`.
        """
        self._publish_channel(text, "diagnostic")

    def on_tool_call(self, tool: str, args: dict[str, Any] | str | None = None) -> None:  # noqa: D102  # tracked: #288
        if isinstance(args, dict):
            normalized = args
        elif args is None:
            normalized = {}
        else:
            normalized = {"args": str(args)}
        self.log_tool_call(tool, normalized)

    def on_tool_result(  # noqa: D102, PLR0913  # tracked: #288
        self,
        tool: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        duration: float | None = None,  # noqa: ARG002 - protocol parity
        *,
        payload: ToolResultPayload | None = None,
    ) -> None:
        is_error = bool(stderr) or (exit_code not in (None, 0))
        content = stdout or stderr
        self.log_tool_result(tool, content, is_error=is_error, payload=payload)

    def on_usage(self, usage: dict[str, Any]) -> None:  # noqa: D102  # tracked: #288
        self.update_usage(usage)

    def update_usage(self, usage: dict[str, Any] | None) -> None:
        """Refresh token tracking from a CLI provider's per-turn usage dict.

        We overwrite, not accumulate, because the prefix reflects *current
        context window pressure*, not cumulative spend.  ``input_tokens``
        includes cached tokens on every provider: the CLI drivers fold the
        cache counts in before reporting.

        A zero / missing ``input_tokens`` field is treated as "no update"
        so a stale usage block can't clobber the last real reading.
        """
        if not usage:
            return
        self._latest_usage = usage
        input_tokens = usage.get("input_tokens") or 0
        if input_tokens:
            self._input_tokens = input_tokens
            self._publish_usage()

    def streamed_external_text_this_turn(self) -> bool:
        """Whether an external driver already streamed this turn's answer.

        One logger serves exactly one invocation (``AgentClient._invoke_turn``
        builds it per turn), so this reads as "during this turn". A caller that
        also holds the turn's final text asks this before printing it, so an
        answer that already reached the assistant channel as it arrived is not
        rendered a second time at the end.
        """
        return self._streamed_external_text

    def log_text(self, text: str) -> None:
        """Emit one exact assistant-text delta from an external driver."""
        if not text:
            return
        status = self._status()
        self._publish(text, "assistant", status=status)
        if not self._external_text_streaming:
            self._log_write(format_status_prefix(status))
        self._log_write(text)
        self._external_text_streaming = True
        self._streamed_external_text = True
        self._external_text_ends_with_newline = text.endswith("\n")

    def end_text(self) -> None:
        """Close the current external-driver assistant-text segment."""
        if not self._external_text_streaming:
            return
        if not self._external_text_ends_with_newline:
            self._publish("\n", "assistant")
            self._log_write("\n")
        self._external_text_streaming = False
        self._external_text_ends_with_newline = False

    def log_tool_call(self, name: str, args: dict[str, Any]) -> None:
        """Emit a tool invocation."""
        self._emit_tool_call(name, args)

    def log_tool_result(
        self,
        name: str,
        content: str,
        *,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
    ) -> None:
        """Emit a tool result."""
        self._emit_tool_result(name, content, is_error=is_error, payload=payload)

    def _publish(
        self,
        content: str,
        channel: AgentOutputChannel,
        status: AgentStatusData | None = None,
    ) -> None:
        output_sink().agent_output(
            content,
            channel=channel,
            status=status if status is not None else self._status(),
            agent_kind=self._agent_kind,
            round_label=self._round_label,
            invocation_id=self._invocation_id,
        )

    def _publish_usage(self) -> None:
        output_sink().usage_update(
            self._input_tokens,
            context_window=self._context_window,
            model=self._model_name,
            agent_kind=self._agent_kind,
            round_label=self._round_label,
            invocation_id=self._invocation_id,
        )
