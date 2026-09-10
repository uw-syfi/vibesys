import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from agentshim.claude import ClaudeGenerationSession
from agentshim.events import AgentEventHandler

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentshim.executor import CommandExecutor


class _TextStreamHandler(AgentEventHandler, Protocol):
    """Event handler that separates assistant text from reasoning.

    ``on_text`` is a VibeSys extension: agentshim's protocol routes every
    assistant content block through ``on_thinking``, which downstream publishes
    on the ``analysis`` channel.
    """

    def on_text(self, text: str) -> None: ...


def _decoded_event(line: str) -> object:
    """Decode one ``stream-json`` line, or return ``None`` if it is not JSON.

    The value comes straight off a subprocess, so it may be any JSON type and
    callers must narrow before indexing.
    """
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


class _PartialAssistantText:
    """Deliver Claude's text deltas and drop the finished block they duplicate.

    Under ``--include-partial-messages`` every assistant text block is reported
    twice: as ``content_block_delta`` tokens while the block is open, and again
    as the whole-message ``assistant`` echo that agentshim turns into
    ``on_thinking``. The echo arrives before the block's ``content_block_stop``,
    so the accumulated deltas still describe exactly the block it repeats and an
    equality test identifies it. Any text that does not match is forwarded:
    duplicated output is a visible annoyance, dropped output is data loss.

    ``assistant`` lines also carry ``message.usage``, so suppression happens
    here, per text block, rather than by discarding the line upstream.
    """

    def __init__(self, handler: _TextStreamHandler) -> None:
        self._handler = handler
        # Text already delivered for the content block currently open. Used
        # only to recognize that block's echo.
        self._streamed = ""

    @property
    def handlers(self) -> tuple[_TextStreamHandler, ...]:
        """Expose the wrapped handler to agentshim's context binding.

        ``bind_event_handler_context`` recurses through ``handlers``, which is
        the whole of this wrapper's transparency for ``bind_context``. Defining
        ``bind_context`` here as well would bind the inner handler twice.
        """
        return (self._handler,)

    def observe(self, event: object) -> None:
        """Read one ``stream_event`` payload, forwarding any assistant text token."""
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "content_block_start":
            self._streamed = ""
            return
        if kind != "content_block_delta":
            return
        delta = event.get("delta")
        # ``thinking_delta`` and ``signature_delta`` describe reasoning, which
        # stays on its existing channel and is never folded into text.
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return
        self._streamed += text
        self._handler.on_text(text)

    def on_thinking(self, text: str) -> None:
        if text and text == self._streamed:
            self._streamed = ""
            return
        self._handler.on_thinking(text)

    def on_tool_call(self, tool: str, args: dict[str, Any] | str | None = None) -> None:
        self._handler.on_tool_call(tool, args)

    def on_tool_result(
        self,
        tool: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        duration: float | None = None,
    ) -> None:
        self._handler.on_tool_result(
            tool=tool,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration=duration,
        )

    def on_usage(self, usage: dict[str, Any]) -> None:
        self._handler.on_usage(usage)

    # agentshim looks the process-lifecycle hooks up on the handler with
    # ``getattr``, so the wrapper must offer them and stay a no-op for inner
    # handlers that do not implement them.

    def on_run_start(self, command: "Sequence[str]") -> None:
        hook = getattr(self._handler, "on_run_start", None)
        if hook is not None:
            hook(command)

    def on_run_end(self, exit_code: int | None = None) -> None:
        hook = getattr(self._handler, "on_run_end", None)
        if hook is not None:
            hook(exit_code)

    def on_stderr(self, text: str) -> None:
        hook = getattr(self._handler, "on_stderr", None)
        if hook is not None:
            hook(text)


