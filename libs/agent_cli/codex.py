from pathlib import Path

from agentshim.codex import CodexGenerationSession
from agentshim.events import AgentEventHandler

from .base import MCPServerSpec
from .cli_agent import CLICodingAgent


def _toml_str(value: str) -> str:
    """Quote *value* as a TOML basic string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_array(values: list[str]) -> str:
    """Render a list of strings as a TOML inline array of basic strings."""
    return "[" + ",".join(_toml_str(v) for v in values) + "]"


class CodexCodingAgent(CLICodingAgent):
    """Coding agent implementation using the Codex CLI tool."""

    def __init__(
        self,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor=None,
    ):
        """Initialize the Codex coding agent.

        Args:
            model: Optional model name to use with codex. If None, uses default.
            event_handler: Optional event handler for UI updates.
            executor: Optional agentshim :class:`CommandExecutor`.
        """
        super().__init__(
            "codex",
            model,
            event_handler,
            executor=executor,
        )
        # Extra ``--config key=value`` flags appended to ``codex exec`` by
        # :meth:`_get_command`. Populated by :meth:`install_mcp_servers`
        # because Codex has no project-level config file discovery — its
        # only project-scoped knob is the runtime ``--config`` override
        # layer (verified via codex-rs/core/src/config_loader/README.md).
        self.base_config_args: list[str] = []
        self.extra_config_args: list[str] = []

    @property
    def codex_path(self) -> str:
        """Return path to codex binary (for backward compatibility)."""
        return self.binary_path

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return "[Codex]"

    def _get_command(self, prompt: str) -> list[str]:
        cmd = [
            self.binary_path, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.base_config_args:
            cmd.extend(self.base_config_args)
        if self.extra_config_args:
            cmd.extend(self.extra_config_args)
        return cmd

    def _get_resume_command(self, prompt: str, session_id: str) -> list[str]:
        # ``codex exec resume`` does NOT fall back to stdin when the ``[PROMPT]``
        # positional is omitted — only the literal ``-`` sentinel makes it read
        # from stdin. Pass ``-`` so the prompt we write to the subprocess's
        # stdin is actually consumed. (``codex exec`` is more lenient and
        # treats stdin as the default fallback, so we don't need this there.)
        cmd = [
            self.binary_path, "exec", "resume", session_id, "-",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.base_config_args:
            cmd.extend(self.base_config_args)
        if self.extra_config_args:
            cmd.extend(self.extra_config_args)
        return cmd

    def _create_session(self, cmd, cwd=None, timeout=None, silent=False):
        return CodexGenerationSession(
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

    def _extract_session_id(self, session: CodexGenerationSession) -> str | None:
        return session.session_id

    def install_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Stash ``--config mcp_servers.<name>.<key>=<value>`` flags on the
        instance for the next ``codex exec`` invocation.

        Codex has no project-scoped config file (its config loader only
        looks at MDM, system-managed config, session ``--config`` flags,
        and ``~/.codex/config.toml``), so MCP servers are configured by
        passing dotted-path TOML overrides at the command line. ``--config``
        values are parsed as TOML literals, so strings need TOML quoting
        and arrays use TOML inline array syntax.

        TOML table keys are snake_case by convention, so ``"vibeserve-issues"``
        becomes ``mcp_servers.vibeserve_issues``.
        """
        flags: list[str] = []
        for s in servers:
            key = s.name.replace("-", "_")
            flags.extend(
                [
                    "--config",
                    f"mcp_servers.{key}.command={_toml_str(s.command)}",
                    "--config",
                    f"mcp_servers.{key}.args={_toml_array(list(s.args))}",
                ]
            )
            for env_key, env_val in s.env.items():
                flags.extend(
                    [
                        "--config",
                        f"mcp_servers.{key}.env.{env_key}={_toml_str(env_val)}",
                    ]
                )
        self.extra_config_args = flags

    def uninstall_mcp_servers(
        self, workspace: Path, servers: list[MCPServerSpec]
    ) -> None:
        """Clear the runtime ``--config`` flags. Idempotent."""
        self.extra_config_args = []
