"""Run one agent GPU command through a local Slurm controller with ``srun``.

The ``[vibesys.agent_gpu]`` capability keeps the agent on the submit host and
sends only its GPU processes to Slurm. This module owns the operator limits,
the partition choice, and the blocking ``srun`` launch the host broker uses.
"""

from __future__ import annotations

import math
import secrets
import shlex
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Protocol, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from vs_decode.api import decode_json

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vs_sim.api import CommandProbe, Event, PipedProcess, PipedProcessLauncher, Threads

_UNLIMITED = "unlimited"
_WINDOWS_TIMEOUT_SECONDS = 30
_CANCEL_GRACE_SECONDS = 30
_SCANCEL_TIMEOUT_SECONDS = 60
#: ``scancel`` runs again while its outcome is unknown: it could not start, timed
#: out, or exited nonzero (an ``ssh`` wrapper that lost its connection exits 255).
_SCANCEL_ATTEMPTS = 3
_SQUEUE_TIMEOUT_SECONDS = 30
#: After a cancel, ``squeue`` is asked this many times, this far apart, whether
#: the job is gone; a completing job can stay listed for a few seconds.
_CONFIRM_ATTEMPTS = 6
_CONFIRM_INTERVAL_SECONDS = 5
#: How long ``run`` waits for the cancel worker once ``srun`` exited. It bounds
#: every ``scancel`` attempt; the worker returns at once in every other state.
_WATCHER_JOIN_SECONDS = _SCANCEL_ATTEMPTS * _SCANCEL_TIMEOUT_SECONDS + _CANCEL_GRACE_SECONDS
_READ_CHARACTERS = 65_536
_MAX_QUEUE_MINUTES = 60 * 24 * 365


def _is_normalized_absolute(path: str) -> bool:
    """Whether *path* is absolute, not the root, with no empty, ``.`` or ``..`` segment."""
    segments = path.split("/")
    return (
        path.startswith("/")
        and len(segments) > 1
        and all(segment not in {"", ".", ".."} for segment in segments[1:])
    )


class AgentGpuRequestError(ValueError):
    """A GPU command request outside the operator's limits."""


class AgentGpuConfig(BaseModel):
    """Operator limits and commands for the agent's GPU jobs, submitted from this host.

    ``partitions`` is a preference order. With ``windows_command`` set, the
    first partition whose reported window starts the job now is chosen;
    otherwise, and when nothing starts now, the first partition whose time
    limit fits the request is used and the job waits in its queue.

    A job sees its workspace (writable), the system directories, and
    ``read_only_paths``: extra absolute host paths, such as the operator's
    Python environment, that it may read and never write. Without them an
    interpreter or dependency outside the workspace is invisible to the job.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    partitions: tuple[str, ...] = Field(min_length=1)
    max_gpus: Annotated[int, Field(gt=0)]
    max_time_minutes: Annotated[int, Field(gt=0)]
    default_time_minutes: Annotated[int, Field(gt=0)] = 30
    windows_command: tuple[str, ...] | None = None
    srun_arguments: tuple[str, ...] = ()
    read_only_paths: tuple[str, ...] = ()
    job_name_prefix: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")] = (
        "vibesys-gpu"
    )

    @field_validator(
        "partitions",
        "windows_command",
        "srun_arguments",
        "read_only_paths",
        mode="before",
    )
    @classmethod
    def _normalize_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("partitions", "srun_arguments")
    @classmethod
    def _non_empty_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            message = "arrays must contain non-empty strings"
            raise ValueError(message)
        return value

    @field_validator("read_only_paths")
    @classmethod
    def _absolute_normalized_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not _is_normalized_absolute(item):
                message = (
                    f"read_only_paths entry {item!r} must be an absolute, normalized path "
                    "other than the root (no '..', '.', repeated or trailing slashes)"
                )
                raise ValueError(message)
        if len(set(value)) != len(value):
            message = "read_only_paths must not repeat a path"
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
            raise AgentGpuRequestError(message)
        if request.time_minutes > self.max_time_minutes:
            message = (
                f"--time {request.time_minutes} exceeds the operator limit of "
                f"{self.max_time_minutes} minutes"
            )
            raise AgentGpuRequestError(message)
        return request


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


def read_windows(config: AgentGpuConfig, probe: CommandProbe) -> Mapping[str, object] | None:
    """Run the operator's window report command, or return ``None`` if unavailable."""
    if config.windows_command is None:
        return None
    completed = probe.run(config.windows_command, timeout_seconds=_WINDOWS_TIMEOUT_SECONDS)
    if completed is None or completed.returncode != 0:
        return None
    report = decode_json(
        completed.stdout, _ANY_JSON, source="agent GPU window report", max_bytes=_MAX_REPORT_BYTES
    )
    return report if isinstance(report, dict) else None


_ANY_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MAX_REPORT_BYTES = 1024 * 1024
"""The largest window report read from the operator's command."""
"""Any JSON value; pydantic's parser bounds nesting, so deep input is invalid, not a crash."""