class StructuredOutputClaudeSession(ClaudeGenerationSession):
    """:class:`ClaudeGenerationSession` that also surfaces ``structured_output``.

    When ``--json-schema`` is passed, Claude Code returns the schema-conformant
    payload in a dedicated ``structured_output`` field of the terminal
    ``result`` event, separate from the freeform ``result`` text. Upstream
    agentshim only reads ``result``, so this subclass captures
    ``structured_output`` from the raw event line and, when present, returns it
    (serialized) as the session result. Non-schema turns are unaffected: the
    field is absent, so :meth:`run` falls through to agentshim's ``result``.
    """

    def __init__(self, **kwargs: Any):  # noqa: ANN204, ANN401  # tracked: #288
        handler = kwargs.get("event_handler")
        # Only ``None`` lets agentshim substitute its own Null/Console handler,
        # so a missing handler must reach ``super()`` unwrapped. A handler
        # without ``on_text`` keeps the pre-streaming path exactly: no deltas
        # consumed, no echo suppressed.
        partial = (
            _PartialAssistantText(cast("_TextStreamHandler", handler))
            if handler is not None and hasattr(handler, "on_text")
            else None
        )
        if partial is not None:
            kwargs["event_handler"] = partial
        super().__init__(**kwargs)
        self._partial = partial
        # ``None`` means "no schema-enforced payload seen"; a captured value is
        # any JSON type the schema root produced (always an object for VibeSys
        # response models).
        self.structured_output: Any = None

    def _process_stdout(self, line: str) -> None:
        data = _decoded_event(line)
        if (
            self._partial is not None
            and isinstance(data, dict)
            and data.get("type") == "stream_event"
        ):
            # agentshim's event parser has no ``stream_event`` branch and drops
            # the line, so the token deltas are routed to the wrapper instead.
            self._partial.observe(data.get("event"))
            return
        self._capture_structured_output(data)
        super()._process_stdout(line)

    def _capture_structured_output(self, data: object) -> None:
        if (
            isinstance(data, dict)
            and data.get("type") == "result"
            and data.get("structured_output") is not None
        ):
            self.structured_output = data["structured_output"]

    def run(self, prompt: str) -> str:
        result = super().run(prompt)
        if self.structured_output is not None:
            return json.dumps(self.structured_output)
        return result


