"""Canonical public request and read models for VibeSys runs."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.inputs import InputBundle
from vibesys.repository import RepositoryVisibility
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api.infrastructure import RunEnvironmentSpec


class ResumeRef(BaseModel):
    """Identify a prior run that a new session should resume from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str


class ProfilerKind(StrEnum):
    """Known profiler modes selectable in a run request."""

    AUTO = "auto"
    NONE = "none"
    NSYS = "nsys"
    NCU = "ncu"
    ROCPROF = "rocprof"
    OTEL = "otel"
    TORCH = "torch"
    NEURON = "neuron"
    MACOS_CPU = "macos_cpu"
    LINUX_CPU = "linux_cpu"
    HEADROOM = "headroom"


class RunRequest(BaseModel):
    """Resolved input and execution settings for one orchestration run.

    The selected policy owns and validates ``orchestration.options`` before
    run setup begins.
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
    def resolved_run_id(self) -> str:
        """Return the resume target or the identity of a fresh run."""
        if self.resume is not None:
            return self.resume.run_id
        if self.exp_name is None:
            message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
            raise ValueError(message)
        return self.exp_name


class RunResult(BaseModel):
    """Terminal outcome of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    succeeded: bool


class RunStatus(StrEnum):
    """Lifecycle status the public API can report for a run.

    This is narrower than event status. A live session reports ``ACTIVE`` and
    then its terminal state. Persisted runs carry no lifecycle field, so the
    read store reports ``UNKNOWN`` rather than guessing process state.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class RoundSummary(BaseModel):
    """One policy round in the strategy-neutral shape used for run events."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    number: int
    status: Literal["completed", "failed"]
    attempts: int
    judge_verdict: Literal["pass", "fail", "skipped"] | None = None
    perf_metric: float | None = None
    perf_unit: str | None = None
    profile_skipped: bool = False


class PluginProjection(BaseModel):
    """Policy-owned JSON plus product-generic facts exposed by VibeSys."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    payload: dict[str, JsonValue] | None
    rounds: tuple[RoundSummary, ...] = ()
    experiment_revision: int | None = None


class RunView(BaseModel):
    """Read-only run identity, lifecycle, and policy-owned JSON projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    status: RunStatus
    projection: dict[str, JsonValue] | None = None
    rounds: tuple[RoundSummary, ...] = ()
    experiment_revision: int | None = None


__all__ = [
    "PluginProjection",
    "ProfilerKind",
    "ResumeRef",
    "RoundSummary",
    "RunRequest",
    "RunResult",
    "RunStatus",
    "RunView",
]
