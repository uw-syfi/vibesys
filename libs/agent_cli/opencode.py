import json
from pathlib import Path
from typing import Any

from agentshim.events import AgentEventHandler
from agentshim.opencode import OpencodeGenerationSession

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent

OPENCODE_DEFAULT_MODEL = "google-vertex/gemini-3-pro-preview"


class OpencodeCodingAgent(CLICodingAgent):
    """Coding agent implementation using the Opencode CLI tool."""

    def __init__(
        self,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor=None,
    ):
        """Initialize the Opencode coding agent.

        Args:
            model: Optional model name to use.
            event_handler: Optional event handler for UI updates.
            executor: Optional agentshim :class:`CommandExecutor`.
        """
        if not model:
            model = OPENCODE_DEFAULT_MODEL
        super().__init__(
            "opencode",
            model,
            event_handler,
            executor=executor,
        )

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return "[Opencode]"

    def _get_command(self, prompt: str) -> list[str]:
        cmd = [self.binary_path, "run", f'"{prompt}"']

        if self.model:
            cmd.extend(["--model", self.model])

        # Output in json format
        cmd.extend(["--format=json"])

        return cmd

    def _create_session(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> OpencodeGenerationSession:
        return OpencodeGenerationSession(
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
        """Write ``<workspace>/opencode.json`` so opencode auto-discovers
        the MCP servers from cwd. opencode uses the ``mcp`` key (not
        ``mcpServers``) and a single combined ``command`` array. Non-
        interactive ``opencode run`` already auto-approves all permissions,
        so no extra ``permission`` block is needed."""
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                s.name: {
                    "type": "local",
                    "command": [s.command, *s.args],
                    "enabled": True,
                    **({"environment": dict(s.env)} if s.env else {}),
                }
                for s in servers
            },
        }
        (workspace / "opencode.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )

    def uninstall_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Remove ``<workspace>/opencode.json``. Idempotent."""
        target = workspace / "opencode.json"
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
