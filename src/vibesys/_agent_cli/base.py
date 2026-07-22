import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from vs_sandbox import WorkspaceSandbox


@dataclass
class MCPServerSpec:
    """Provider-agnostic stdio MCP server description.

    Each provider's :meth:`CodingAgent.install_mcp_servers` consumes a list
    of these and serializes them into the format that provider's CLI expects
    (e.g. ``.mcp.json`` for Claude Code, ``.gemini/settings.json`` for
    Gemini, ``opencode.json`` for opencode, ``--config`` flags for Codex).
    """

    name: str
    """Server identifier (e.g. ``"vibesys-issues"``)."""

    command: str
    """Executable to launch (e.g. ``"python"``)."""

    args: list[str]
    """Arguments passed to *command*."""

    env: dict[str, str] = field(default_factory=dict)
    """Optional environment variables for the spawned MCP server process."""


class CodingAgent(ABC):
    """Abstract base class for coding agents."""

    event_handler: Any | None = None

    # Declared (not assigned) here: every concrete provider sets these in its
    # ``__init__``.  ``env`` is the subprocess environment the CLI is spawned
    # with; ``executor`` is the agentshim ``CommandExecutor`` (host, docker,
    # or modal) that runs the CLI binary.
    env: dict[str, str]
    executor: Any

    # Optional OS-level confinement for host execution (issue #149); set by
    # the cli runner on the host path, left as None under container executors.
    sandbox: WorkspaceSandbox | None

    def _install_mcp_config_file(
        self,
        target: Path,
        *,
        server_key: str,
        server_config: dict[str, dict[str, Any]],
        defaults: dict[str, Any] | None = None,
    ) -> None:
        """Temporarily merge MCP servers into a JSON config file.

        The original bytes are retained so :meth:`_restore_mcp_config_file`
        can put a workspace-owned config back exactly as it was. Keeping the
        snapshot on the agent also lets reused agents manage different
        workspaces without sharing restoration state.
        """
        backups = getattr(self, "_mcp_config_backups", None)
        if backups is None:
            backups = {}
            self._mcp_config_backups: dict[Path, bytes | None] = backups
        if target in backups:
            raise RuntimeError(f"temporary MCP config is already installed at {target}")

        original = target.read_bytes() if target.exists() else None
        config: dict[str, Any] = {}
        if original is not None:
            try:
                loaded: object = json.loads(original)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"cannot merge MCP servers into invalid JSON config: {target}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"MCP config must contain a JSON object: {target}")
            config = dict(cast(dict[str, Any], loaded))

        existing_servers = config.get(server_key, {})
        if not isinstance(existing_servers, dict):
            raise ValueError(f"{server_key!r} must be a JSON object in MCP config: {target}")
        if defaults:
            for key, value in defaults.items():
                config.setdefault(key, value)
        config[server_key] = {
            **cast(dict[str, Any], existing_servers),
            **server_config,
        }

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config, indent=2), encoding="utf-8")
        backups[target] = original

    def _restore_mcp_config_file(self, target: Path) -> None:
        """Restore a config saved by :meth:`_install_mcp_config_file`."""
        backups = getattr(self, "_mcp_config_backups", None)
        if backups is None or target not in backups:
            return

        original = backups[target]
        if original is None:
            if target.exists():
                target.unlink()
        else:
            target.write_bytes(original)
        del backups[target]

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

    def install_mcp_servers(self, workspace: Path, servers: list[MCPServerSpec]) -> None:
        """Install per-agent MCP server config so the next :meth:`generate`
        call exposes these stdio servers as tools.

        Default implementation is a no-op for providers that don't (yet)
        support MCP. Subclasses override to write the appropriate config
        file under *workspace*, or (in Codex's case) to stash runtime
        ``--config`` flags on the instance.
        """
        return None

    def uninstall_mcp_servers(self, workspace: Path, servers: list[MCPServerSpec]) -> None:
        """Remove anything written by :meth:`install_mcp_servers`.

        Idempotent. Default implementation is a no-op.
        """
        return None
