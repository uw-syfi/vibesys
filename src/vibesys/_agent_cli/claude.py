import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentshim.claude import ClaudeGenerationSession
from agentshim.events import AgentEventHandler

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent

if TYPE_CHECKING:
    from agentshim.executor import CommandExecutor


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
        super().__init__(**kwargs)
        # ``None`` means "no schema-enforced payload seen"; a captured value is
        # any JSON type the schema root produced (always an object for VibeSys
        # response models).
        self.structured_output: Any = None

    def _process_stdout(self, line: str) -> None:
        self._capture_structured_output(line)
        super()._process_stdout(line)

    def _capture_structured_output(self, line: str) -> None:
        if not line:
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
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
