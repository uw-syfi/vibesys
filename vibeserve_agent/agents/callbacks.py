import json
import sys
import time
import traceback
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler

from vibeserve_agent.constants import _DIM, _BOLD, _CYAN, _GREEN, _RED, _YELLOW, _RESET


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
    ("gpt-5.4",            1_050_000),  # gpt-5.4, gpt-5.4-pro
    ("gpt-5",                400_000),  # gpt-5, gpt-5-mini, gpt-5-nano, gpt-5.2
    ("gpt-4o",               128_000),
    ("gpt-4-turbo",          128_000),
    ("o1",                   200_000),
    ("o3",                   200_000),
    ("o4",                   200_000),
    # Anthropic — https://platform.claude.com/docs/en/docs/about-claude/models/overview
    # Opus 4.6 and Sonnet 4.6 default to 1M; older 4.x and Haiku 4.5 are 200k.
    # (Pre-4.6 models had a context-1m-2025-08-07 beta header that swapped them
    # to 1M per request, but that's enabled at request time, not by model ID.)
    ("claude-opus-4-6",    1_000_000),
    ("claude-sonnet-4-6",  1_000_000),
    ("claude-",              200_000),
    # Google
    ("gemini-",            1_048_576),  # Gemini 2.5 / 3.x families default to 1M
    ("gemma-",                 8_192),
)


def _default_context_window_lookup(model_name: str | None) -> int | None:
    """Default ``ContextWindowLookup`` — prefix match against the static table."""
    if not model_name:
        return None
    for prefix, ctx in _MODEL_CONTEXT_WINDOWS:
        if model_name.startswith(prefix):
            return ctx
    return None


