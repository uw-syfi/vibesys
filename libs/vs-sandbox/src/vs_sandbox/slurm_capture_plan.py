"""Machine-local plan consumed by remote profiler capture adapters."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vs_decode.api import Unreadable, ValidationOptions, read_json
from vs_project.api import atomic_write_bytes

_SUPPORT_NAME = re.compile(r"^(?!\.\.?$)[A-Za-z0-9.][A-Za-z0-9._-]*$")


def _validate_support_paths(value: dict[str, Path]) -> dict[str, Path]:
    if any(_SUPPORT_NAME.fullmatch(name) is None for name in value):
        raise ValueError("support path names must be safe workspace basenames")  # noqa: TRY003  # lint-waiver: LW-930035 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
    return value


_MAX_PLAN_BYTES = 16 * 1024 * 1024
"""The largest capture plan file read."""


class SlurmCapturePlanError(ValueError):
    """A machine-local capture plan is malformed."""

    def __init__(self, path: Path) -> None:
        """Name the invalid plan path without echoing its contents."""
        super().__init__(f"invalid Slurm capture plan at {path}")


class SlurmCapturePlan(BaseModel):
    """Trusted service load command and immutable support trees."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # Legacy plans remain readable; remote capture requires explicit durable storage.
    cluster_state_root: Path | None = None
    profile_command: tuple[str, ...] | None = None
    profile_timeout_seconds: int | None = Field(default=None, gt=0)
    support_paths: dict[str, Path] = Field(default_factory=dict)

    @field_validator("profile_command")
    @classmethod
    def _valid_command(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (not value or any(not part for part in value)):
            raise ValueError("profile_command must contain non-empty argv")  # noqa: TRY003  # lint-waiver: LW-930036 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    @field_validator("support_paths")
    @classmethod
    def _valid_support_paths(cls, value: dict[str, Path]) -> dict[str, Path]:
        return _validate_support_paths(value)


class SlurmEvaluationPlan(BaseModel):
    """Trusted commands and support trees the cluster executor stages for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    config_path: Path
    # Legacy plans remain readable; execution requires an explicit durable
    # writable root and rejects its absence before contacting the cluster.
    cluster_state_root: Path | None = None
    accuracy_command: tuple[str, ...] | None = None
    benchmark_command: tuple[str, ...] | None = None
    benchmark_output_argument: str | None = None
    support_paths: dict[str, Path] = Field(default_factory=dict)
    # The trusted serving profile capture (see ``slurm_profile``); ``None`` when
    # the run stages no profiler or configures no service, so the executor
    # cannot produce profile evidence.
    profile_command: tuple[str, ...] | None = None

    @field_validator("accuracy_command", "benchmark_command", "profile_command")
    @classmethod
    def _valid_commands(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (not value or any(not part for part in value)):
            raise ValueError("evaluation commands must contain non-empty argv")  # noqa: TRY003  # lint-waiver: LW-930037 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    @field_validator("benchmark_output_argument")
    @classmethod
    def _valid_output_argument(cls, value: str | None) -> str | None:
        if value == "":
            raise ValueError("benchmark_output_argument must be non-empty")  # noqa: TRY003  # lint-waiver: LW-930038 [TRY003]; this validation boundary must raise ValueError with its precise contract message; a custom exception class would add a public type without improving recovery.
        return value

    @field_validator("support_paths")
    @classmethod
    def _valid_evaluation_support_paths(cls, value: dict[str, Path]) -> dict[str, Path]:
        return _validate_support_paths(value)


def write_slurm_capture_plan(path: Path, plan: SlurmCapturePlan) -> None:
    """Durably publish a complete capture plan without credential values."""
    atomic_write_bytes(path, (plan.model_dump_json() + "\n").encode())


def read_slurm_capture_plan(path: Path) -> SlurmCapturePlan:
    """Load a strict machine-local capture plan."""
    plan = read_json(
        path, SlurmCapturePlan, max_bytes=_MAX_PLAN_BYTES, options=ValidationOptions(strict=True)
    )
    if isinstance(plan, Unreadable):
        raise SlurmCapturePlanError(path)
    return plan
