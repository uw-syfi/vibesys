"""Run one agent GPU command through a local Slurm controller with ``srun``.

The ``[vibesys.agent_gpu]`` capability keeps the agent on the submit host and
sends only its GPU processes to Slurm. This module owns the operator limits,
the partition choice, and the blocking ``srun`` launch the host broker uses.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import shlex
import shutil
from contextlib import suppress
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

    from vs_sim.api import (
        CommandProbe,
        Event,
        Lock,
        PipedProcess,
        PipedProcessLauncher,
        Threads,
        Worker,
    )

_UNLIMITED = frozenset({"unlimited", "infinite"})
_MAX_CLOCK_FIELDS = 3
_SINFO_FIELDS = 2
_SINFO_TIMEOUT_SECONDS = 30
#: Exit status of a command rejected before ``srun`` (the shell's "usage error").
_REJECTED_STATUS = 2
_WINDOWS_TIMEOUT_SECONDS = 30
_CANCEL_GRACE_SECONDS = 30
_CANCEL_POLL_SECONDS = 0.2
_SCANCEL_TIMEOUT_SECONDS = 60
#: ``scancel`` runs again while its outcome is unknown: it could not start, timed
#: out, or exited nonzero (an ``ssh`` wrapper that lost its connection exits 255).
_SCANCEL_ATTEMPTS = 3
_SQUEUE_TIMEOUT_SECONDS = 30
#: A process-start acknowledgement can be lost before Slurm has made the job
#: visible.  Require a complete absence window, rather than treating one empty
#: queue response as proof that no late submission can still appear.
_CONFIRM_ABSENT_OBSERVATIONS = 6
_CONFIRM_MAX_OBSERVATIONS = 12
#: The default pause between those queue checks, so the window of proof is
#: ``(_CONFIRM_ABSENT_OBSERVATIONS - 1)`` times this many seconds.
_DEFAULT_QUEUE_CHECK_INTERVAL_SECONDS = 5.0
#: How long ``run`` waits for the cancel worker once ``srun`` exited. It bounds
#: every ``scancel`` attempt; the worker returns at once in every other state.
_WATCHER_JOIN_SECONDS = _SCANCEL_ATTEMPTS * _SCANCEL_TIMEOUT_SECONDS + _CANCEL_GRACE_SECONDS
_READ_CHARACTERS = 65_536
_MAX_QUEUE_MINUTES = 60 * 24 * 365
_MAX_PREFIX_CHARS = 32
#: Hex characters of the run's digest in its job name prefix: 32 bits keeps two
#: concurrent runs of one user apart and leaves the operator's prefix most of its room.
_RUN_TAG_CHARS = 8


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


class _GpuCancellationWatcherError(RuntimeError):
    """The cancellation watcher did not settle within its bounded join."""


@dataclass(slots=True)
class _CancellationAuthority:
    """Revocable permission for one watcher to act on a stable external identity."""

    lock: Lock
    active: bool = True

    def run(self, action: Callable[[], None]) -> bool:
        """Run *action* only while this call still owns the job name."""
        with self.lock:
            if not self.active:
                return False
            action()
            return True

    def revoke(self) -> None:
        """Wait out any authorized action, then permanently fence later ones."""
        with self.lock:
            self.active = False


@dataclass(slots=True)
class _SubmittedJobState:
    """Facts accumulated while one possibly submitted job is being settled."""

    finished: Event
    status: int = 1
    failure: BaseException | None = None
    stop_required: bool = False
    relay_finished: bool = False
    watcher_settled: bool = False

    def record_failure(self, error: BaseException, context: str) -> None:
        self.stop_required = True
        self.status = 1
        if self.failure is None:
            self.failure = error
        else:
            self.failure.add_note(f"{context}: {type(error).__name__}: {error}")


class AgentGpuConfig(BaseModel):
    """Operator limits and commands for the agent's GPU jobs, submitted from this host.

    ``partitions`` is a preference order. A partition is eligible only when its
    time limit, read from the scheduler (``sinfo``), fits the request. With
    ``windows_command`` set, the first eligible partition whose reported window
    starts the job now is chosen; otherwise, and when nothing starts now, the
    first eligible partition is used and the job waits in its queue. A request
    no partition's limit fits is rejected before ``srun``, naming each limit.

    A job sees its workspace (writable), the system directories, and
    ``read_only_paths``: extra absolute host paths, such as the operator's
    Python environment, that it may read and never write. Without them an
    interpreter or dependency outside the workspace is invisible to the job.

    ``max_concurrent_jobs`` caps how many of the run's agent GPU jobs, across
    all of its agents, are submitted to the scheduler at once (``None``: no
    cap). A request past the cap waits on the submit host, first come first
    served, and is submitted when one of the run's jobs ends; its ``--time``
    limit starts at submission. The cap keeps one run from monopolizing a
    shared partition with a steady stream of short jobs.

    After a command that ended with a nonzero status or was stopped, the launcher
    checks the queue until the job has been absent from it for
    ``queue_check_interval_seconds`` times five consecutive checks, so a job whose
    submission was acknowledged late is not mistaken for gone. A command that ends
    nonzero returns its status only after that window, so a shorter interval
    shortens the window and the wait.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    partitions: tuple[str, ...] = Field(min_length=1)
    max_gpus: Annotated[int, Field(gt=0)]
    max_time_minutes: Annotated[int, Field(gt=0)]
    default_time_minutes: Annotated[int, Field(gt=0)] = 30
    windows_command: tuple[str, ...] | None = None
    srun_arguments: tuple[str, ...] = ()
    read_only_paths: tuple[str, ...] = ()
    max_concurrent_jobs: Annotated[int, Field(gt=0)] | None = None
    queue_check_interval_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = (
        _DEFAULT_QUEUE_CHECK_INTERVAL_SECONDS
    )
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

    def for_run(self, run_id: str) -> AgentGpuConfig:
        """This configuration with a job name prefix that only ``run_id``'s jobs carry.

        The operator's prefix is kept (cut to leave room) and a digest of the run id is
        appended, so the run's own GPU jobs can be told apart in the queue from another
        run's of the same user. The result still satisfies the prefix pattern.
        """
        tag = hashlib.sha256(run_id.encode()).hexdigest()[:_RUN_TAG_CHARS]
        kept = self.job_name_prefix[: _MAX_PREFIX_CHARS - len(tag) - 1]
        return self.model_copy(update={"job_name_prefix": f"{kept}-{tag}"})

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
    job_name: str | None = None


