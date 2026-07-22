import json
import os
import tempfile
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


@dataclass(frozen=True)
class _MCPConfigBackup:
    original_bytes: bytes | None
    original_config: dict[str, Any]
    installed_config: dict[str, Any]
    server_key: str


_MISSING = object()


def _atomic_write(target: Path, content: bytes) -> None:
    """Replace *target* without exposing a partially written config file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode if target.exists() else None
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_json_object(raw: bytes, target: Path) -> dict[str, Any]:
    try:
        loaded: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot merge MCP servers into invalid JSON config: {target}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"MCP config must contain a JSON object: {target}")
    return dict(cast(dict[str, Any], loaded))


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
            self._mcp_config_backups: dict[Path, _MCPConfigBackup] = backups
        if target in backups:
            raise RuntimeError(f"temporary MCP config is already installed at {target}")

        original = target.read_bytes() if target.exists() else None
        original_config = _load_json_object(original, target) if original is not None else {}
        config = dict(original_config)

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

        backup = _MCPConfigBackup(
            original_bytes=original,
            original_config=original_config,
            installed_config=config,
            server_key=server_key,
        )
        backups[target] = backup
        try:
            _atomic_write(target, json.dumps(config, indent=2).encode())
        except BaseException:
            del backups[target]
            raise

    def _restore_mcp_config_file(self, target: Path) -> None:
        """Restore a config saved by :meth:`_install_mcp_config_file`."""
        backups = getattr(self, "_mcp_config_backups", None)
        if backups is None or target not in backups:
            return

        backup = backups[target]
        if not target.exists():
            # Deletion during the invocation is a workspace edit, not something
            # cleanup should silently undo.
            del backups[target]
            return

        current = _load_json_object(target.read_bytes(), target)
        if current == backup.installed_config:
            if backup.original_bytes is None:
                target.unlink()
            else:
                _atomic_write(target, backup.original_bytes)
            del backups[target]
            return

        original_servers = backup.original_config.get(backup.server_key, {})
        installed_servers = backup.installed_config.get(backup.server_key, {})
        current_servers = current.get(backup.server_key, {})
        if not all(isinstance(value, dict) for value in (original_servers, installed_servers)):
            raise ValueError(f"{backup.server_key!r} must be a JSON object in MCP config: {target}")
        if not isinstance(current_servers, dict):
            raise ValueError(f"{backup.server_key!r} must be a JSON object in MCP config: {target}")

        restored_servers = dict(cast(dict[str, Any], current_servers))
        original_server_map = cast(dict[str, Any], original_servers)
        for name, installed_value in cast(dict[str, Any], installed_servers).items():
            original_value = original_server_map.get(name, _MISSING)
            if installed_value == original_value:
                continue
            if restored_servers.get(name, _MISSING) != installed_value:
                continue
            if original_value is _MISSING:
                restored_servers.pop(name, None)
            else:
                restored_servers[name] = original_value

        if restored_servers or backup.server_key in backup.original_config:
            current[backup.server_key] = restored_servers
        else:
            current.pop(backup.server_key, None)

        for key, installed_value in backup.installed_config.items():
            if key == backup.server_key:
                continue
            original_value = backup.original_config.get(key, _MISSING)
            if installed_value == original_value or current.get(key, _MISSING) != installed_value:
                continue
            if original_value is _MISSING:
                current.pop(key, None)
            else:
                current[key] = original_value

        if current:
            _atomic_write(target, json.dumps(current, indent=2).encode())
        else:
            target.unlink()
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
