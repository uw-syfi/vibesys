"""Run one GPU command through a local Slurm controller with ``srun``.

The ``slurm-gpu`` run environment keeps the agent on the submit host and sends
only GPU processes to Slurm. This module owns the operator configuration, the
partition choice, and the blocking ``srun`` launch shared by the host broker
(agent commands) and the trusted gates (framework commands).
"""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import subprocess
import threading
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vs_sim.api import SubprocessProbe

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_sim.api import CommandProbe, Event

_UNLIMITED = "unlimited"
_WINDOWS_TIMEOUT_SECONDS = 30
_CANCEL_GRACE_SECONDS = 30
_SCANCEL_TIMEOUT_SECONDS = 60
_READ_BYTES = 65_536
_MAX_QUEUE_MINUTES = 60 * 24 * 365


class SlurmGpuConfigError(ValueError):
    """Invalid operator-owned ``slurm-gpu`` configuration."""

    @classmethod
    def load_failed(cls, path: Path, error: Exception) -> SlurmGpuConfigError:
        """Describe invalid settings by field without echoing their values."""
        if isinstance(error, ValidationError):
            fields = sorted(
                {".".join(str(part) for part in item["loc"]) for item in error.errors()}
            )
            detail = f"invalid settings: {', '.join(fields)}"
        else:
            detail = type(error).__name__
        return cls(f"Could not load Slurm GPU config at {path}: {detail}")


class SlurmGpuRequestError(ValueError):
    """A GPU command request outside the operator's limits."""


class AgentGpuConfig(BaseModel):
    """Operator limits and commands for the agent's GPU jobs, submitted from this host.

    ``partitions`` is a preference order. With ``windows_command`` set, the
    first partition whose reported window starts the job now is chosen;
    otherwise, and when nothing starts now, the first partition whose time
    limit fits the request is used and the job waits in its queue.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    partitions: tuple[str, ...] = Field(min_length=1)
    max_gpus: Annotated[int, Field(gt=0)]
    max_time_minutes: Annotated[int, Field(gt=0)]
    default_time_minutes: Annotated[int, Field(gt=0)] = 30
    srun_command: tuple[str, ...] = ("srun",)
    scancel_command: tuple[str, ...] = ("scancel",)
    windows_command: tuple[str, ...] | None = None
    srun_arguments: tuple[str, ...] = ()
    job_name_prefix: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")] = (
        "vibesys-gpu"
    )

    @field_validator(
        "partitions",
        "srun_command",
        "scancel_command",
        "windows_command",
        "srun_arguments",
        mode="before",
    )
    @classmethod
    def _normalize_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("partitions", "srun_command", "scancel_command", "srun_arguments")
    @classmethod
    def _non_empty_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            message = "arrays must contain non-empty strings"
            raise ValueError(message)
        return value

    @field_validator("windows_command")
    @classmethod
    def _valid_windows_command(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (not value or any(not item for item in value)):
            message = "windows_command must contain non-empty strings"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _default_within_limit(self) -> Self:
        if self.default_time_minutes > self.max_time_minutes:
            message = "default_time_minutes exceeds max_time_minutes"
            raise ValueError(message)
        return self

    def request(self, gpus: int | None, time_minutes: int | None) -> GpuJobRequest:
        """Fill defaults and reject a request above the operator limits."""
        request = GpuJobRequest(
            gpus=1 if gpus is None else gpus,
            time_minutes=self.default_time_minutes if time_minutes is None else time_minutes,
        )
        if request.gpus > self.max_gpus:
            message = f"--gpus {request.gpus} exceeds the operator limit of {self.max_gpus}"
            raise SlurmGpuRequestError(message)
        if request.time_minutes > self.max_time_minutes:
            message = (
                f"--time {request.time_minutes} exceeds the operator limit of "
                f"{self.max_time_minutes} minutes"
            )
            raise SlurmGpuRequestError(message)
        return request


class SlurmGpuConfig(AgentGpuConfig):
    """The ``slurm-gpu`` environment's table: the agent's limits plus its gates' size.

    The ``slurm`` environment sizes its gate jobs by sbatch arguments, so these
    two fields exist only here.
    """

    gate_gpus: Annotated[int, Field(gt=0)] = 1
    gate_time_minutes: Annotated[int, Field(gt=0)] = 60

    @model_validator(mode="after")
    def _gate_within_limits(self) -> Self:
        if self.gate_gpus > self.max_gpus:
            message = "gate_gpus exceeds max_gpus"
            raise ValueError(message)
        if self.gate_time_minutes > self.max_time_minutes:
            message = "gate_time_minutes exceeds max_time_minutes"
            raise ValueError(message)
        return self


class GpuJobRequest(BaseModel):
    """GPU count and wall-clock limit of one command."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gpus: Annotated[int, Field(gt=0)]
    time_minutes: Annotated[int, Field(gt=0)]