@dataclass(frozen=True, slots=True)
class GpuJobOutcome:
    """The scheduler-visible outcome of one GPU job.

    ``termination_confirmed`` is false after cancellation or a post-submission
    relay failure when the launcher cannot prove the named Slurm job left the
    queue. A nonzero exit status from the local ``srun`` wrapper is not proof
    that the allocation ended, so it also requires scheduler absence evidence.
    ``failure`` distinguishes launcher failure from the job's own exit status
    without throwing away that settlement proof.
    """

    exit_code: int
    termination_confirmed: bool = True
    failure: BaseException | None = None
    """A launch/relay failure, returned only after settlement was attempted."""
    job_name: str | None = None
    """Stable scheduler identity, required while termination remains unconfirmed."""

    def __post_init__(self) -> None:
        """Reject an unknown outcome that admission and recovery cannot reconcile."""
        if not self.termination_confirmed and not self.job_name:
            message = "an unconfirmed GPU job outcome requires its stable job name"
            raise ValueError(message)


def _minutes(value: str) -> float:
    """Parse a Slurm ``[D-][[HH:]MM:]SS`` limit, or ``unlimited``/``infinite``, to minutes.

    Raises ``ValueError`` for anything else (``n/a``, ``NOT_SET``, an empty string).
    """
    if value.lower() in _UNLIMITED:
        return math.inf
    days, _, clock = value.rpartition("-")
    parts = [int(part) for part in clock.split(":")]
    if not 1 <= len(parts) <= _MAX_CLOCK_FIELDS or any(part < 0 for part in parts):
        message = f"not a Slurm time limit: {value!r}"
        raise ValueError(message)
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return int(days or 0) * 24 * 60 + seconds / 60


