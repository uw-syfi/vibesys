"""Typed operator configuration for remote Slurm execution."""

from __future__ import annotations

import math
import re
import shlex
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

PORT_PLACEHOLDER = "VIBESYS_DYNAMIC_PORT"
# Slurm's default KillWait, the time between the SIGTERM and the SIGKILL Slurm sends a job
# it ends. On that SIGTERM the job script's teardown sends SIGTERM to the processes it
# started and SIGKILL the stop grace later; the grace stays 10 s under KillWait, so the
# script's SIGKILL, not Slurm's, ends them and the script still sees them go.
_SLURM_DEFAULT_KILL_WAIT_SECONDS = 30
_SERVICE_STOP_GRACE_MAX_SECONDS = _SLURM_DEFAULT_KILL_WAIT_SECONDS - 10
# After SIGKILL, how long the job script waits for the processes it started to exit. A CUDA
# process with a large GPU allocation can take tens of seconds; past Slurm's default
# UnkillableStepTimeout (60 s) a process Slurm finds left in the step drains the node.
_PROCESS_EXIT_WAIT_DEFAULT_SECONDS = 120
_PROCESS_EXIT_WAIT_MAX_SECONDS = 1800
# ``sbatch`` time limit spellings: minutes, minutes:seconds, hours:minutes:seconds,
# days-hours, days-hours:minutes, days-hours:minutes:seconds.
_TIME_LIMIT = re.compile(
    r"(?:(?P<days>\d+)-(?P<dh>\d+)(?::(?P<dm>\d+)(?::(?P<ds>\d+))?)?)"
    r"|(?:(?P<a>\d+)(?::(?P<b>\d+)(?::(?P<c>\d+))?)?)"
)
_NO_TIME_LIMIT = frozenset({"INFINITE", "UNLIMITED"})
_PORT_EXPANSION = '"${PORT}"'


def shell_join_with_port(arguments: Sequence[str]) -> str:
    """Quote `arguments` as one shell command line, expanding each port placeholder.

    Every byte of every argument reaches the command unchanged, except that each
    occurrence of `PORT_PLACEHOLDER` becomes the value of the job's `PORT`
    variable. Only the placeholder is expanded: the text around it stays quoted,
    so an argument that is itself a shell script (`bash -c ...`) keeps its own
    `$?`, quotes, and variables for the shell that runs it.
    """
    return " ".join(
        _PORT_EXPANSION.join(
            shlex.quote(part) if part else "" for part in argument.split(PORT_PLACEHOLDER)
        )
        if PORT_PLACEHOLDER in argument
        else shlex.quote(argument)
        for argument in arguments
    )


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


_NON_INTERACTIVE_SSH_OPTIONS = ("-o", "BatchMode=yes")


class SlurmSshTransport(BaseModel):
    """Built-in OpenSSH and rsync transport using the user's SSH configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["ssh"] = "ssh"
    host: Annotated[str, Field(min_length=1, max_length=255)]
    ssh_command: tuple[str, ...] = ("ssh",)
    rsync_command: tuple[str, ...] = ("rsync",)

    @property
    def ssh_argv(self) -> tuple[str, ...]:
        """``ssh_command`` with every prompt off: the program each SSH call runs.

        A run has no terminal, so an ssh that would ask for a password,
        passphrase, host-key confirmation or second factor would submit empty
        answers to the server (risking a lockout) or open an askpass window.
        ``BatchMode=yes`` makes it fail instead. ssh keeps the first value of an
        option, so an operator's own ``-o BatchMode=...`` in ``ssh_command`` wins.
        """
        return (*self.ssh_command, *_NON_INTERACTIVE_SSH_OPTIONS)

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


class SlurmLocalTransport(BaseModel):
    """Transport for a host that is itself a Slurm submit node.

    Scheduler and filesystem commands run through ``shell_command`` on this
    host, and transfers are local ``rsync`` copies. ``remote_workspace_root``
    must therefore be a directory this host shares with the compute nodes.
    ``shell_command`` receives the command line as its last argument, so a
    wrapper (for example one that enters a login container) may precede the
    shell.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["local"]
    shell_command: tuple[str, ...] = ("bash", "-c")
    rsync_command: tuple[str, ...] = ("rsync",)

    @field_validator("shell_command", "rsync_command", mode="before")
    @classmethod
    def _normalize_command_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("shell_command", "rsync_command")
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
    SlurmSshTransport | SlurmLocalTransport | SlurmConnectorTransport,
    Field(discriminator="kind"),
]


