from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MCPServerSpec:
    """Provider-agnostic stdio MCP server description.

    Each provider's :meth:`CodingAgent.install_mcp_servers` consumes a list
    of these and serializes them into the format that provider's CLI expects
    (e.g. ``.mcp.json`` for Claude Code, ``.gemini/settings.json`` for
    Gemini, ``opencode.json`` for opencode, ``--config`` flags for Codex).
    """

    name: str
    """Server identifier (e.g. ``"vibeserve-issues"``)."""

    command: str
    """Executable to launch (e.g. ``"python"``)."""

    args: list[str]
    """Arguments passed to *command*."""

    env: dict[str, str] = field(default_factory=dict)
    """Optional environment variables for the spawned MCP server process."""


class CodingAgent(ABC):
    """Abstract base class for coding agents."""

    event_handler: Any | None = None

    @abstractmethod
    def generate(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> str:
        """Generate text/code based on a prompt.

        Args:
            prompt: The prompt to send to the agent.
            cwd: Optional working directory context.
            timeout: Timeout in seconds. ``None`` means no timeout.
            silent: If True, suppress stdout printing of the agent's output.

        Returns:
            Generated text.
        """

    def install_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Install per-agent MCP server config so the next :meth:`generate`
        call exposes these stdio servers as tools.

        Default implementation is a no-op for providers that don't (yet)
        support MCP. Subclasses override to write the appropriate config
        file under *workspace*, or (in Codex's case) to stash runtime
        ``--config`` flags on the instance.
        """
        return None

    def uninstall_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Remove anything written by :meth:`install_mcp_servers`.

        Idempotent. Default implementation is a no-op.
        """
        return None