def parse_partition_limits(sinfo_output: str) -> Mapping[str, float]:
    """Map partition name to its time limit in minutes from ``sinfo -h -o "%P %l"`` text.

    The default partition's ``*`` suffix is dropped. A line that is not
    ``<name> <limit>`` with a parseable limit is left out, so its partition is
    reported as unknown rather than guessed at.
    """
    limits: dict[str, float] = {}
    for line in sinfo_output.splitlines():
        fields = line.split()
        if len(fields) != _SINFO_FIELDS:
            continue
        try:
            limits[fields[0].removesuffix("*")] = _minutes(fields[1])
        except ValueError:
            continue
    return limits


def read_partition_limits(shell: Sequence[str], probe: CommandProbe) -> Mapping[str, float]:
    """Ask the scheduler, through *shell*, for every partition's time limit in minutes.

    An unanswered or failed ``sinfo`` yields no limits, so every partition is
    then unknown to :func:`choose_partition` and a request is rejected, not guessed.
    """
    argv = (*shell, shlex.join(("sinfo", "--noheader", "--format=%P %l")))
    result = probe.run(argv, timeout_seconds=_SINFO_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        return {}
    return parse_partition_limits(result.stdout)


def _starts_now(partition: Mapping[str, object], request: GpuJobRequest) -> bool:
    """Return whether the partition's window starts *request* now."""
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
    limits: Mapping[str, float],
    windows: Mapping[str, object] | None,
) -> str:
    """Prefer an eligible partition that starts *request* now, else the first eligible one.

    A partition is eligible when its time limit in *limits* (minutes by name,
    from :func:`read_partition_limits`) is at least the request's. *windows* is
    the scheduler's start-time report (the ``slurm-windows --json`` shape:
    ``groups[].options[]`` with ``max_time`` and ``start`` per partition). A
    malformed or absent report only loses the "starts now" preference.

    Raises :class:`AgentGpuRequestError`, naming the requested minutes and each
    configured partition's limit, when none is eligible; a partition missing
    from *limits* is shown as unknown and never chosen.
    """
    eligible = [
        name for name in config.partitions if limits.get(name, -math.inf) >= request.time_minutes
    ]
    if not eligible:
        shown = ", ".join(
            f"{name} ({_describe_limit(limits[name]) if name in limits else 'limit unknown'})"
            for name in config.partitions
        )
        message = (
            f"--time {request.time_minutes} fits no configured partition's time limit: {shown}"
        )
        raise AgentGpuRequestError(message)
    try:
        entries = cast(
            "list[Mapping[str, object]]", cast("Mapping[str, object]", windows)["partitions"]
        )
        reported = {str(entry["partition"]): entry for entry in entries}
        for name in eligible:
            if name in reported and _starts_now(reported[name], request):
                return name
    except (KeyError, TypeError, ValueError):
        pass
    return eligible[0]