class ClaudeCodeCodingAgent(CLICodingAgent[ClaudeGenerationSession]):
    """Coding agent implementation using the Claude Code CLI tool."""

    supports_native_output_schema = True
    # ``claude --resume <session-id>`` continues a stored session, so a
    # checkpointed session ID can be adopted before the next turn.
    supports_session_resume = True
    # Claude Code's ``--json-schema`` flag takes the schema *inline*, not a
    # path, so the schema file must be read at command-build time. The runner
    # therefore hands this provider an absolute host path (readable on both the
    # host and container execution paths, where the workspace is bind-mounted)
    # rather than the workspace-relative path Codex resolves against its cwd.
    native_output_schema_wants_absolute_path = True
    # Unlike Codex's strict subset, ``--json-schema`` accepts open-ended object
    # maps, so ``dict[str, T]`` fields (e.g. ``ImplementerResponse.metrics``)
    # stay on the native path instead of degrading to the prompt hint.
    native_output_schema_allows_arbitrary_keys = True

    def __init__(  # noqa: ANN204  # tracked: #288
        self,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor: "CommandExecutor | None" = None,
    ):
        """Initialize the Claude Code coding agent.

        Args:
            model: Optional model name to use with Claude Code. If None, uses default.
            event_handler: Optional event handler for UI updates.
            executor: Optional agentshim :class:`CommandExecutor`.
        """
        super().__init__(
            "claude",
            model,
            event_handler,
            executor=executor,
        )
        # Compact inline JSON Schema for ``--json-schema``, or ``None`` to keep
        # the portable prompt-hint contract. Populated by
        # :meth:`set_output_schema_path`.
        self.output_schema_json: str | None = None

    @property
    def claude_path(self) -> str:
        """Return path to claude binary (for backward compatibility)."""
        return self.binary_path

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return "[Claude]"

    def set_output_schema_path(self, path: str | None) -> None:
        """Apply a native structured-output JSON Schema to the next turn.

        *path* is an absolute path to a JSON Schema file (see
        :attr:`native_output_schema_wants_absolute_path`). Claude Code's
        ``--json-schema`` flag requires the schema *inline*, so the file is read
        and normalized to a compact JSON string here — early enough to fail
        before a turn is spawned. ``None`` clears it, restoring the portable
        prompt-hint contract for the next turn.

        Raises:
            RuntimeError: If the schema file cannot be read or does not contain
                valid JSON. The message names the offending path but never the
                subprocess environment.
        """
        if path is None:
            self.output_schema_json = None
            return
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"claude native output schema unreadable at {path}: {type(exc).__name__}"
            ) from exc
        try:
            schema = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(  # noqa: TRY003  # tracked: #288
                f"claude native output schema at {path} is not valid JSON: {exc}"
            ) from exc
        # Compact form keeps the process argv small; claude re-validates it as a
        # JSON Schema and rejects malformed input on its own.
        self.output_schema_json = json.dumps(schema, separators=(",", ":"))

    def _append_output_schema(self, cmd: list[str]) -> None:
        if self.output_schema_json is not None:
            cmd.extend(["--json-schema", self.output_schema_json])

    def set_reasoning_effort(self, effort: str) -> None:
        """Apply a per-agent effort level to fresh and resumed turns.

        Claude Code takes this as a session flag rather than a config override,
        which is why it is stored and re-emitted per command instead of being
        appended to a persistent argument list the way Codex does it.
        """
        self.reasoning_effort = effort

    def _append_reasoning_effort(self, cmd: list[str]) -> None:
        effort = getattr(self, "reasoning_effort", None)
        if effort:
            cmd.extend(["--effort", effort])

    def _get_command(self, prompt: str) -> list[str]:  # noqa: ARG002  # tracked: #288
        cmd = [
            self.binary_path,
            "-p",  # Print mode, reads prompt from stdin
            "--dangerously-skip-permissions",  # Auto-approval mode
            "--output-format",
            "stream-json",
            "--verbose",
            # Claude Code emits assistant tokens only while their content block
            # is still open, and only in this envelope; without the flag
            # stream-json carries finished messages alone.
            "--include-partial-messages",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        self._append_reasoning_effort(cmd)
        self._append_output_schema(cmd)
        return cmd

    def _get_resume_command(self, prompt: str, session_id: str) -> list[str]:  # noqa: ARG002  # tracked: #288
        cmd = [
            self.binary_path,
            "--resume",
            session_id,
            "-p",  # Print mode, reads prompt from stdin
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        self._append_reasoning_effort(cmd)
        self._append_output_schema(cmd)
        return cmd

    def _extract_session_id(self, session: ClaudeGenerationSession) -> str | None:
        return session.session_id

    def _create_session(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,  # noqa: FBT001, FBT002  # tracked: #288
    ) -> ClaudeGenerationSession:
        return StructuredOutputClaudeSession(
            binary_name=self.binary_name,
            env=self.env,
            log_prefix=self._log_prefix,
            cmd=cmd,
            logger=self.logger,
            cwd=cwd,
            timeout=timeout,
            silent=silent,
            event_handler=self.event_handler,
            executor=self.executor,
        )

    def install_mcp_servers(self, workspace: Path, servers: list[MCPServerSpec]) -> None:
        """Merge servers into ``<workspace>/.mcp.json`` for auto-discovery."""
        server_config: dict[str, dict[str, Any]] = {
            s.name: {
                "command": s.command,
                "args": list(s.args),
                **({"env": dict(s.env)} if s.env else {}),
            }
            for s in servers
        }
        self._install_mcp_config_file(
            workspace / ".mcp.json",
            server_key="mcpServers",
            server_config=server_config,
        )

    def uninstall_mcp_servers(self, workspace: Path, servers: list[MCPServerSpec]) -> None:  # noqa: ARG002  # tracked: #288
        """Restore the workspace's original ``.mcp.json``. Idempotent."""
        self._restore_mcp_config_file(workspace / ".mcp.json")