def _format_token_count(n: int) -> str:
    """Format a token count compactly: ``999`` / ``20k`` / ``1.0M``."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


_STATUS_INDICATORS = {
    "completed": f"{_GREEN}✓{_RESET}",
    "in_progress": f"{_YELLOW}▶{_RESET}",
    "pending": f"{_DIM}○{_RESET}",
}


class TodoDisplay:
    """Renders a persistent todo list box using ANSI cursor control."""

    def __init__(self, file=None):
        self._file = file or sys.stdout
        self._prev_lines = 0

    def update(self, todos: list[dict]) -> None:
        if not todos:
            return
        # Build box lines
        items = []
        for t in todos:
            indicator = _STATUS_INDICATORS.get(t["status"], "?")
            items.append(f"│ {indicator} {t['content']}")
        width = max(len(self._strip_ansi(line)) for line in items) + 2
        width = max(width, len("─ Todo ─") + 4)

        top = f"┌─ Todo {'─' * (width - 8)}┐"
        bot = f"└{'─' * (width - 1)}┘"
        padded = [line + " " * (width - len(self._strip_ansi(line)) - 1) + "│" for line in items]
        box = [top] + padded + [bot]

        out = self._file
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
        import re
        return re.sub(r"\033\[[0-9;]*m", "", s)


class AgentLogger(BaseCallbackHandler):
    """Single callback handler for all agent logging: token streaming, tool calls, and tool results.

    When ``log_file`` is provided, full untruncated output is written there while
    stdout receives the truncated version.
    """

    def __init__(
        self,
        max_result_len: int | None = 500,
        log_file=None,
        model_name: str | None = None,
        agent_label: str | None = None,
        context_window_lookup: ContextWindowLookup | None = None,
    ):
        self._streaming = False
        self._max_result_len = max_result_len
        self._output = sys.stdout
        self._log_file = log_file
        self._call_count = 0
        self._model_name = model_name
        self._agent_label = agent_label
        self._start_time = time.monotonic()
        self._input_tokens = 0
        # Most recent usage dict from the cli backend (see ``update_usage``).
        # The deepagents path doesn't populate this — it drives ``_input_tokens``
        # directly via ``on_llm_end``.
        self._latest_usage: dict | None = None
        self._context_window_lookup = context_window_lookup or _default_context_window_lookup
        self._context_window = self._context_window_lookup(model_name)

    def _format_prefix(self) -> str:
        """Build the dynamic ``[label | elapsed | tokens/max]`` prefix.

        Computed lazily on each use so the elapsed-time and token-count
        readings reflect the latest state when the prefix is printed.
        """
        if not self._agent_label:
            return ""
        elapsed = time.monotonic() - self._start_time
        used = _format_token_count(self._input_tokens)
        if self._context_window:
            tokens_str = f"{used}/{_format_token_count(self._context_window)}"
        else:
            tokens_str = used
        return f"{_BOLD}[{self._agent_label} | {elapsed:.1f}s | {tokens_str}]{_RESET} "

    # --- LLM call context (log-file only) ---

    def on_chat_model_start(self, serialized, messages, **kwargs):
        if not self._log_file:
            return
        self._call_count += 1
        flat = messages[0] if messages else []

        # Separator with call number
        self._print_log(f"\n{'─'*60}")
        self._print_log(f"  LLM call #{self._call_count}")
        self._print_log(f"{'─'*60}")

        # First call: log model info and system prompt
        if self._call_count == 1:
            model_name = self._model_name or (serialized.get("kwargs") or {}).get("model") or ""
            if not model_name:
                id_parts = serialized.get("id", [])
                model_name = "/".join(id_parts) if id_parts else "unknown"
            self._print_log(f"  Model: {model_name}")

            for msg in flat:
                if getattr(msg, "type", None) == "system":
                    self._print_log(f"\n  System prompt:\n{msg.content}")
                    break

        # Message type summary
        from collections import Counter
        type_counts = Counter(getattr(m, "type", "unknown") for m in flat)
        summary = ", ".join(f"{count} {typ}" for typ, count in sorted(type_counts.items()))
        self._print_log(f"  Messages: {len(flat)} ({summary})")

        # Last human or tool message (trigger for this call)
        for msg in reversed(flat):
            if getattr(msg, "type", None) in ("human", "tool"):
                content = str(msg.content)
                if len(content) > 500:
                    content = content[:500] + "..."
                self._print_log(f"  Last {msg.type} message: {content}")
                break

    # --- LLM token streaming ---

    def on_llm_new_token(self, token, **kwargs):
        if token:
            if not self._streaming and self._agent_label:
                self._print(f"{self._format_prefix()}", end="", flush=True)
            self._print(f"{_GREEN}{token}{_RESET}", end="", flush=True)
            self._streaming = True

    def on_llm_end(self, response, **kwargs):
        was_streaming = self._streaming
        if self._streaming:
            self._print()
            self._streaming = False

        msg = response.generations[0][0].message

        # Refresh context-window-usage tracking from the standardized
        # langchain UsageMetadata field. All chat-model wrappers in
        # ``models.py`` (ChatAnthropic, ChatOpenAI, ChatGoogleGenerativeAI,
        # ChatAnthropicVertex) populate this — Vertex Anthropic uses the
        # exact same ``_create_usage_metadata`` from langchain-anthropic,
        # so there's no provider-specific shape to handle. The langchain
        # adapters also fold provider quirks into the field (e.g. Anthropic
        # adds cache_read/cache_creation tokens back into ``input_tokens``
        # so it reflects true context size, not just the non-cached portion).
        #
        # ``or {}`` handles openai-compatible servers (vLLM, Ollama via
        # ChatOpenAI with custom base_url) that may omit the usage block —
        # in that case the count stays at 0 and the prefix shows ``0/<max>``
        # rather than crashing.
        usage = getattr(msg, "usage_metadata", None) or {}
        input_tokens = usage.get("input_tokens") or 0
        if input_tokens:
            self._input_tokens = input_tokens

        content = msg.content

        # Display thinking blocks if present
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        self._print_thinking(thinking_text)

        if was_streaming:
            return

        # Fallback: print full text for models that don't stream tokens
        text = msg.text
        if text:
            self._print(f"{_GREEN}{text}{_RESET}")
        for tc in msg.tool_calls:
            self._print_tool_call(tc["name"], tc["args"])

    # --- Tool execution ---

    def on_tool_start(self, serialized, input_str, *, inputs=None, **kwargs):
        pass  # tool call already logged in on_llm_end

    def on_tool_end(self, output, **kwargs):
        content = output.content if hasattr(output, "content") else str(output)
        name = getattr(output, "name", None) or "unknown"
        status = getattr(output, "status", None)
        is_error = status == "error" or "[Command failed" in str(content)
        self._print_tool_result(name, content, color=_RED if is_error else _DIM)

    def on_tool_error(self, error, **kwargs):
        self._print(f"  {_RED}✗ Tool error: {error!r}{_RESET}")
        if isinstance(error, BaseException):
            tb = traceback.format_exception(type(error), error, error.__traceback__)
            if tb:
                self._print(f"  {_RED}{''.join(tb).strip()}{_RESET}")

    # --- Formatting ---

    # Tool names whose args contain code content that is already tracked in
    # git — no need to duplicate it in the run log.
    _CODE_CHANGE_TOOLS = frozenset({
        "Write", "Edit",              # Claude Code tools
        "write_file", "edit_file",    # deepagents tools
    })
    # Args that carry bulk code content and should be omitted from the log.
    _CODE_CONTENT_ARGS = frozenset({
        "content", "old_string", "new_string",  # Write/Edit
        "old_text", "new_text",                  # deepagents edit variants
    })

    def _print_tool_call(self, name: str, args: dict):
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
            self._print_log(f"\n{_CYAN}{_BOLD}→ {name}({', '.join(full_parts)}){_RESET}")

        # Truncated args to stdout
        parts = []
        for k, v in args.items():
            s = json.dumps(v) if not isinstance(v, str) else v
            if len(s) > 80:
                s = s[:80] + "..."
            parts.append(f'{k}="{s}"' if isinstance(v, str) else f"{k}={s}")
        self._print_stdout(f"\n{self._format_prefix()}{_CYAN}{_BOLD}→ {name}({', '.join(parts)}){_RESET}")

    def _print_thinking(self, text: str):
        self._print(f"\n{_DIM}[thinking]{_RESET}")
        for line in text.split("\n"):
            self._print(f"{_DIM}{line}{_RESET}")

    # Maximum chars to write per tool result in the log file.  Keeps logs
    # readable while still capturing enough output for debugging.
    _LOG_MAX_RESULT_LEN = 2000

    def _print_tool_result(self, name: str, content: str, color: str = _DIM):
        full_text = str(content)

        # Truncated to log file
        if self._log_file:
            log_preview = full_text
            if len(log_preview) > self._LOG_MAX_RESULT_LEN:
                log_preview = log_preview[:self._LOG_MAX_RESULT_LEN] + f"... [{len(full_text) - self._LOG_MAX_RESULT_LEN} more chars]"
            for line in log_preview.split("\n"):
                self._print_log(f"  {color}{line}{_RESET}")

        # Truncated to stdout
        preview = full_text
        if self._max_result_len is not None and len(preview) > self._max_result_len:
            preview = preview[:self._max_result_len] + "..."
        for line in preview.split("\n"):
            self._print_stdout(f"  {color}{line}{_RESET}")

    def _print(self, *args, **kwargs):
        """Write to both stdout and log file.

        Flushes the log file after each write so operators tailing
        ``run-*.log`` see agent events as they arrive (e.g. codex reasoning
        and command executions streamed during a long turn). Python opens
        regular files with block buffering, so without the explicit flush
        streamed events can sit in the buffer for minutes.
        """
        print(*args, file=self._output, **kwargs)
        if self._log_file:
            print(*args, file=self._log_file, **kwargs)
            self._log_file.flush()

    def _print_stdout(self, *args, **kwargs):
        """Write to stdout only."""
        print(*args, file=self._output, **kwargs)

    def _print_log(self, *args, **kwargs):
        """Write to log file only."""
        if self._log_file:
            print(*args, file=self._log_file, **kwargs)
            self._log_file.flush()

    # --- Public hooks for non-langchain event sources ---
    #
    # The deepagents path drives ``AgentLogger`` via the langchain
    # ``BaseCallbackHandler`` hooks (``on_llm_new_token``, ``on_tool_end``, …).
    # The cli runner in ``vibeserve_agent.agents.cli_runner`` receives events
    # from ``libs.agent_cli.AgentEventHandler`` directly on this object. Both
    # paths converge on the same private ``_print_*`` formatters, so on-screen
    # output is identical regardless of which backend is in use.

    def on_thinking(self, text: str) -> None:
        self.log_text(text)

    def on_tool_call(self, tool: str, args: dict[str, Any] | str | None = None) -> None:
        if isinstance(args, dict):
            normalized = args
        elif args is None:
            normalized = {}
        else:
            normalized = {"args": str(args)}
        self.log_tool_call(tool, normalized)

    def on_tool_result(
        self,
        tool: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        duration: float | None = None,  # noqa: ARG002 - protocol parity
    ) -> None:
        is_error = bool(stderr) or (exit_code not in (None, 0))
        content = stdout or stderr
        self.log_tool_result(tool, content, is_error=is_error)

    def on_usage(self, usage: dict[str, Any]) -> None:
        self.update_usage(usage)

    def update_usage(self, usage: dict | None) -> None:
        """Refresh token tracking from a CLI provider's per-turn usage dict.

        Mirrors the deepagents path (:meth:`on_llm_end`): we overwrite, not
        accumulate, because the prefix reflects *current context window
        pressure*, not cumulative spend.  Claude Code's stream-json already
        folds ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
        back into ``input_tokens``, matching what ``langchain-anthropic``
        does for the deepagents path.

        A zero / missing ``input_tokens`` field is treated as "no update"
        so a stale usage block can't clobber the last real reading.
        """
        if not usage:
            return
        self._latest_usage = usage
        input_tokens = usage.get("input_tokens") or 0
        if input_tokens:
            self._input_tokens = input_tokens

    def log_text(self, text: str) -> None:
        """Print *text* with the agent's prefix and the standard green styling.

        Mirrors how ``on_llm_new_token`` renders streamed tokens, but for
        complete chunks of text emitted by the cli backend.
        """
        if not text:
            return
        self._print(f"{self._format_prefix()}{_GREEN}{text}{_RESET}")

    def log_tool_call(self, name: str, args: dict) -> None:
        """Format a tool invocation the same way the deepagents path does."""
        self._print_tool_call(name, args)

    def log_tool_result(
        self,
        name: str,
        content: str,
        *,
        is_error: bool = False,
    ) -> None:
        """Format a tool result the same way ``on_tool_end`` does."""
        self._print_tool_result(name, content, color=_RED if is_error else _DIM)
