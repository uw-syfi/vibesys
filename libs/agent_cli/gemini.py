import json
from pathlib import Path
from typing import Any

from agentshim.events import AgentEventHandler
from agentshim.gemini import GeminiGenerationSession

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent


class GeminiCodingAgent(CLICodingAgent):
    """Coding agent implementation using the Gemini CLI tool."""

    def __init__(
        self,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor=None,
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

    def install_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Write ``<workspace>/.gemini/settings.json`` so Gemini CLI
        auto-discovers the MCP servers from cwd. ``trust: true`` skips
        Gemini's per-tool approval prompts for these servers."""
        gemini_dir = workspace / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        config: dict[str, Any] = {
            "mcpServers": {
                s.name: {
                    "command": s.command,
                    "args": list(s.args),
                    "trust": True,
                    **({"env": dict(s.env)} if s.env else {}),
                }
                for s in servers
            }
        }
        (gemini_dir / "settings.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    def uninstall_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Remove ``<workspace>/.gemini/settings.json``. Leaves the
        ``.gemini/`` directory itself in place because Gemini also writes
        session data into it (e.g. ``.gemini/tmp/``)."""
        target = workspace / ".gemini" / "settings.json"
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
