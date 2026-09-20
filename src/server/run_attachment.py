"""Server-owned resources handed off from a live core run.

These dataclasses used to live in `vibesys.run.integration` as the shape of
the (now-deleted) `RunIntegration.attach_run` port. Core now hands off the
same facts through the type-erased `vibesys.run.integration.RunResourceHandoff`
via `RunSession.on_run_resources`; `RunIntegrationAdapter._handle_run_resources`
converts one into a `RunAttachment` here, which is what `ExperimentChatFactory`
and its callers still consume unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

from vs_agent import AgentSelection  # noqa: TC001  # tracked: #288

if TYPE_CHECKING:
    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vibesys.sandbox.run_environment import RunEnvironment, RunEnvironmentRequest
    from vs_project import Project
    from vs_sandbox import HostResource, ProjectPathPolicy


@dataclass(frozen=True, slots=True)
class AgentRuntimeResources:
    """Neutral run resources from which an application may build another agent."""

    config: Config
    compute_backend: ComputeBackend
    skill_source_dirs: tuple[Path, ...]
    environment: RunEnvironment
    environment_request: RunEnvironmentRequest
    run_environment_sandboxed: bool
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]


@dataclass(frozen=True, slots=True)
class RunAttachment:
    """Core resources exposed to optional application-owned run surfaces."""

    project: Project
    run_id: str
    workspace: Path
    log_dir: Path
    agent_backend: str
    agent_defaults: AgentSelection
    agent_runtime: AgentRuntimeResources
