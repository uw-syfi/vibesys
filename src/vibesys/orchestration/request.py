"""Generic run request contract and versioned descriptor request."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.evaluators.input_manifest import InputBundle
from vibesys.profilers import ProfilerKind
from vibesys.repository import RepositoryVisibility
from vibesys.sandbox.run_environment import RunEnvironmentSpec
from vs_project.api import OrchestrationDescriptor


class ResumeRef(BaseModel):
    """Identifies a prior run a new session should resume from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str


class OrchestrationRunRequest(BaseModel):
    """Descriptor-based request for a registered orchestration policy.

    Policy-specific settings belong in ``orchestration.options``. The legacy
    ``RunRequest`` remains available for callers of the built-in loops.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    orchestration: OrchestrationDescriptor
    config: Config
    input_bundle: InputBundle
    objective: str | None = None
    resume: ResumeRef | None = None
    exp_name: str | None = None
    runs_dir: Path | None = None
    profiler_kind: ProfilerKind = ProfilerKind.AUTO
    skills_dirs: list[str] | None = None
    run_environment: RunEnvironmentSpec | None = None
    agent_backend: str | None = None
    cli_provider: str | None = None
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND
    remote_repo: str | None = None
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE

    @property
    def orchestration_id(self) -> str:
        """Return the stable ID selected by this descriptor."""
        return self.orchestration.id


class RunRequestLike(Protocol):
    """Facts the framework needs to run any registered orchestration."""

    @property
    def project_root(self) -> Path:
        """Return the project workspace root."""
        ...

    @property
    def orchestration(self) -> OrchestrationDescriptor | None:
        """Return the versioned descriptor when selected."""
        ...

    @property
    def config(self) -> Config:
        """Return shared agent and runtime configuration."""
        ...

    @property
    def input_bundle(self) -> InputBundle:
        """Return the resolved input bundle."""
        ...

    @property
    def objective(self) -> str | None:
        """Return the objective text when supplied."""
        ...

    @property
    def resume(self) -> ResumeRef | None:
        """Return the run to resume when supplied."""
        ...

    @property
    def exp_name(self) -> str | None:
        """Return the requested experiment name."""
        ...

    @property
    def runs_dir(self) -> Path | None:
        """Return the optional runs directory."""
        ...

    @property
    def profiler_kind(self) -> ProfilerKind:
        """Return the requested profiler selection."""
        ...

    @property
    def skills_dirs(self) -> list[str] | None:
        """Return optional skill source directories."""
        ...

    @property
    def run_environment(self) -> RunEnvironmentSpec | None:
        """Return the selected execution environment."""
        ...

    @property
    def agent_backend(self) -> str | None:
        """Return the default agent backend."""
        ...

    @property
    def cli_provider(self) -> str | None:
        """Return the default CLI provider."""
        ...

    @property
    def backend(self) -> ComputeBackend:
        """Return the compute backend."""
        ...

    @property
    def remote_repo(self) -> str | None:
        """Return the optional remote repository."""
        ...

    @property
    def repo_visibility(self) -> RepositoryVisibility:
        """Return the remote repository visibility."""
        ...

    @property
    def orchestration_id(self) -> str:
        """Return the registered orchestration stable ID."""
        ...
