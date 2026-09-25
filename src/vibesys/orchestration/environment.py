"""Runtime environment protocol shared with orchestration policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.config import Config
    from vibesys.skills import SkillSelection
    from vs_agent.api import MCPServerSpec
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox


class AgentEnvironment(Protocol):
    """A live agent-construction environment opened for one run.

    Returned by `vibesys.api.session.RunAgentHost.open_agent_environment`.
    Carries exactly what `server.chat.factory.build_chat_agent` needs to build
    a sibling agent over the run's workspace: the construction inputs (`config`,
    `skill_selection`,
    `skill_source_dirs`, `project_path_policy`, `host_resources`), the opened
    sandbox's shape (`backends`, `use_docker`, `isolated`), its path
    translation (`agent_path`), and its lifetime (`close`).

    Every data member is a read-only property, not a plain attribute: no
    caller writes any of them, and this protocol's sole real implementation
    (`vibesys.api.session._OpenedAgentEnvironment`) is a frozen dataclass, so
    a plain attribute (implicitly read-write) would make it structurally
    incompatible with this protocol.
    """

    @property
    def config(self) -> Config:
        """This environment's agent configuration."""
        ...

    @property
    def skill_selection(self) -> SkillSelection:
        """The skill-pruning policy a sibling agent should apply while copying."""
        ...

    @property
    def skill_source_dirs(self) -> tuple[Path, ...]:
        """Directories a sibling agent should load skills from."""
        ...

    @property
    def project_path_policy(self) -> ProjectPathPolicy:
        """The path policy governing this environment's project access."""
        ...

    @property
    def host_resources(self) -> tuple[HostResource, ...]:
        """Host resources mounted into this environment."""
        ...

    @property
    def backends(self) -> dict[str, Sandbox] | None:
        """Sandbox handles for this environment's execution surfaces, if sandboxed."""
        ...

    @property
    def use_docker(self) -> bool:
        """Whether this environment's CLI runs sandboxed under Docker."""
        ...

    @property
    def isolated(self) -> bool:
        """Whether this environment runs with an isolated (non-host-mounted) workspace."""
        ...

    def agent_path(self, host: Path) -> str:
        """Map a host path to its path inside this environment's sandbox."""
        ...

    def investigation_tools(self) -> tuple[MCPServerSpec, ...]:
        """Return the read-only MCP tool servers for investigating this run's history.

        Each spec launches a `vibesys.api.chat_tools_server` subprocess scoped
        to this environment's run, exposing its read-model
        (`vibesys.api.RunStore`) as MCP tools instead of materializing files
        into the sandbox for a shell to `rg`/`tail`.
        """
        ...

    def close(self) -> None:
        """Release the opened environment session."""
        ...
