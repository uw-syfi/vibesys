import json
from pathlib import Path
from typing import Any

from agentshim.claude import ClaudeGenerationSession
from agentshim.events import AgentEventHandler

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent


class ClaudeCodeCodingAgent(CLICodingAgent):
    """Coding agent implementation using the Claude Code CLI tool."""

    def __init__(
        self,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor=None,
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

    @property
    def claude_path(self) -> str:
        """Return path to claude binary (for backward compatibility)."""
        return self.binary_path

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return "[Claude]"

    def _get_command(self, prompt: str) -> list[str]:
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
        return cmd

    def _get_resume_command(self, prompt: str, session_id: str) -> list[str]:
        cmd = [
            self.binary_path,
            "--resume", session_id,
            "-p",  # Print mode, reads prompt from stdin
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        return cmd

    def _extract_session_id(self, session: ClaudeGenerationSession) -> str | None:
        return session.session_id

    def _create_session(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> ClaudeGenerationSession:
        return ClaudeGenerationSession(
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

    def install_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Write ``<workspace>/.mcp.json`` so Claude Code auto-discovers
        the MCP servers from cwd."""
        config: dict[str, Any] = {
            "mcpServers": {
                s.name: {
                    "command": s.command,
                    "args": list(s.args),
                    **({"env": dict(s.env)} if s.env else {}),
                }
                for s in servers
            }
        }
        (workspace / ".mcp.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    def uninstall_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Remove ``<workspace>/.mcp.json``. Idempotent and tolerant of
        files written from inside Docker (root-owned)."""
        target = workspace / ".mcp.json"
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
