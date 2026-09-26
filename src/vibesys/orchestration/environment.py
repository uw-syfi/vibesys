"""Runtime environment protocol shared with orchestration policies, and ``ctx.environment``."""

# ``_Environment`` shares one private owner for resource lifetime with the rest of
# the host capabilities split out of runtime.py.
# lint-waiver: LW-040099 [SLF001]; capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vibesys.sandbox.model_requests import ModelRequestError
from vibesys.sandbox.model_requests import reconcile_model_requests as stage_model_requests

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.config import Config
    from vibesys.context import _RunResources
    from vibesys.evaluators.input_manifest import WorkspaceSource
    from vibesys.orchestration._host import HostResources
    from vibesys.orchestration.workspaces import WorkspaceHandle
    from vibesys.profilers import ProfilerKind
    from vibesys.runtime import WorkspaceScope
    from vibesys.sandbox.run_environment import CandidateRuntime, RunEnvironmentView
    from vibesys.skills import SkillSelection
    from vs_agent.api import ToolServerDescriptor
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox, SandboxExecutionResult


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

    def investigation_tools(self) -> tuple[ToolServerDescriptor, ...]:
        """Return the read-only tool servers for investigating this run's history.

        Each descriptor launches a `vibesys.api.chat_tools_server` subprocess scoped
        to this environment's run, exposing its read-model
        (`vibesys.api.RunStore`) as MCP tools instead of materializing files
        into the sandbox for a shell to `rg`/`tail`.
        """
        ...

    def close(self) -> None:
        """Release the opened environment session."""
        ...


class _Environment:
    """Generic execution facts and candidate deployment lifecycle."""

    def __init__(self, host: HostResources) -> None:
        self._host = host

    @property
    def view(self) -> RunEnvironmentView:
        """Return the resolved root environment's policy-neutral facts."""
        return self.view_for()

    def view_for(self, scope: WorkspaceScope | WorkspaceHandle | None = None) -> RunEnvironmentView:
        """Return environment facts for one live workspace."""
        return self._host.workspaces._resources_for(scope).run_environment_view

    @property
    def reference_path(self) -> str:
        """Return the prompt-visible reference path."""
        return self._host._resources.ref_name

    @property
    def workspace_sources(self) -> tuple[WorkspaceSource, ...]:
        """Return materialized workspace sources for prompt context."""
        return self._host._resources.workspace_sources

    @property
    def skill_source_paths(self) -> tuple[Path, ...]:
        """Return the resolved skill source directories for this run."""
        return tuple(self._host._resources.skill_source_paths)

    @property
    def profiler_kind(self) -> ProfilerKind:
        """Return the profiler selected after environment preflight."""
        return self._host._resources.profiler_kind

    @property
    def model_name(self) -> str:
        """Return the resolved default model name."""
        return self._host._resources.model_name

    @property
    def run_log_path(self) -> Path:
        """Return the current run log file path."""
        return self._host._resources.run_log_path

    @property
    def log_dir(self) -> Path:
        """Return this run's machine-local log directory."""
        return self._host._resources.log_dir

    def candidate_runtime(
        self,
        generation: int,
        child_idx: int,
        *,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
    ) -> CandidateRuntime:
        """Resolve candidate prompt notes and deployment identity."""
        context = self._host.workspaces._resources_for(scope)
        return context.run_environment.candidate_runtime(
            context.run_environment_view, generation, child_idx
        )

    async def teardown_deployment(self, name: str) -> None:
        """Release a candidate deployment through the selected environment."""
        context = self._host._resources
        await self._host._run_blocking(
            context.run_environment.teardown_deployment, name, log=context.lprint
        )

    async def reconcile_model_requests(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> str | None:
        """Stage candidate-declared Modal model weights before trusted gates."""
        context = self._host.workspaces._resources_for(scope)
        if context.run_environment_view.env_kind != "modal":
            return None
        return await self._host._run_blocking(self._stage_model_requests, context)

    @staticmethod
    def _stage_model_requests(context: _RunResources) -> str | None:
        try:
            volumes = stage_model_requests(context.workspace, log=context.lprint)
        except ModelRequestError as exc:
            context.lprint(f"[model-request] rejected: {exc}")
            return f"Model-weight request could not be satisfied: {exc}"
        if volumes:
            context.lprint(
                f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes)
            )
        return None

    async def reselect_device(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> None:
        """Rebalance the device assigned to a workspace before a paid turn."""
        context = self._host.workspaces._resources_for(scope)
        await self._host._run_blocking(context.reselect_gpu)

    async def execute(
        self,
        command: str,
        *,
        timeout_seconds: int | None = None,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
    ) -> SandboxExecutionResult:
        """Execute a policy-selected command in the selected run environment."""
        context = self._host.workspaces._resources_for(scope)
        return await self._host._run_blocking(
            context.run_environment_session.sandbox.execute, command, timeout=timeout_seconds
        )
