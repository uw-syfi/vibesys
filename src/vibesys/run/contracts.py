"""Canonical public request and read models for VibeSys runs."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator, model_validator

from vibesys.config import Config
from vibesys.constants import DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.inputs import InputBundle
from vibesys.repository import RepositoryVisibility
from vs_project.api import OrchestrationDescriptor, ProjectStateError, validate_run_id
from vs_runtime.api.infrastructure import RunEnvironmentSpec


def _validate_identity_value(value: str, *, field: str) -> str:
    try:
        return validate_run_id(value)
    except ProjectStateError as exc:
        message = f"{field}: {exc}"
        raise ValueError(message) from exc


class ResumeRef(BaseModel):
    """Identify a prior run that a new session should resume from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str

    @field_validator("run_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _validate_identity_value(value, field="ResumeRef.run_id")


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
    run_id: str | None = None
    runs_dir: Path | None = None
    profiler_kind: ProfilerKind = ProfilerKind.AUTO
    skills_dirs: list[str] | None = None
    run_environment: RunEnvironmentSpec | None = None
    agent_backend: str | None = None
    cli_provider: str | None = None
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND
    remote_repo: str | None = None
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE

    @field_validator("run_id")
    @classmethod
    def _validate_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identity_value(value, field="RunRequest.run_id")

    @model_validator(mode="after")
    def _validate_resume_identity(self) -> RunRequest:
        if self.resume is not None and self.run_id not in (None, self.resume.run_id):
            message = "RunRequest.run_id must match resume.run_id"
            raise ValueError(message)
        return self

    @property
    def resolved_run_id(self) -> str:
        """Return the resume target or the identity of a fresh run."""
        if self.resume is not None:
            return self.resume.run_id
        if self.run_id is not None:
            return self.run_id
        if self.exp_name is None:
            message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
            raise ValueError(message)
        return self.exp_name


class RunStatus(StrEnum):
    """Lifecycle status the public API can report for a run.

    Persisted runs have no lifecycle field and report ``UNKNOWN``.
    A requested cooperative stop reports ``STOPPED`` after cleanup.
    """

    UNKNOWN = "unknown"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class RunResult(BaseModel):
    """Terminal outcome of one run, including a cooperative requested stop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    loop: str
    succeeded: bool
    status: Literal[RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED] = RunStatus.FAILED

    @model_validator(mode="before")
    @classmethod
    def _terminal_status(cls, value: object) -> object:
        if isinstance(value, dict) and "status" not in value:
            return {
                **value,
                "status": RunStatus.COMPLETED if value.get("succeeded") else RunStatus.FAILED,
            }
        return value

    @model_validator(mode="after")
    def _consistent_status(self) -> RunResult:
        if self.succeeded != (self.status is RunStatus.COMPLETED):
            message = "RunResult.succeeded must agree with status"
            raise ValueError(message)
        return self


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