def srun_argv(
    config: AgentGpuConfig,
    request: GpuJobRequest,
    *,
    partition: str,
    job_name: str,
    command: Sequence[str],
) -> tuple[str, ...]:
    """Build one single-task ``srun`` that allocates *request* and runs *command*.

    The argv is a command line for the Slurm shell (the local transport's
    ``shell_command``), which finds ``srun`` where the operator's Slurm is.
    """
    return (
        "srun",
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
    """Runs one GPU command to completion; :class:`AgentGpuLauncher` is the real one."""

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


class AgentGpuLauncher:
    """Run commands with ``srun`` and cancel their jobs when asked to stop.

    ``srun`` and ``scancel`` run through *shell_command*, the local transport's
    shell (a login-container wrapper may precede ``bash -c``), so the operator
    names how to reach the Slurm binaries in one place. *processes* owns the
    local process group, *probe* runs the scheduler report and ``scancel``, and
    *threads* runs the output pump and bounds the waits.
    """

    def __init__(
        self,
        config: AgentGpuConfig,
        shell_command: Sequence[str],
        *,
        probe: CommandProbe,
        processes: PipedProcessLauncher,
        threads: Threads,
    ) -> None:
        """Bind the operator configuration and the process boundary."""
        self._config = config
        self._shell = tuple(shell_command)
        self._probe = probe
        self._processes = processes
        self._threads = threads

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
        stops ``srun``; the call still returns only after ``srun`` exits. Stopping
        a ``docker exec`` or ``ssh`` wrapper does not stop the ``srun`` behind it,
        so a cancelled job is then looked up in the queue, and one still listed is
        reported on *write* as unconfirmed.
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
        process = self._processes.start(
            (*self._shell, shlex.join(argv)),
            cwd=command.cwd,
            env=dict(command.env),
            merge_stderr=True,
        )
        finished = self._threads.event()
        cancelled = self._threads.event()
        watcher = self._threads.spawn(
            lambda: self._cancel_when_asked(process, job_name, cancel, finished, cancelled),
            name="vibesys-gpu-cancel",
            daemon=True,
        )
        try:
            while chunk := process.stdout.read(_READ_CHARACTERS):
                write(chunk.encode())
            status = process.wait(None)
        finally:
            finished.set()
            watcher.join(_WATCHER_JOIN_SECONDS)
        if cancelled.is_set() and not self.job_gone(job_name):
            write(
                f"[vibesys-gpu] cancel unconfirmed: job {job_name} is still in the "
                "Slurm queue and may keep its allocation until its time limit\n".encode()
            )
        return status

    def _cancel_when_asked(
        self,
        process: PipedProcess,
        job_name: str,
        cancel: Event,
        finished: Event,
        cancelled: Event,
    ) -> None:
        while not finished.is_set():
            if cancel.wait(0.2):
                break
        else:
            return
        if finished.is_set():
            return
        cancelled.set()
        self.cancel(job_name)
        if finished.is_set():
            return
        process.terminate_group()
        if not finished.wait(_CANCEL_GRACE_SECONDS):
            process.kill_group()

    def cancel(self, job_name: str) -> bool:
        """Cancel this user's job named *job_name*, pending or running.

        Returns whether ``scancel`` reported success. Slurm answers success for a
        name that matches no job, so success is not proof the job is gone; see
        :meth:`job_gone`.
        """
        argv = (*self._shell, shlex.join(("scancel", f"--name={job_name}", "--me")))
        for _ in range(_SCANCEL_ATTEMPTS):
            result = self._probe.run(argv, timeout_seconds=_SCANCEL_TIMEOUT_SECONDS)
            if result is not None and result.returncode == 0:
                return True
        return False

    def job_gone(self, job_name: str) -> bool:
        """Whether ``squeue`` reports no job of this user named *job_name*, within a bound.

        An unanswered or failed ``squeue`` is not an answer: the job counts as
        present, so a cancel is never reported confirmed on no evidence.
        """
        argv = (
            *self._shell,
            shlex.join(("squeue", f"--name={job_name}", "--me", "--noheader", "--format=%i")),
        )
        for attempt in range(_CONFIRM_ATTEMPTS):
            if attempt:
                self._threads.sleep(_CONFIRM_INTERVAL_SECONDS)
            result = self._probe.run(argv, timeout_seconds=_SQUEUE_TIMEOUT_SECONDS)
            if result is not None and result.returncode == 0 and not result.stdout.strip():
                return True
        return False


def resolve_executable(command: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve a command's program on this host's PATH, keeping its arguments."""
    resolved = shutil.which(command[0])
    return command if resolved is None else (resolved, *command[1:])


__all__ = [
    "AgentGpuConfig",
    "AgentGpuLauncher",
    "AgentGpuRequestError",
    "GpuCommand",
    "GpuJobRequest",
    "GpuLauncher",
    "choose_partition",
    "read_windows",
    "resolve_executable",
    "srun_argv",
]