def _describe_limit(minutes: float) -> str:
    return "unlimited" if math.isinf(minutes) else f"limit {minutes:g} minutes"


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
    ) -> GpuJobOutcome:
        """Run *command*, stream output, and report whether a requested stop settled."""
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
    ) -> GpuJobOutcome:
        """Run *command* in a new allocation, streaming merged output to *write*.

        Returns the command's exit status as ``srun`` reports it and whether a
        requested stop was confirmed at the scheduler. A request that no
        configured partition can hold writes its diagnostic and returns exit
        status 2 without submitting a job. Setting
        *cancel* (from any thread) cancels the job by its unique name, then
        stops ``srun``; the call still returns only after ``srun`` exits. Stopping
        a ``docker exec`` or ``ssh`` wrapper does not stop the ``srun`` behind it,
        so a cancelled job is then looked up in the queue, and one still listed is
        reported on *write* and in the typed outcome as unconfirmed.
        """
        try:
            try:
                partition = choose_partition(
                    self._config,
                    request,
                    read_partition_limits(self._shell, self._probe),
                    read_windows(self._config, self._probe),
                )
            except AgentGpuRequestError as error:
                write(f"[vibesys-gpu] rejected before srun: {error}\n".encode())
                return GpuJobOutcome(_REJECTED_STATUS)
            job_name = command.job_name or new_job_name(self._config)
            argv = srun_argv(
                self._config, request, partition=partition, job_name=job_name, command=command.argv
            )
            write(
                f"[vibesys-gpu] srun partition={partition} gpus={request.gpus} "
                f"time={request.time_minutes}m job-name={job_name}\n".encode()
            )
        except Exception as error:  # noqa: BLE001  # lint-waiver: LW-213401 [BLE001]; before the process-start boundary, every failure proves no job was submitted and is returned as a typed settled outcome.
            return GpuJobOutcome(1, failure=error)
        try:
            process = self._processes.start(
                (*self._shell, shlex.join(argv)),
                cwd=command.cwd,
                env=dict(command.env),
                merge_stderr=True,
            )
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213421 [BLE001]; process construction can fail after srun starts, so every failure class is scheduler-ambiguous until the fixed job identity is reconciled.
            self._cancel_ambiguous_start(job_name, error)
            return GpuJobOutcome(
                1,
                termination_confirmed=False,
                failure=error,
                job_name=job_name,
            )
        return self._run_submitted(process, job_name, write=write, cancel=cancel)

    def _run_submitted(
        self,
        process: PipedProcess,
        job_name: str,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> GpuJobOutcome:
        """Relay and settle one process after submission may have occurred."""
        finished = self._threads.event()
        cancelled = self._threads.event()
        watcher = None
        watcher_failures: list[BaseException] = []
        state = _SubmittedJobState(finished)
        authority = _CancellationAuthority(self._threads.lock())

        def mark_cancelled() -> None:
            """Record stop ownership before touching the fallible coordination event."""
            state.stop_required = True
            cancelled.set()

        def watch_cancellation() -> None:
            self._watch_submitted_cancellation(
                process,
                job_name,
                cancel,
                state,
                cancelled,
                mark_cancelled,
                watcher_failures,
                authority,
            )

        try:
            watcher = self._threads.spawn(
                watch_cancellation,
                name="vibesys-gpu-cancel",
                daemon=True,
            )
            state.status = self._relay_submitted(process, write)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213402 [BLE001]; after submission, every boundary failure must enter the same fail-closed settlement path before a caller may clean up the workspace.
            self._settle_submission_failure(process, job_name, state, error)
        finally:
            if error := self._finish_watcher(state, watcher, authority):
                self._settle_submission_failure(
                    process,
                    job_name,
                    state,
                    error,
                )
        if watcher_failures:
            watcher_error = watcher_failures[0]
            state.record_failure(watcher_error, "GPU cancellation watcher failed")
            primary = state.failure or watcher_error
            self._settle_failed_job(process, job_name, primary)
        if state.status != 0 and not state.stop_required:
            self._cancel_after_abnormal_exit(job_name)
        scheduler_proof_required = state.stop_required or state.status != 0
        termination_confirmed = state.watcher_settled and (
            not scheduler_proof_required or self._job_gone_after_stop(job_name, state.failure)
        )
        if not termination_confirmed:
            write(
                f"[vibesys-gpu] cancel unconfirmed: job {job_name} is still in the "
                "Slurm queue and may keep its allocation until its time limit\n".encode()
            )
        return GpuJobOutcome(
            state.status,
            termination_confirmed=termination_confirmed,
            failure=state.failure,
            job_name=job_name,
        )

    @staticmethod
    def _relay_submitted(process: PipedProcess, write: Callable[[bytes], None]) -> int:
        while chunk := process.stdout.read(_READ_CHARACTERS):
            write(chunk.encode())
        return process.wait(None)

    def _settle_submission_failure(
        self,
        process: PipedProcess,
        job_name: str,
        state: _SubmittedJobState,
        error: BaseException,
    ) -> None:
        state.record_failure(error, "GPU submitted job failed")
        primary = state.failure or error
        self._settle_failed_job(process, job_name, primary)

    def _watch_submitted_cancellation(  # noqa: PLR0913  # lint-waiver: LW-213420 [PLR0913]; these are the already-owned job and its coordination roles, not independent configuration.
        self,
        process: PipedProcess,
        job_name: str,
        cancel: Event,
        state: _SubmittedJobState,
        cancelled: Event,
        mark_cancelled: Callable[[], None],
        failures: list[BaseException],
        authority: _CancellationAuthority,
    ) -> None:
        try:
            self._cancel_when_asked(process, job_name, cancel, state, mark_cancelled, authority)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213410 [BLE001]; worker failures cross the thread boundary as typed launcher outcomes and force settlement.
            # Publish the primary failure before another injected event fault can occur.
            failures.append(error)

            def interrupt_failed_watcher(failure: BaseException = error) -> None:
                self._interrupt_failed_watcher(process, job_name, failure)

            try:
                self._record_stop_event(cancelled, error, "GPU watcher")
            finally:
                authority.run(interrupt_failed_watcher)

    @staticmethod
    def _record_stop_event(event: Event, failure: BaseException, context: str) -> None:
        try:
            event.set()
        except BaseException as coordination_error:  # noqa: BLE001  # lint-waiver: LW-213414 [BLE001]; event bookkeeping is diagnostic and cannot skip independent settlement.
            failure.add_note(
                f"{context} could not record cancellation: "
                f"{type(coordination_error).__name__}: {coordination_error}"
            )

    @staticmethod
    def _finish_watcher(
        state: _SubmittedJobState,
        watcher: Worker | None,
        authority: _CancellationAuthority,
    ) -> BaseException | None:
        """Publish completion, revoke external authority, then join the watcher."""
        state.relay_finished = True
        errors: list[BaseException] = []
        try:
            state.finished.set()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213407 [BLE001]; the caller converts any injected coordination failure into fail-closed settlement.
            errors.append(error)
        try:
            authority.revoke()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213426 [BLE001]; revocation and watcher settlement are independent cleanup boundaries whose failures both retain unconfirmed ownership.
            errors.append(error)
        if watcher is None:
            state.watcher_settled = True
        else:
            try:
                watcher.join(_WATCHER_JOIN_SECONDS)
                if watcher.is_alive():
                    errors.append(_GpuCancellationWatcherError())
                else:
                    state.watcher_settled = True
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213422 [BLE001]; completion publication and watcher ownership are independent cleanup steps, and both outcomes must be retained.
                errors.append(error)
        if not errors:
            return None
        primary = errors[0]
        for extra in errors[1:]:
            primary.add_note(
                f"GPU cancellation watcher cleanup also failed: {type(extra).__name__}: {extra}"
            )
        return primary

    def _cancel_ambiguous_start(self, job_name: str, failure: BaseException) -> None:
        """Request cancellation but retain unknown ownership for restart recovery.

        An empty queue immediately after ``start`` raises is not proof: a child
        whose acknowledgement was lost can submit later. The durable caller keeps
        this identity and a run-scoped recovery owner applies the absence fence.
        """
        try:
            self.cancel(job_name)
        except BaseException as stop_error:  # noqa: BLE001  # lint-waiver: LW-213423 [BLE001]; the original process-start failure remains primary and missing scheduler evidence must fail closed.
            failure.add_note(f"ambiguous Slurm submission cancellation also failed: {stop_error}")

    def _cancel_after_abnormal_exit(self, job_name: str) -> None:
        """Best-effort stop a job whose local wrapper exited nonzero.

        The original exit status remains the workload/transport evidence. A
        failed cancellation request therefore does not replace it; the queue
        absence window below still decides whether termination is confirmed.
        """
        # Best-effort scancel cannot replace the original status; absence proof still fails closed.
        with suppress(BaseException):
            self.cancel(job_name)

    def reconcile(self, job_name: str) -> bool:
        """Cancel a durably recorded job identity and confirm it left the queue."""
        try:
            self.cancel(job_name)
        except Exception:  # noqa: BLE001  # lint-waiver: LW-213408 [BLE001]; reconciliation fails closed on every controller-boundary failure.
            return False
        return self._job_gone_after_stop(job_name, None)

    def _settle_failed_job(
        self, process: PipedProcess, job_name: str, failure: BaseException
    ) -> None:
        """Best-effort stop after a submitted job's relay fails; queue proof follows."""
        try:
            self.cancel(job_name)
        except BaseException as stop_error:  # noqa: BLE001  # lint-waiver: LW-213403 [BLE001]; a failed stop attempt is recorded on the primary relay failure and the later queue check still fails closed.
            failure.add_note(f"Slurm cancellation also failed: {stop_error}")
        actions = (
            ("terminate", process.terminate_group),
            ("kill", process.kill_group),
            ("reap", lambda: process.wait(_CANCEL_GRACE_SECONDS)),
        )
        for label, action in actions:
            try:
                action()
            except BaseException as stop_error:  # noqa: BLE001  # lint-waiver: LW-213404 [BLE001]; each local-wrapper cleanup step is best effort, cannot replace the primary relay failure, and cannot bypass scheduler confirmation.
                failure.add_note(f"local GPU client {label} also failed: {stop_error}")

    def _interrupt_failed_watcher(
        self, process: PipedProcess, job_name: str, failure: BaseException
    ) -> None:
        """Break the main relay out when its cancellation worker itself fails."""
        actions = (
            ("cancel", lambda: self.cancel(job_name)),
            ("terminate", process.terminate_group),
            ("kill", process.kill_group),
            ("close output", process.stdout.close),
        )
        for label, action in actions:
            try:
                action()
            except BaseException as stop_error:  # noqa: BLE001  # lint-waiver: LW-213411 [BLE001]; the main relay performs full settlement; this worker attempts every independent unblocking action.
                failure.add_note(f"GPU watcher failure {label} also failed: {stop_error}")

    def _job_gone_after_stop(self, job_name: str, failure: BaseException | None) -> bool:
        """Fail closed when the scheduler confirmation itself raises."""
        try:
            return self.job_gone(job_name)
        except BaseException as confirmation_error:  # noqa: BLE001  # lint-waiver: LW-213405 [BLE001]; absent scheduler evidence is an unconfirmed stop for every boundary failure type.
            if failure is not None:
                failure.add_note(f"Slurm stop confirmation also failed: {confirmation_error}")
            return False

    def _cancel_when_asked(  # noqa: PLR0913  # lint-waiver: LW-213427 [PLR0913]; this private watcher step receives the already-owned process, job identity, lifecycle facts, and revocable authority as one operation rather than exposing a second state bundle.
        self,
        process: PipedProcess,
        job_name: str,
        cancel: Event,
        state: _SubmittedJobState,
        mark_cancelled: Callable[[], None],
        authority: _CancellationAuthority,
    ) -> None:
        while not state.relay_finished:
            if cancel.wait(_CANCEL_POLL_SECONDS):
                break
        else:
            return
        if state.relay_finished:
            return

        def stop() -> None:
            if state.relay_finished:
                return
            mark_cancelled()
            self.cancel(job_name)
            if state.relay_finished:
                return
            process.terminate_group()
            deadline = self._threads.now() + _CANCEL_GRACE_SECONDS
            while not state.relay_finished and self._threads.now() < deadline:
                remaining = deadline - self._threads.now()
                state.finished.wait(min(_CANCEL_POLL_SECONDS, remaining))
            if not state.relay_finished:
                process.kill_group()

        authority.run(stop)

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
        """Whether ``squeue`` proves a bounded continuous absence for *job_name*.

        An unanswered or failed ``squeue`` is not an answer: the job counts as
        present, so a cancel is never reported confirmed on no evidence. Multiple
        consecutive empty observations fence a process-start failure whose child
        may submit only after the first scheduler query.
        """
        argv = (
            *self._shell,
            shlex.join(("squeue", f"--name={job_name}", "--me", "--noheader", "--format=%i")),
        )
        absent = 0
        for attempt in range(_CONFIRM_MAX_OBSERVATIONS):
            if attempt:
                self._threads.sleep(self._config.queue_check_interval_seconds)
            result = self._probe.run(argv, timeout_seconds=_SQUEUE_TIMEOUT_SECONDS)
            if result is not None and result.returncode == 0 and not result.stdout.strip():
                absent += 1
                if absent == _CONFIRM_ABSENT_OBSERVATIONS:
                    return True
            else:
                absent = 0
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
    "GpuJobOutcome",
    "GpuJobRequest",
    "GpuLauncher",
    "choose_partition",
    "parse_partition_limits",
    "read_partition_limits",
    "read_windows",
    "resolve_executable",
    "srun_argv",
]
