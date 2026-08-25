"""Operator-owned cluster profiles for SkyPilot-backed execution."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

if TYPE_CHECKING:
    from vs_project import RunResourceRequest

_PROFILE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_ALLOCATION_TIME = re.compile(
    r"^(?:(?P<days>\d+)-)?(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})$"
)
_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24


class ClusterProfileError(ValueError):
    """Raised when an operator profile document cannot be loaded or resolved."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SkyPilotProfile(_StrictModel):
    """Concrete SkyPilot capacity and runtime policy for one cluster profile."""

    runner: Literal["skypilot"]
    infra: Annotated[str, Field(min_length=1)]
    accelerator_backend: Literal["cuda", "rocm", "trainium"]
    accelerator_type: Annotated[str, Field(min_length=1)]
    accelerators_per_node: Annotated[int, Field(gt=0)]
    cpus_per_node: Annotated[int, Field(gt=0)] | None = None
    # SkyPilot's Slurm backend defaults GPU-task memory to 4 GB per requested
    # CPU when this is unset. That default can exceed a partition's actual
    # per-node RAM (e.g. many CPUs, comparatively little memory), which fails
    # resource resolution outright. Set this whenever cpus_per_node times 4
    # would overshoot the partition's real per-node memory.
    memory_gb_per_node: Annotated[int, Field(gt=0)] | None = None
    max_nodes: Annotated[int, Field(gt=0)] = 1
    exclusive: bool = True
    remote_runtime_image: str | None = None
    command_prefix: (
        Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)] | None
    ) = None
    allocation_time: str | None = None
    remote_artifact_root: str

    @field_validator("infra", "accelerator_type", "remote_runtime_image")
    @classmethod
    def _strip_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")  # noqa: TRY003
        return stripped

    @field_validator("allocation_time")
    @classmethod
    def _valid_allocation_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        match = _ALLOCATION_TIME.fullmatch(value)
        if match is None:
            raise ValueError("must use [days-]hours:minutes:seconds")  # noqa: TRY003
        if (
            int(match.group("minutes")) >= _MINUTES_PER_HOUR
            or int(match.group("seconds")) >= _MINUTES_PER_HOUR
        ):
            raise ValueError("minutes and seconds must be less than 60")  # noqa: TRY003
        if match.group("days") is not None and int(match.group("hours")) >= _HOURS_PER_DAY:
            raise ValueError("hours must be less than 24 when days are present")  # noqa: TRY003
        if not any(int(match.group(part) or 0) for part in ("days", "hours", "minutes", "seconds")):
            raise ValueError("must be greater than zero")  # noqa: TRY003
        return value

    @field_validator("remote_artifact_root")
    @classmethod
    def _absolute_remote_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
            raise ValueError("must be an absolute normalized POSIX path")  # noqa: TRY003
        return path.as_posix()


class ClusterProfilesFile(_StrictModel):
    """Versioned operator profile document loaded from TOML."""

    schema_version: Literal[1] = 1
    profiles: dict[str, SkyPilotProfile]

    @field_validator("profiles")
    @classmethod
    def _valid_profile_names(
        cls, profiles: dict[str, SkyPilotProfile]
    ) -> dict[str, SkyPilotProfile]:
        if not profiles:
            raise ValueError("must declare at least one profile")  # noqa: TRY003
        invalid = sorted(name for name in profiles if _PROFILE_NAME.fullmatch(name) is None)
        if invalid:
            raise ValueError(f"invalid profile name: {invalid[0]!r}")  # noqa: TRY003
        return profiles


class ResolvedSkyPilotResources(_StrictModel):
    """Validated effective resources passed to the SkyPilot runner."""

    profile_name: str
    infra: str
    nodes: Annotated[int, Field(gt=0)]
    accelerator_backend: Literal["cuda", "rocm", "trainium"]
    accelerator_type: str
    accelerators_per_node: Annotated[int, Field(gt=0)]
    cpus_per_node: Annotated[int, Field(gt=0)] | None = None
    memory_gb_per_node: Annotated[int, Field(gt=0)] | None = None
    exclusive: bool
    remote_runtime_image: str | None = None
    command_prefix: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    allocation_time: str | None = None
    remote_artifact_root: str


def load_cluster_profiles(path: Path) -> ClusterProfilesFile:
    """Load one strict cluster profile document with a path-specific error."""
    normalized = Path(path).expanduser()
    try:
        with normalized.open("rb") as profile_file:
            raw = tomllib.load(profile_file)
        return ClusterProfilesFile.model_validate(raw, strict=True)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ClusterProfileError(  # noqa: TRY003
            f"Could not load cluster profiles from {normalized}: {exc}"
        ) from exc


def resolve_profile(
    document: ClusterProfilesFile,
    profile_name: str,
    request: RunResourceRequest,
) -> ResolvedSkyPilotResources:
    """Resolve a portable request against one operator-owned profile."""
    try:
        profile = document.profiles[profile_name]
    except KeyError as exc:
        available = ", ".join(sorted(document.profiles))
        raise ClusterProfileError(  # noqa: TRY003
            f"Unknown cluster profile {profile_name!r}; available profiles: {available}"
        ) from exc
    if request.accelerator_backend != profile.accelerator_backend:
        raise ClusterProfileError(  # noqa: TRY003
            f"Profile {profile_name!r} provides {profile.accelerator_backend}, "
            f"but the run requests {request.accelerator_backend}"
        )
    if request.nodes > profile.max_nodes:
        raise ClusterProfileError(  # noqa: TRY003
            f"Profile {profile_name!r} permits at most {profile.max_nodes} nodes, "
            f"but the run requests {request.nodes}"
        )
    if request.accelerators_per_node > profile.accelerators_per_node:
        raise ClusterProfileError(  # noqa: TRY003
            f"Profile {profile_name!r} provides at most {profile.accelerators_per_node} "
            f"accelerators per node, but the run requests {request.accelerators_per_node}"
        )
    if (
        request.cpus_per_node is not None
        and profile.cpus_per_node is not None
        and request.cpus_per_node > profile.cpus_per_node
    ):
        raise ClusterProfileError(  # noqa: TRY003
            f"Profile {profile_name!r} provides at most {profile.cpus_per_node} CPUs per node, "
            f"but the run requests {request.cpus_per_node}"
        )
    return ResolvedSkyPilotResources(
        profile_name=profile_name,
        infra=profile.infra,
        nodes=request.nodes,
        accelerator_backend=profile.accelerator_backend,
        accelerator_type=profile.accelerator_type,
        accelerators_per_node=request.accelerators_per_node,
        cpus_per_node=request.cpus_per_node or profile.cpus_per_node,
        memory_gb_per_node=profile.memory_gb_per_node,
        exclusive=profile.exclusive,
        remote_runtime_image=profile.remote_runtime_image,
        command_prefix=tuple(profile.command_prefix or ()),
        allocation_time=profile.allocation_time,
        remote_artifact_root=profile.remote_artifact_root,
    )
