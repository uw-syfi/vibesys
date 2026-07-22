from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentshim.events import AgentEventHandler
from agentshim.gemini import GeminiGenerationSession

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent

if TYPE_CHECKING:
    from agentshim.executor import CommandExecutor


class GeminiCodingAgent(CLICodingAgent[GeminiGenerationSession]):
    """Coding agent implementation using the Gemini CLI tool."""

    def __init__(
        self,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor: "CommandExecutor | None" = None,
    ):
        """Initialize the Gemini coding agent.

        Args:
            model: Optional model name to use.
            event_handler: Optional event handler for UI updates.
            executor: Optional agentshim :class:`CommandExecutor`.
        """
        super().__init__(
            "gemini",
            model,
            event_handler,
            executor=executor,
        )

    @property
    def gemini_path(self) -> str:
        """Return path to gemini binary (for backward compatibility)."""
        return self.binary_path

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return "[Gemini]"

    def _get_command(self, prompt: str) -> list[str]:
        cmd = [self.binary_path]

        # Enable yolo mode
        cmd.extend(["-y"])

        if self.model:
            cmd.extend(["--model", self.model])

        # Output in stream-json format
        cmd.extend(["-o", "stream-json"])

        return cmd

    def _create_session(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> GeminiGenerationSession:
        return GeminiGenerationSession(
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
        """Merge servers into ``<workspace>/.gemini/settings.json``.

        Gemini CLI discovers the servers from cwd. ``trust: true`` skips
        Gemini's per-tool approval prompts for these servers."""
        server_config: dict[str, dict[str, Any]] = {
            s.name: {
                "command": s.command,
                "args": list(s.args),
                "trust": True,
                **({"env": dict(s.env)} if s.env else {}),
            }
            for s in servers
        }
        self._install_mcp_config_file(
            workspace / ".gemini" / "settings.json",
            server_key="mcpServers",
            server_config=server_config,
        )

    def uninstall_mcp_servers(self, workspace: Path, servers: list[MCPServerSpec]) -> None:
        """Restore the original settings, leaving ``.gemini/`` in place."""
        self._restore_mcp_config_file(workspace / ".gemini" / "settings.json")