@dataclass(frozen=True, slots=True)
class GpuCommand:
    """Argv, working directory, and environment of one GPU command."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]


class _SlurmGpuDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    slurm_gpu: SlurmGpuConfig


def load_slurm_gpu_config(path: Path) -> SlurmGpuConfig:
    """Load the ``[slurm_gpu]`` table from an operator-owned TOML file."""
    config_path = path.expanduser()
    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
        return _SlurmGpuDocument.model_validate(document, strict=True).slurm_gpu
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise SlurmGpuConfigError.load_failed(config_path, exc) from exc


def _minutes(value: str) -> float:
    """Parse a Slurm ``[D-]HH:MM:SS`` limit, or ``unlimited``, to minutes."""
    if value == _UNLIMITED:
        return math.inf
    days, _, clock = value.rpartition("-")
    hours, minutes, seconds = (int(part) for part in clock.split(":"))
    return (int(days or 0) * 24 + hours) * 60 + minutes + seconds / 60


def _starts_now(partition: Mapping[str, object], request: GpuJobRequest) -> bool | None:
    """Return whether the partition's window starts *request* now, or ``None`` if it cannot fit."""
    if request.time_minutes > _minutes(str(partition["partition_max_time"])):
        return None
    groups = partition.get("groups")
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not group["gpus_min"] <= request.gpus <= group["gpus_max"]:
            continue
        for option in group["options"]:
            if request.time_minutes <= _minutes(str(option["max_time"])):
                return option["start"] == "now"
    return False


def choose_partition(
    config: AgentGpuConfig,
    request: GpuJobRequest,
    windows: Mapping[str, object] | None,
) -> str:
    """Prefer a partition that starts *request* now, else the first whose limit fits it.

    *windows* is the scheduler's start-time report (the ``slurm-windows --json``
    shape: ``partitions[].partition_max_time`` and ``groups[].options[]`` with
    ``max_time`` and ``start``). Without it the first configured partition is
    used. Malformed reports are treated as absent rather than guessed at.
    """
    if windows is None:
        return config.partitions[0]
    try:
        entries = cast("list[Mapping[str, object]]", windows["partitions"])
        reported = {str(entry["partition"]): entry for entry in entries}
        fits = []
        for name in config.partitions:
            if name not in reported:
                continue
            starts = _starts_now(reported[name], request)
            if starts:
                return name
            if starts is not None:
                fits.append(name)
    except (KeyError, TypeError, ValueError):
        return config.partitions[0]
    return fits[0] if fits else config.partitions[0]


def read_windows(
    config: AgentGpuConfig, probe: CommandProbe | None = None
) -> Mapping[str, object] | None:
    """Run the operator's window report command, or return ``None`` if unavailable."""
    if config.windows_command is None:
        return None
    completed = (probe or SubprocessProbe()).run(
        config.windows_command, timeout_seconds=_WINDOWS_TIMEOUT_SECONDS
    )
    if completed is None or completed.returncode != 0:
        return None
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None


