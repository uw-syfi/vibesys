"""The descriptor-backed request for one orchestration run."""

from __future__ import annotations

from pathlib import Path

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


class RunRequest(BaseModel):
    """Resolved input and execution settings for a registered orchestration.

    The selected policy owns and validates ``orchestration.options`` before
    run setup begins.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: Path
    orchestration: OrchestrationDescriptor
    config: Config
    input_bundle: InputBundle
    objective: str | None = None
    debug: bool = False
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
