"""Typed operator configuration for remote Slurm execution."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PORT_PLACEHOLDER = "VIBESYS_DYNAMIC_PORT"


class SlurmConfigError(ValueError):
    """Invalid operator-owned Slurm configuration."""

    @classmethod
    def load_failed(cls, path: Path, error: Exception) -> SlurmConfigError:
        """Describe an unreadable or invalid configuration without leaking values."""
        if isinstance(error, ValidationError):
            fields = sorted(
                {".".join(str(part) for part in item["loc"]) for item in error.errors()}
            )
            detail = f"invalid settings: {', '.join(fields)}"
        else:
            detail = type(error).__name__
        return cls(f"Could not load Slurm config at {path}: {detail}")

    @classmethod
    def invalid_document(cls) -> SlurmConfigError:
        """Describe a document without a valid Slurm table."""
        return cls("Slurm config must contain a [slurm] table")

    @classmethod
    def invalid_invocation_id(cls) -> SlurmConfigError:
        """Describe an invocation identifier unsafe for a remote path."""
        return cls("Slurm invocation id must be a safe path component")


class SlurmService(BaseModel):
    """Service process and readiness probe launched inside an allocation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    command: tuple[str, ...] = Field(min_length=1)
    readiness_url: str = Field(min_length=1)
    startup_timeout_seconds: int = Field(gt=0)

    @field_validator("command", mode="before")
    @classmethod
    def _normalize_command_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("command")
    @classmethod
    def _valid_service_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part for part in value) or PORT_PLACEHOLDER not in value:
            message = "command must include VIBESYS_DYNAMIC_PORT as a complete argv item"
            raise ValueError(message)
        return value

    @field_validator("readiness_url")
    @classmethod
    def _valid_readiness_url(cls, value: str) -> str:
        if PORT_PLACEHOLDER not in value:
            message = "readiness_url must include VIBESYS_DYNAMIC_PORT"
            raise ValueError(message)
        return value


class SlurmSshTransport(BaseModel):
    """Built-in OpenSSH and rsync transport using the user's SSH configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["ssh"] = "ssh"
    host: Annotated[str, Field(min_length=1, max_length=255)]
    ssh_command: tuple[str, ...] = ("ssh",)
    rsync_command: tuple[str, ...] = ("rsync",)

    @field_validator("host")
    @classmethod
    def _safe_host(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", value) is None:
            message = "must be an SSH host or alias without options or whitespace"
            raise ValueError(message)
        return value

    @field_validator("ssh_command", "rsync_command", mode="before")
    @classmethod
    def _normalize_command_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("ssh_command", "rsync_command")
    @classmethod
    def _valid_command_array(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not argument for argument in value):
            message = "command arrays must contain at least one non-empty string"
            raise ValueError(message)
        return value


class SlurmConnectorTransport(BaseModel):
    """Advanced transport delegated to a versioned JSON connector executable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["connector"]
    command: tuple[str, ...] = Field(min_length=1)

    @field_validator("command", mode="before")
    @classmethod
    def _normalize_command_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("command")
    @classmethod
    def _valid_command_array(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument for argument in value):
            message = "command must contain non-empty strings"
            raise ValueError(message)
        return value


SlurmTransport = Annotated[
    SlurmSshTransport | SlurmConnectorTransport,
    Field(discriminator="kind"),
]


class SlurmConfig(BaseModel):
    """Cluster-neutral scheduler and transport settings.

    The normal transport invokes OpenSSH and rsync, which obtain credentials
    and site routing from the user's SSH agent and configuration. An advanced
    connector transport supports gateways that cannot expose SSH directly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Annotated[str, Field(min_length=1)]
    remote_workspace_root: Annotated[str, Field(min_length=1)]
    transport: SlurmTransport
    sbatch_command: tuple[str, ...] = ("sbatch",)
    sbatch_arguments: tuple[str, ...] = ()
    poll_interval_seconds: Annotated[float, Field(gt=0)] = 5.0
    job_timeout_seconds: Annotated[int, Field(gt=0)] = 3600
    transport_timeout_seconds: Annotated[int, Field(gt=0)] = 110
    evaluation_capacity: Annotated[int, Field(gt=0)] = 1

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", value) is None:
            message = "must be a safe name of at most 63 characters"
            raise ValueError(message)
        return value

    @field_validator(
        "sbatch_command",
        "sbatch_arguments",
        mode="before",
    )
    @classmethod
    def _normalize_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("sbatch_command", "sbatch_arguments")
    @classmethod
    def _validate_arrays(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not argument for argument in value):
            message = "command and argument arrays must contain non-empty strings"
            raise ValueError(message)
        return value

    @field_validator("remote_workspace_root")
    @classmethod
    def _normalized_remote_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or path.as_posix() != value
            or any(part in {".", ".."} for part in path.parts[1:])
        ):
            message = "remote_workspace_root must be an absolute normalized POSIX path"
            raise ValueError(message)
        return path.as_posix()


def load_slurm_config(path: Path) -> SlurmConfig:
    """Load scheduler and transport settings from an operator-owned file."""
    config_path = path.expanduser()
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SlurmConfigError.load_failed(config_path, exc) from exc
    if not isinstance(document.get("slurm"), dict):
        raise SlurmConfigError.invalid_document()
    try:
        return SlurmConfig.model_validate(document["slurm"], strict=True)
    except ValidationError as exc:
        raise SlurmConfigError.load_failed(config_path, exc) from exc