def srun_argv(
    config: AgentGpuConfig,
    request: GpuJobRequest,
    *,
    partition: str,
    job_name: str,
    command: Sequence[str],
) -> tuple[str, ...]:
    """Build one single-task ``srun`` that allocates *request* and runs *command*."""
    return (
        *config.srun_command,
        f"--partition={partition}",
        f"--gres=gpu:{request.gpus}",
        f"--time={request.time_minutes}",
        f"--job-name={job_name}",
        "--ntasks=1",
        "--unbuffered",
        *config.srun_arguments,
        "--",
        *command,
    )


def new_job_name(config: AgentGpuConfig) -> str:
    """Return a job name unique enough to cancel exactly this command's job."""
    return f"{config.job_name_prefix}-{secrets.token_hex(6)}"


class GpuLauncher(Protocol):
    """Runs one GPU command to completion; :class:`SlurmGpuLauncher` is the real one."""

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        """Run *command*, stream its output to *write*, and stop when *cancel* is set."""
        ...


class SlurmGpuLauncher:
    """Run commands with ``srun`` and cancel their jobs when asked to stop.

    *popen* starts processes and *probe* runs the scheduler report and ``scancel``;
    tests inject both to drive the lifecycle with a fake ``srun``.
    """

    def __init__(
        self,
        config: AgentGpuConfig,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        probe: CommandProbe | None = None,
    ) -> None:
        """Bind the operator configuration and the process boundary."""
        self._config = config
        self._popen = popen
        self._probe: CommandProbe = probe or SubprocessProbe()

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        """Run *command* in a new allocation, streaming merged output to *write*.

        Returns the command's exit status as ``srun`` reports it. Setting
        *cancel* (from any thread) cancels the job by its unique name, then
        stops ``srun``; the call still returns only after ``srun`` exits.
        """
        partition = choose_partition(self._config, request, read_windows(self._config, self._probe))
        job_name = new_job_name(self._config)
        argv = srun_argv(
            self._config, request, partition=partition, job_name=job_name, command=command.argv
        )
        write(
            f"[vibesys-gpu] srun partition={partition} gpus={request.gpus} "
            f"time={request.time_minutes}m job-name={job_name}\n".encode()
        )
        process = self._popen(
            argv,
            cwd=command.cwd,
            env=dict(command.env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Keep terminal signals aimed at the caller away from srun, whose
            # job this launcher cancels explicitly.
            process_group=0,
        )
        finished = threading.Event()
        watcher = threading.Thread(
            target=self._cancel_when_asked,
            args=(process, job_name, cancel, finished),
            name="vibesys-gpu-cancel",
            daemon=True,
        )
        watcher.start()
        try:
            stdout = process.stdout
            if stdout is not None:
                while chunk := os.read(stdout.fileno(), _READ_BYTES):
                    write(chunk)
            return process.wait()
        finally:
            finished.set()
            watcher.join()

    def _cancel_when_asked(
        self,
        process: subprocess.Popen[bytes],
        job_name: str,
        cancel: Event,
        finished: threading.Event,
    ) -> None:
        while not finished.is_set():
            if cancel.wait(0.2):
                break
        else:
            return
        self.cancel(job_name)
        process.terminate()
        try:
            process.wait(_CANCEL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()

    def cancel(self, job_name: str) -> None:
        """Cancel this user's job named *job_name*, pending or running."""
        self._probe.run(
            (*self._config.scancel_command, f"--name={job_name}", "--me"),
            timeout_seconds=_SCANCEL_TIMEOUT_SECONDS,
        )


def resolve_executable(command: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve a command's program on this host's PATH, keeping its arguments."""
    resolved = shutil.which(command[0])
    return command if resolved is None else (resolved, *command[1:])


__all__ = [
    "AgentGpuConfig",
    "GpuCommand",
    "GpuJobRequest",
    "GpuLauncher",
    "SlurmGpuConfig",
    "SlurmGpuConfigError",
    "SlurmGpuLauncher",
    "SlurmGpuRequestError",
    "choose_partition",
    "load_slurm_gpu_config",
    "read_windows",
    "resolve_executable",
    "srun_argv",
]