class SlurmConfig(BaseModel):
    """Cluster-neutral scheduler and transport settings.

    The normal transport invokes OpenSSH and rsync, which obtain credentials
    and site routing from the user's SSH agent and configuration. The local
    transport serves a host that is itself a submit node. An advanced
    connector transport supports gateways that cannot expose SSH directly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Annotated[str, Field(min_length=1)]
    remote_workspace_root: Annotated[str, Field(min_length=1)]
    transport: SlurmTransport
    sbatch_command: tuple[str, ...] = ("sbatch",)
    sbatch_arguments: tuple[str, ...] = ()
    # Replaces the derived 1-CPU request for minimal jobs (the startup probe) on sites
    # whose partitions require a GPU or other resources; see ``minimal_job``.
    probe_sbatch_arguments: tuple[str, ...] | None = None
    # Declares that each measurement job gets its GPUs and CPU cores to itself through the
    # scheduler (for example ``--gres=gpu:4 -c 64``), though it may share the host with other
    # jobs. A bare ``--exclusive`` in ``sbatch_arguments`` implies it. It lets an official
    # measurement reuse an earlier trusted one of the same input, so leave it false unless true.
    dedicated_measurement_resources: bool = False
    poll_interval_seconds: Annotated[float, Field(gt=0)] = 5.0
    # Bounds a job's run, from when it is first seen running; queue wait is not counted.
    job_timeout_seconds: Annotated[int, Field(gt=0)] = 3600
    # Bounds a job's wait in the queue, per scheduler attempt; None leaves it unbounded.
    queue_timeout_seconds: Annotated[int, Field(gt=0)] | None = None
    transport_timeout_seconds: Annotated[int, Field(gt=0)] = 110
    evaluation_capacity: Annotated[int, Field(gt=0)] = 1
    # When a job ends, the processes it started (its service's and its command's sessions)
    # get SIGTERM, then SIGKILL this long later.
    service_stop_grace_seconds: Annotated[int, Field(gt=0, le=_SERVICE_STOP_GRACE_MAX_SECONDS)] = (
        _SERVICE_STOP_GRACE_MAX_SECONDS
    )
    # After that SIGKILL, the longest the job script waits for those processes to exit. One
    # still alive then is named on the job's stderr and fails the job (exit code 71). The
    # stop grace plus this wait must be shorter than an ``sbatch`` time limit (checked
    # below), and the default shrinks to fit one; the run deadline ``job_timeout_seconds``
    # should leave the same room after the command.
    process_exit_wait_seconds: Annotated[
        int, Field(gt=0, le=_PROCESS_EXIT_WAIT_MAX_SECONDS, validate_default=True)
    ] = _PROCESS_EXIT_WAIT_DEFAULT_SECONDS

    @property
    def dedicates_measurement_resources(self) -> bool:
        """Whether each measurement job gets its GPUs and CPU cores to itself.

        True when the operator declares ``dedicated_measurement_resources``, or when the last
        sharing option in ``sbatch_arguments`` is a bare ``--exclusive``.

        ``--exclusive=user``, ``=mcs`` or ``=topo`` share nodes with other jobs (the agent's
        own GPU jobs run as the same user), and ``--oversubscribe`` (``-s``, or its older spelling ``--share``) shares them too;
        sbatch takes the last of these options it reads.
        """
        if self.dedicated_measurement_resources:
            return True
        sharing = [
            argument
            for argument in self.sbatch_arguments
            if argument in {"--oversubscribe", "--share", "-s"}
            or argument.startswith("--exclusive")
        ]
        return bool(sharing) and sharing[-1] == "--exclusive"

    @model_validator(mode="before")
    @classmethod
    def _default_exit_wait_fits_the_time_limit(cls, data: object) -> object:
        """Default the exit wait to 120 s, or to what an sbatch time limit leaves of it."""
        if not isinstance(data, dict) or "process_exit_wait_seconds" in data:
            return data
        arguments = data.get("sbatch_arguments", ())
        grace = data.get("service_stop_grace_seconds", _SERVICE_STOP_GRACE_MAX_SECONDS)
        if (
            not isinstance(arguments, list | tuple)
            or not all(isinstance(argument, str) for argument in arguments)
            or type(grace) is not int
        ):
            return data  # the fields' own validation reports these
        try:
            limit = _sbatch_time_limit_seconds(arguments)
        except ValueError:
            return data  # reported by sbatch_arguments' validation
        if limit is None:
            return data
        wait = max(1, min(_PROCESS_EXIT_WAIT_DEFAULT_SECONDS, limit - grace - 1))
        return {**data, "process_exit_wait_seconds": wait}

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
        "probe_sbatch_arguments",
        mode="before",
    )
    @classmethod
    def _normalize_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("sbatch_command", "sbatch_arguments", "probe_sbatch_arguments")
    @classmethod
    def _validate_arrays[T: tuple[str, ...] | None](cls, value: T) -> T:
        if value is not None and any(not argument for argument in value):
            message = "command and argument arrays must contain non-empty strings"
            raise ValueError(message)
        return value

    @field_validator("sbatch_arguments", "probe_sbatch_arguments")
    @classmethod
    def _no_requeue[T: tuple[str, ...] | None](cls, value: T) -> T:
        if value is not None and "--requeue" in value:
            message = (
                "--requeue is not allowed: VibeSys retries jobs itself and submits --no-requeue"
            )
            raise ValueError(message)
        return value

    @field_validator("sbatch_arguments", "probe_sbatch_arguments")
    @classmethod
    def _readable_time_limit[T: tuple[str, ...] | None](cls, value: T) -> T:
        if value is not None:
            _sbatch_time_limit_seconds(value)  # raises on a time limit sbatch would not read
        return value

    @field_validator("process_exit_wait_seconds")
    @classmethod
    def _teardown_fits_the_time_limit(cls, value: int, info: ValidationInfo) -> int:
        grace = info.data.get("service_stop_grace_seconds")
        if not isinstance(grace, int):
            return value  # that field failed its own validation and is reported there
        for field in ("sbatch_arguments", "probe_sbatch_arguments"):
            arguments = info.data.get(field)
            if not isinstance(arguments, tuple):
                continue  # unset, or failed its own validation and is reported there
            limit = _sbatch_time_limit_seconds(arguments)
            if limit is not None and grace + value >= limit:
                message = (
                    f"service_stop_grace_seconds + process_exit_wait_seconds "
                    f"({grace + value} s) must be shorter than the {field} time limit "
                    f"({limit} s): the job script waits that long for the processes it "
                    "started to exit"
                )
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


# The ``sbatch`` options a minimal job keeps from ``sbatch_arguments``: where and as whom
# it runs. Every other option is dropped with any value it takes: GPUs, CPUs, memory,
# nodes, time, holds, dependencies, arrays, node lists and anything unknown, since an
# option this list does not name could request resources or delay the start. Only
# sbatch's canonical spellings are recognized; an abbreviation (``--part``) is dropped.
_PLACEMENT_LONG_OPTIONS = frozenset(
    {
        "--account",
        "--clusters",
        "--comment",
        "--constraint",
        "--exclude",
        "--partition",
        "--qos",
        "--reservation",
        "--wckey",
    }
)
_PLACEMENT_SHORT_OPTIONS = frozenset({"-A", "-C", "-M", "-p", "-q", "-x"})
_MINIMAL_JOB_CPUS = 1
_MINIMAL_JOB_MEMORY = "1G"
# Seconds the minimal job leaves for its own command, besides its teardown waits.
_MINIMAL_JOB_COMMAND_SECONDS = 60


class SlurmJobResources(StrEnum):
    """Which resource request a job is submitted with.

    ``CONFIGURED`` uses the operator's ``sbatch_arguments`` as written: the shape an
    evaluation needs. ``MINIMAL`` keeps their partition, account and other placement
    options but asks for one CPU, no GPU and a few minutes
    (:func:`minimal_job`), so a job that only touches
    files is not queued behind GPU capacity.
    """

    CONFIGURED = "configured"
    MINIMAL = "minimal"


@dataclass(frozen=True)
class MinimalJob:
    """How a job that only touches a few files is submitted on this cluster.

    ``arguments`` replace ``sbatch_arguments`` for it; ``time_limit_seconds`` is the
    time limit they set (None: none set); ``description`` names the request for
    operator-facing messages.
    """

    arguments: tuple[str, ...]
    time_limit_seconds: int | None
    description: str


def _placement_options(arguments: Sequence[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        if argument in _PLACEMENT_LONG_OPTIONS or argument in _PLACEMENT_SHORT_OPTIONS:
            kept.extend(arguments[index - 1 : index + 1])
            index += 1
        elif argument.split("=", 1)[0] in _PLACEMENT_LONG_OPTIONS or (
            argument[:2] in _PLACEMENT_SHORT_OPTIONS and not argument.startswith("--")
        ):
            kept.append(argument)
    return kept


def minimal_job(config: SlurmConfig) -> MinimalJob:
    """The submission of a tiny job: ``probe_sbatch_arguments``, or 1 CPU and no GPU.

    Without the override, the operator's placement options (partition, account, QoS,
    constraint, reservation, clusters, excluded nodes, wckey, comment) are kept from
    ``sbatch_arguments`` and everything else is replaced by one node, one task, one
    CPU, 1 GiB and the shortest whole-minute time limit that still fits the job
    script's teardown (``service_stop_grace_seconds + process_exit_wait_seconds``)
    plus a minute for the command. A site whose partitions require a GPU sets
    ``probe_sbatch_arguments`` instead.
    """
    if config.probe_sbatch_arguments is not None:
        arguments = config.probe_sbatch_arguments
        return MinimalJob(
            arguments=arguments,
            time_limit_seconds=_sbatch_time_limit_seconds(arguments),
            description="the resources `probe_sbatch_arguments` requests",
        )
    teardown = config.service_stop_grace_seconds + config.process_exit_wait_seconds
    minutes = math.ceil((teardown + _MINIMAL_JOB_COMMAND_SECONDS) / 60)
    return MinimalJob(
        arguments=(
            *_placement_options(config.sbatch_arguments),
            "--nodes=1",
            "--ntasks=1",
            f"--cpus-per-task={_MINIMAL_JOB_CPUS}",
            f"--mem={_MINIMAL_JOB_MEMORY}",
            f"--time={minutes}",
        ),
        time_limit_seconds=minutes * 60,
        description=(
            f"{_MINIMAL_JOB_CPUS} CPU, {_MINIMAL_JOB_MEMORY} of memory and {minutes} min "
            "without GPUs, in the partition `sbatch_arguments` names"
        ),
    )


def _sbatch_time_limit_seconds(arguments: Sequence[str]) -> int | None:
    """The time limit ``--time``/``-t`` in ``arguments`` sets, in seconds; None for no limit.

    The last occurrence wins, as in ``sbatch``. A value ``sbatch`` would not read raises
    ``ValueError``.
    """
    value: str | None = None
    for index, argument in enumerate(arguments):
        if argument in {"--time", "-t"}:
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        elif argument.startswith("--time="):
            value = argument.removeprefix("--time=")
        elif argument.startswith("-t") and not argument.startswith("--"):
            value = argument.removeprefix("-t")
    if value is None or value.upper() in _NO_TIME_LIMIT:
        return None
    match = _TIME_LIMIT.fullmatch(value)
    if match is None:
        message = f"{value!r} is not an sbatch time limit"
        raise ValueError(message)
    if match["days"] is not None:
        days, hours = int(match["days"]), int(match["dh"])
        minutes, seconds = int(match["dm"] or 0), int(match["ds"] or 0)
        limit = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    elif match["c"] is not None:
        limit = (int(match["a"]) * 60 + int(match["b"])) * 60 + int(match["c"])
    else:
        limit = int(match["a"]) * 60 + int(match["b"] or 0)
    return limit or None  # a zero time limit is no limit


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
