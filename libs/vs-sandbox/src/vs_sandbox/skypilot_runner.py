"""Focused external-CLI adapter for SkyPilot cluster jobs."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import tempfile
import uuid
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, assert_never, runtime_checkable

import yaml
from pydantic import TypeAdapter

from vs_decode.api import Unreadable, UnreadableKind, decode_json
from vs_sandbox.skypilot_states import (
    INITIAL_JOB_LIFE,
    ClusterEnded,
    ClusterInfo,
    ClusterInitializing,
    ClusterUnknown,
    ClusterUp,
    Finished,
    Indeterminate,
    JobLife,
    JobStatus,
    JobUnknown,
    LogsFailed,
    RemoteJobInfo,
    advance,
    cluster_life,
    describe_cluster,
    job_reading,
    logs_verdict,
    status_text,
)
from vs_sim.api import (
    OsThreads,
    PipedProcess,
    PipedProcessLauncher,
    ProcessTimeoutError,
    SubprocessPipedProcessLauncher,
    Threads,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from io import TextIOBase

    from vs_sandbox.skypilot_config import ResolvedSkyPilotResources
    from vs_sim.api import Condition, Lock, Worker

_CLUSTER_COMPONENT = re.compile(r"[^a-z0-9-]+")
_MAX_CLUSTER_NAME = 28
_JOB_DISCOVERY_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 2.0
_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0
_PROCESS_OUTPUT_TAIL_CHARACTERS = 64 * 1024
_PROCESS_OUTPUT_READ_CHARACTERS = 64 * 1024
_MAX_PENDING_OUTPUT_SEGMENTS = 1


class SkyPilotCLIError(RuntimeError):
    """Base error for the SkyPilot external process boundary."""

    @classmethod
    def executable_not_found(cls, executable: str) -> SkyPilotCLIError:
        """Describe a missing SkyPilot executable."""
        return cls(f"SkyPilot executable {executable!r} was not found")


class SkyPilotTimeoutError(SkyPilotCLIError):
    """Raised when a SkyPilot control-plane command times out."""

    @classmethod
    def waiting_timed_out(cls, detail: str) -> SkyPilotTimeoutError:
        """Describe a cluster wait that exhausted its remaining deadline."""
        return cls(detail)

    @classmethod
    def operation_deadline_exceeded(cls) -> SkyPilotTimeoutError:
        """Describe a SkyPilot operation that exceeded its deadline."""
        return cls("SkyPilot operation exceeded its deadline")

    @classmethod
    def command_timed_out(cls, timeout: float | None) -> SkyPilotTimeoutError:
        """Describe a SkyPilot process that exceeded its timeout."""
        return cls(f"SkyPilot command timed out after {timeout} seconds")


class SkyPilotControlPlaneError(SkyPilotCLIError):
    """Raised when a SkyPilot lifecycle command exits unsuccessfully."""

    @classmethod
    def logs_failed(cls, returncode: int) -> SkyPilotControlPlaneError:
        """Describe a failed SkyPilot log retrieval command."""
        return cls(f"SkyPilot logs failed with exit code {returncode}")

    @classmethod
    def operation_failed(cls, operation: str, returncode: int) -> SkyPilotControlPlaneError:
        """Describe a failed SkyPilot lifecycle command."""
        return cls(f"SkyPilot {operation} failed with exit code {returncode}")


# A control command's JSON reply is read whole or not at all: ``vs_decode`` bounds its size and
# refuses text that is not one complete JSON document (a cut reply, unbalanced or absurdly nested
# brackets), and a document without the list the caller reads is an error, never "no entries".
_CONTROL_JSON_MAX_BYTES = 16 * 1024 * 1024
_STATUS_DOCUMENT: TypeAdapter[list[dict[str, Any]] | dict[str, Any]] = TypeAdapter(
    list[dict[str, Any]] | dict[str, Any]
)
_QUEUE_DOCUMENT: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])


def _status_error(unreadable: Unreadable) -> SkyPilotOutputError:
    if unreadable.kind is UnreadableKind.NOT_JSON:
        return SkyPilotOutputError.invalid_status_json()
    if unreadable.kind is UnreadableKind.TOO_LARGE:
        return SkyPilotOutputError.truncated_control_output("status")
    return SkyPilotOutputError.status_missing_cluster_list()


def _queue_error(unreadable: Unreadable) -> SkyPilotOutputError:
    if unreadable.kind is UnreadableKind.NOT_JSON:
        return SkyPilotOutputError.invalid_queue_json()
    if unreadable.kind is UnreadableKind.TOO_LARGE:
        return SkyPilotOutputError.truncated_control_output("queue")
    return SkyPilotOutputError.queue_missing_job_list()


class SkyPilotOutputError(SkyPilotCLIError):
    """Raised when SkyPilot emits malformed machine-readable output."""

    @classmethod
    def invalid_status_json(cls) -> SkyPilotOutputError:
        """Describe a cluster status response that is not JSON."""
        return cls("SkyPilot status returned invalid JSON")

    @classmethod
    def status_missing_cluster_list(cls) -> SkyPilotOutputError:
        """Describe a cluster status response without a cluster list."""
        return cls("SkyPilot status JSON must contain a cluster list")

    @classmethod
    def duplicate_jobs(cls, job_name: str) -> SkyPilotOutputError:
        """Describe duplicate job records for one requested name."""
        return cls(f"SkyPilot queue returned duplicate jobs named {job_name!r}")

    @classmethod
    def invalid_job_id(cls) -> SkyPilotOutputError:
        """Describe a queue record whose job ID is not an integer."""
        return cls("SkyPilot queue returned a non-integer job ID")

    @classmethod
    def job_id_changed(cls, job_name: str, expected: int, actual: int) -> SkyPilotOutputError:
        """Describe a job whose persisted ID changed in the remote queue."""
        return cls(f"SkyPilot job {job_name!r} changed ID from {expected} to {actual}")

    @classmethod
    def unknown_job_status(cls, raw: str | None) -> SkyPilotOutputError:
        """Describe a queue record whose job status is missing or not a documented one."""
        return cls(f"SkyPilot queue returned an unknown job status {raw!r}")

    @classmethod
    def invalid_queue_json(cls) -> SkyPilotOutputError:
        """Describe a queue response that is not JSON."""
        return cls("SkyPilot queue returned invalid JSON")

    @classmethod
    def queue_missing_job_list(cls) -> SkyPilotOutputError:
        """Describe a queue response without a job list for the cluster."""
        return cls("SkyPilot queue JSON must map the cluster name to a job list")

    @classmethod
    def truncated_control_output(cls, operation: str) -> SkyPilotOutputError:
        """Reject an incomplete machine-readable control-plane response."""
        return cls(f"SkyPilot {operation} output exceeded the retained-output limit")


class SkyPilotClusterNotReadyError(SkyPilotCLIError):
    """Raised when a cluster does not reach a reusable state in time."""

    @classmethod
    def unknown_status(cls, name: str, raw: str | None) -> SkyPilotClusterNotReadyError:
        """Describe a cluster whose reported status is missing or not a documented one."""
        return cls(f"SkyPilot cluster {name!r} has an unknown status {raw!r}")

    @classmethod
    def became_unavailable(cls, name: str, status: str) -> SkyPilotClusterNotReadyError:
        """Describe a cluster that left initialization before becoming ready."""
        return cls(f"SkyPilot cluster {name!r} became {status} while initializing")


class SkyPilotJobStateError(SkyPilotCLIError):
    """Raised when SkyPilot reports an invalid or indeterminate remote job state."""

    @classmethod
    def logs_indicate_unknown_state(cls, returncode: int, job_id: int) -> SkyPilotJobStateError:
        """Describe an indeterminate state code returned while reading job logs."""
        return cls(f"SkyPilot logs returned job state code {returncode} for job {job_id}")


@dataclass(frozen=True)
class ProcessResult:
    """Exit status and bounded diagnostic tails from one external process."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    stdout_omitted_characters: int = 0
    stderr_omitted_characters: int = 0

    def __post_init__(self) -> None:
        """Reject invalid truncation metadata at the process boundary."""
        if self.stdout_omitted_characters < 0 or self.stderr_omitted_characters < 0:
            message = "SkyPilot output omitted-character counts must be nonnegative"
            raise ValueError(message)

    @property
    def stdout_truncated(self) -> bool:
        """Whether characters before the retained stdout tail were discarded."""
        return self.stdout_omitted_characters > 0

    @property
    def stderr_truncated(self) -> bool:
        """Whether characters before the retained stderr tail were discarded."""
        return self.stderr_omitted_characters > 0


class CommandRunner(Protocol):
    """Injectable process boundary used by :class:`SkyPilotJobRunner`."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: Path | None = None,
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Run one command and return its captured result.

        A sink that implements :class:`CancellableOutputSink` receives every
        bounded segment concurrently and is cancelled during process cleanup.
        An ordinary callable receives the retained tail synchronously after
        successful process and pipe completion, so the runner never abandons
        a callback in a background worker. The result reports how many earlier
        characters were omitted from each retained tail.
        """
        ...


@runtime_checkable
class CancellableOutputSink(Protocol):
    """A stream callback whose active delivery can be stopped during cleanup."""

    def __call__(self, data: str) -> None:
        """Deliver one output segment."""
        ...

    def cancel(self) -> None:
        """Return promptly and cause any active callback invocation to return."""
        ...


class SubprocessCommandRunner:
    """Run one owned POSIX process group while forwarding both output streams.

    A failed wait kills every process remaining in the group, then reaps the
    leader and joins both pipe drains against one shared cleanup budget (five
    seconds by default). After a normal leader exit, drains receive one cleanup
    budget. If a descendant still holds a pipe, its process group is killed and
    receives one bounded post-kill cleanup budget. Pipe drains never invoke
    output sinks. Cancellable sinks stream from a one-segment bounded queue;
    cancellation releases its backpressure so a slow consumer cannot retain an
    owned pipe past cleanup. Ordinary callables are replayed synchronously
    after successful pipe completion.
    """

    def __init__(
        self,
        threads: Threads | None = None,
        *,
        processes: PipedProcessLauncher | None = None,
        cleanup_timeout: float = _PROCESS_CLEANUP_TIMEOUT_SECONDS,
        max_output_characters: int = _PROCESS_OUTPUT_TAIL_CHARACTERS,
    ) -> None:
        """Bind process ownership and fixed cleanup and retained-output bounds."""
        if not math.isfinite(cleanup_timeout) or cleanup_timeout < 0:
            message = "cleanup_timeout must be finite and nonnegative"
            raise ValueError(message)
        if max_output_characters < 0:
            message = "max_output_characters must be nonnegative"
            raise ValueError(message)
        self._threads: Threads = threads or OsThreads()
        self._processes: PipedProcessLauncher = processes or SubprocessPipedProcessLauncher()
        self._cleanup_timeout = cleanup_timeout
        self._max_output_characters = max_output_characters

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: Path | None = None,
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Run one command while optionally forwarding output."""
        normalized = tuple(str(part) for part in argv)
        process = self._processes.start(normalized, cwd=cwd)
        sink_failures = _SinkFailures(self._threads.lock())
        stdout = process.stdout
        stderr = process.stderr
        outputs = (
            _StreamOutput(
                self._threads,
                stdout,
                stdout_sink,
                max_output_characters=self._max_output_characters,
                sink_failures=sink_failures,
            ),
            _StreamOutput(
                self._threads,
                stderr,
                stderr_sink,
                max_output_characters=self._max_output_characters,
                sink_failures=sink_failures,
            ),
        )
        drain_workers: list[Worker] = []
        delivery_workers: list[Worker] = []
        try:
            for name, output in zip(("stdout", "stderr"), outputs, strict=True):
                drain_workers.append(
                    self._threads.spawn(output.drain, name=f"sky-{name}", daemon=True)
                )
                if output.has_delivery_worker:
                    delivery_workers.append(
                        self._threads.spawn(
                            output.deliver,
                            name=f"sky-{name}-sink",
                            daemon=True,
                        )
                    )
            returncode = process.wait(timeout=timeout)
        except BaseException as failure:
            self._cleanup_failed_run(
                process,
                outputs,
                (drain_workers, delivery_workers),
                (stdout, stderr),
                failure,
            )
            raise
        if not self._join_workers(drain_workers):
            self._cancel_deliveries(outputs)
            deadline = self._threads.now() + self._cleanup_timeout
            try:
                self._stop_and_drain(process, drain_workers, deadline=deadline)
            finally:
                self._join_workers(delivery_workers, deadline=deadline)
                self._close_if_drained((stdout, stderr), drain_workers)
            message = (
                "SkyPilot command output did not close within "
                f"{self._cleanup_timeout} seconds after its process exited; "
                "the process group was killed and given the same bounded cleanup period"
            )
            raise SkyPilotCLIError(message)
        stdout.close()
        stderr.close()
        if not self._join_workers(delivery_workers):
            self._cancel_deliveries(outputs)
            if not self._join_workers(delivery_workers):
                message = (
                    "SkyPilot output delivery did not stop within "
                    f"{self._cleanup_timeout} seconds after cancellation"
                )
                raise SkyPilotCLIError(message)
            message = (
                "SkyPilot output delivery exceeded "
                f"{self._cleanup_timeout} seconds and was cancelled"
            )
            raise SkyPilotCLIError(message)
        for output in outputs:
            output.deliver_captured()
        sink_failure = sink_failures.first
        if sink_failure is not None:
            raise sink_failure
        captured_stdout = outputs[0].result()
        captured_stderr = outputs[1].result()
        return ProcessResult(
            normalized,
            returncode,
            captured_stdout.text,
            captured_stderr.text,
            captured_stdout.omitted_characters,
            captured_stderr.omitted_characters,
        )

    def _cleanup_failed_run(
        self,
        process: PipedProcess,
        outputs: Sequence[_StreamOutput],
        workers: tuple[Sequence[Worker], Sequence[Worker]],
        streams: tuple[TextIOBase, TextIOBase],
        failure: BaseException,
    ) -> None:
        """Settle owned resources and surface a failed group signal over *failure*."""
        drain_workers, delivery_workers = workers
        self._cancel_deliveries(outputs)
        deadline = self._threads.now() + self._cleanup_timeout
        cleanup_error: OSError | None = None
        try:
            self._stop_and_drain(process, drain_workers, deadline=deadline)
        except OSError as exc:
            cleanup_error = exc
        finally:
            self._join_workers(delivery_workers, deadline=deadline)
            self._close_if_drained(streams, drain_workers)
        if cleanup_error is not None:
            raise cleanup_error from failure

    def _stop_and_drain(
        self,
        process: PipedProcess,
        workers: Sequence[Worker],
        *,
        deadline: float,
    ) -> None:
        """Kill the group, cancel pipe reads, then settle within one cleanup bound."""
        cleanup_error: OSError | None = None
        try:
            process.kill_group()
        except OSError as exc:
            cleanup_error = exc
        try:
            process.cancel_output()
        except OSError as exc:
            cleanup_error = cleanup_error or exc
        try:
            with suppress(OSError, ProcessTimeoutError):
                process.wait(timeout=self._remaining(deadline))
        finally:
            self._join_workers(workers, deadline=deadline)
        if cleanup_error is not None:
            raise cleanup_error

    @staticmethod
    def _close_if_drained(
        streams: tuple[TextIOBase, TextIOBase], workers: Sequence[Worker]
    ) -> None:
        """Close owned pipes only after no worker can still be reading them."""
        if not any(worker.is_alive() for worker in workers):
            for stream in streams:
                stream.close()

    @staticmethod
    def _cancel_deliveries(outputs: Sequence[_StreamOutput]) -> None:
        for output in outputs:
            output.cancel_delivery()

    def _join_workers(
        self,
        workers: Sequence[Worker],
        *,
        deadline: float | None = None,
    ) -> bool:
        """Join workers against one shared finite deadline."""
        effective_deadline = (
            self._threads.now() + self._cleanup_timeout if deadline is None else deadline
        )
        for worker in workers:
            worker.join(self._remaining(effective_deadline))
        return not any(worker.is_alive() for worker in workers)

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - self._threads.now())


@dataclass(frozen=True)
class _RetainedOutput:
    """One bounded stream suffix and the size of its discarded prefix."""

    text: str
    omitted_characters: int


class _OutputTail:
    """Drain arbitrary text segments while retaining a fixed-size suffix."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: deque[str] = deque()
        self._retained_characters = 0
        self._omitted_characters = 0

    def append(self, text: str) -> None:
        """Retain only the newest configured number of characters."""
        self._parts.append(text)
        self._retained_characters += len(text)
        excess = self._retained_characters - self._limit
        while excess > 0:
            oldest = self._parts[0]
            if len(oldest) <= excess:
                self._parts.popleft()
                removed = len(oldest)
            else:
                self._parts[0] = oldest[excess:]
                removed = excess
            self._retained_characters -= removed
            self._omitted_characters += removed
            excess -= removed

    def result(self) -> _RetainedOutput:
        """Return the retained suffix and exact number of discarded characters."""
        return _RetainedOutput("".join(self._parts), self._omitted_characters)


class _SinkFailures:
    """Retain the first concurrent sink failure."""

    def __init__(self, lock: Lock) -> None:
        self._lock = lock
        self._errors: list[Exception] = []

    @property
    def first(self) -> Exception | None:
        """Return the first recorded failure after delivery workers settle."""
        return self._errors[0] if self._errors else None

    def record(self, error: Exception) -> None:
        """Keep the first failure without stopping either pipe drain."""
        with self._lock:
            if not self._errors:
                self._errors.append(error)


class _StreamOutput:
    """Capture one pipe and deliver its segments without blocking the drain."""

    def __init__(
        self,
        threads: Threads,
        stream: TextIOBase,
        sink: Callable[[str], None] | None,
        *,
        max_output_characters: int,
        sink_failures: _SinkFailures,
    ) -> None:
        self._stream = stream
        self._sink = sink
        self._tail = _OutputTail(max_output_characters)
        self._pending: deque[str] = deque()
        self._condition: Condition = threads.condition()
        self._input_closed = False
        self._delivery_cancelled = False
        self._sink_failures = sink_failures

    @property
    def has_delivery_worker(self) -> bool:
        """Whether this stream has a cancellable concurrent sink."""
        return isinstance(self._sink, CancellableOutputSink)

    def drain(self) -> None:
        """Own the pipe read and publish complete segments to the delivery queue."""
        try:
            while True:
                if self.has_delivery_worker:
                    with self._condition:
                        self._condition.wait_for(
                            lambda: (
                                self._delivery_cancelled
                                or len(self._pending) < _MAX_PENDING_OUTPUT_SEGMENTS
                            )
                        )
                line = self._stream.readline(_PROCESS_OUTPUT_READ_CHARACTERS)
                if not line:
                    break
                with self._condition:
                    self._tail.append(line)
                    if self.has_delivery_worker and not self._delivery_cancelled:
                        self._pending.append(line)
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._input_closed = True
                self._condition.notify_all()

    def deliver(self) -> None:
        """Forward captured segments until input ends or cleanup cancels delivery."""
        sink = self._sink
        if sink is None:
            return
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: self._delivery_cancelled or bool(self._pending) or self._input_closed
                )
                if self._delivery_cancelled:
                    return
                if not self._pending:
                    return
                data = self._pending.popleft()
                self._condition.notify_all()
            self._deliver(sink, data)

    def deliver_captured(self) -> None:
        """Replay successful output to an ordinary callback in the calling thread."""
        sink = self._sink
        if sink is None or self.has_delivery_worker:
            return
        retained = self._tail.result().text
        if retained:
            self._deliver(sink, retained)

    def _deliver(self, sink: Callable[[str], None], data: str) -> None:
        try:
            sink(data)
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-009053 [BLE001]; a sink failure must not stop pipe ownership or later deliveries.
            self._sink_failures.record(exc)

    def cancel_delivery(self) -> None:
        """Discard queued delivery and interrupt a cancellable active sink."""
        with self._condition:
            self._delivery_cancelled = True
            self._pending.clear()
            self._condition.notify_all()
        sink = self._sink
        if isinstance(sink, CancellableOutputSink):
            with suppress(Exception):
                sink.cancel()

    def result(self) -> _RetainedOutput:
        """Return bounded capture after the drain has completed."""
        return self._tail.result()


@dataclass(frozen=True)
class JobResult:
    """Application result returned from a remote cluster."""

    status: JobStatus
    sky_exit_code: int
    remote_job_id: int
    stdout: str
    stderr: str
    cluster_name: str
    stdout_omitted_characters: int = 0
    stderr_omitted_characters: int = 0

    def __post_init__(self) -> None:
        """Reject invalid truncation metadata at the job boundary."""
        if self.stdout_omitted_characters < 0 or self.stderr_omitted_characters < 0:
            message = "SkyPilot job output omitted-character counts must be nonnegative"
            raise ValueError(message)

    @property
    def stdout_truncated(self) -> bool:
        """Whether characters before the retained stdout tail were discarded."""
        return self.stdout_omitted_characters > 0

    @property
    def stderr_truncated(self) -> bool:
        """Whether characters before the retained stderr tail were discarded."""
        return self.stderr_omitted_characters > 0


def _resource_fingerprint(resources: ResolvedSkyPilotResources) -> str:
    relevant = resources.model_dump(
        mode="json",
        exclude={"profile_name", "remote_artifact_root"},
    )
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def stable_cluster_name(run_id: str, resources: ResolvedSkyPilotResources) -> str:
    """Return a bounded name derived from run identity and effective resources."""
    component = _CLUSTER_COMPONENT.sub("-", run_id.lower()).strip("-") or "run"
    identity = f"{run_id}\0{_resource_fingerprint(resources)}".encode()
    suffix = hashlib.sha256(identity).hexdigest()[:12]
    prefix_budget = _MAX_CLUSTER_NAME - len("vibesys--") - len(suffix)
    component = component[:prefix_budget].rstrip("-") or "run"
    return f"vibesys-{component}-{suffix}"


def build_task_document(
    resources: ResolvedSkyPilotResources,
    *,
    command: Sequence[str],
    workdir: Path | None = None,
    name: str | None = None,
    use_command_prefix: bool = True,
) -> dict[str, object]:
    """Build the provider-neutral subset of one SkyPilot task document."""
    if not command:
        message = "SkyPilot task command must not be empty"
        raise ValueError(message)
    resource_document: dict[str, object] = {
        "infra": resources.infra,
        "accelerators": f"{resources.accelerator_type}:{resources.accelerators_per_node}",
    }
    if resources.cpus_per_node is not None:
        resource_document["cpus"] = resources.cpus_per_node
    if resources.remote_runtime_image is not None:
        resource_document["image_id"] = resources.remote_runtime_image
    effective_command = (
        (*resources.command_prefix, *command) if use_command_prefix else tuple(command)
    )
    document: dict[str, object] = {
        "num_nodes": resources.nodes,
        "resources": resource_document,
        "run": shlex.join(effective_command),
    }
    if name is not None:
        document["name"] = name
    if resources.infra == "slurm" or resources.infra.startswith("slurm/"):
        sbatch_options: dict[str, object] = {}
        if resources.exclusive:
            sbatch_options["exclusive"] = True
        if resources.allocation_time is not None:
            sbatch_options["time"] = resources.allocation_time
        if sbatch_options:
            document["config"] = {"slurm": {"sbatch_options": sbatch_options}}
    if workdir is not None:
        document["workdir"] = str(workdir)
    return document


class SkyPilotJobRunner:
    """Inspect, launch, use, cancel, and release named SkyPilot clusters."""

    def __init__(
        self,
        command_runner: CommandRunner | None = None,
        *,
        executable: str = "sky",
        threads: Threads | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        job_name_factory: Callable[[], str] | None = None,
    ) -> None:
        """Create a runner around an injectable external-process boundary."""
        self._threads: Threads = threads or OsThreads()
        self._command_runner = command_runner or SubprocessCommandRunner(self._threads)
        self._executable = executable
        self._poll_interval = poll_interval
        # The held life of each job this runner has read: one order per job, whichever path read it.
        self._job_lives: dict[tuple[str, str], JobLife] = {}
        self._job_name_factory = job_name_factory or (lambda: f"vibesys-job-{uuid.uuid4().hex}")

    def inspect_cluster(self, name: str, *, timeout: float = 60) -> ClusterInfo | None:
        """Return the named cluster's state, or ``None`` when it is absent."""
        result = self._control(["status", "--refresh", "--output", "json", name], timeout=timeout)
        if result.stdout_truncated:
            raise SkyPilotOutputError.truncated_control_output("status")
        payload = decode_json(
            result.stdout, _STATUS_DOCUMENT, source="sky status", max_bytes=_CONTROL_JSON_MAX_BYTES
        )
        if isinstance(payload, Unreadable):
            raise _status_error(payload)
        entries = payload if isinstance(payload, list) else payload.get("clusters")
        if not isinstance(entries, list):
            raise SkyPilotOutputError.status_missing_cluster_list()
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") != name:
                continue
            return ClusterInfo(name=name, life=cluster_life(status_text(entry.get("status"))))
        return None

    def launch(
        self,
        name: str,
        resources: ResolvedSkyPilotResources,
        *,
        timeout: float | None = None,
    ) -> ClusterInfo:
        """Launch one detached named cluster from resolved resources."""
        task = build_task_document(resources, command=("true",), use_command_prefix=False)
        with self._task_file(task) as task_path:
            self._control(
                ["launch", "-y", "-d", "-c", name, str(task_path)],
                timeout=timeout,
            )
        return ClusterInfo(name=name, life=ClusterUp())

    def ensure_cluster(
        self,
        name: str,
        resources: ResolvedSkyPilotResources,
        *,
        timeout: float | None = 300,
    ) -> ClusterInfo:
        """Reuse an active named cluster or replace an inactive one."""
        deadline = None if timeout is None else self._threads.now() + timeout
        current = self.inspect_cluster(name, timeout=self._remaining(deadline, default=60) or 60)
        if current is not None:
            match current.life:
                case ClusterUp():
                    return current
                case ClusterInitializing():
                    return self._wait_for_cluster(name, deadline)
                case ClusterUnknown(raw=raw):
                    raise SkyPilotClusterNotReadyError.unknown_status(name, raw)
                case ClusterEnded():
                    self.release(name, timeout=self._remaining(deadline, default=60) or 60)
                case _:
                    assert_never(current.life)
        return self.launch(name, resources, timeout=self._remaining(deadline))

    def run(  # noqa: PLR0913  # lint-waiver: LW-009055 [PLR0913]; streaming callbacks and resume identity are independent controls of one public job operation.
        self,
        cluster_name: str,
        resources: ResolvedSkyPilotResources,
        *,
        workdir: Path,
        command: Sequence[str],
        timeout: float | None = None,
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
        job_started: Callable[[int], None] | None = None,
        job_name: str | None = None,
        existing_job_id: int | None = None,
        log_tail: int = 0,
    ) -> JobResult:
        """Submit a detached task, identify it, then stream logs to completion."""
        if log_tail < 0:
            message = "SkyPilot log tail must be nonnegative"
            raise ValueError(message)
        deadline = None if timeout is None else self._threads.now() + timeout
        resolved_job_name = job_name or self._job_name_factory()
        if existing_job_id is None:
            task = build_task_document(
                resources, command=command, workdir=workdir, name=resolved_job_name
            )
            with self._task_file(task) as task_path:
                self._control(
                    ["exec", "-d", cluster_name, str(task_path)],
                    timeout=self._remaining(deadline),
                )
            job_id = self._discover_job_id(cluster_name, resolved_job_name, deadline)
        else:
            job_id = existing_job_id
        if job_started is not None:
            job_started(job_id)
        result = self._invoke(
            [
                self._executable,
                "logs",
                cluster_name,
                str(job_id),
                "--tail",
                str(log_tail),
            ],
            timeout=self._remaining(deadline),
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
        verdict = logs_verdict(result.returncode)
        match verdict:
            case Finished(status=status):
                pass
            case Indeterminate(returncode=code):
                raise SkyPilotJobStateError.logs_indicate_unknown_state(code, job_id)
            case LogsFailed(returncode=code):
                raise SkyPilotControlPlaneError.logs_failed(code)
            case _:
                assert_never(verdict)
        return JobResult(
            status=status,
            sky_exit_code=result.returncode,
            remote_job_id=job_id,
            stdout=result.stdout,
            stderr=result.stderr,
            cluster_name=cluster_name,
            stdout_omitted_characters=result.stdout_omitted_characters,
            stderr_omitted_characters=result.stderr_omitted_characters,
        )

    def query_job(
        self,
        cluster_name: str,
        *,
        job_name: str,
        job_id: int | None = None,
        timeout: float = 60,
    ) -> RemoteJobInfo | None:
        """Find exactly one job by caller-owned name and optional persisted ID."""
        result = self._control(["queue", cluster_name, "--output", "json"], timeout=timeout)
        if result.stdout_truncated:
            raise SkyPilotOutputError.truncated_control_output("queue")
        records = self._queue_records(result.stdout, cluster_name)
        matches = [record for record in records if record.get("job_name") == job_name]
        if not matches:
            return None
        if len(matches) != 1:
            raise SkyPilotOutputError.duplicate_jobs(job_name)
        record = matches[0]
        remote_id = record.get("job_id")
        if not isinstance(remote_id, int) or isinstance(remote_id, bool):
            raise SkyPilotOutputError.invalid_job_id()
        if job_id is not None and remote_id != job_id:
            raise SkyPilotOutputError.job_id_changed(job_name, job_id, remote_id)
        raw = status_text(record.get("status"))
        reading = job_reading(raw)
        if isinstance(reading, JobUnknown):
            raise SkyPilotOutputError.unknown_job_status(reading.raw)
        key = (cluster_name, job_name)
        life = advance(self._job_lives.get(key, INITIAL_JOB_LIFE), reading)
        self._job_lives[key] = life
        return RemoteJobInfo(remote_id, job_name, life)

    def _wait_for_cluster(self, name: str, deadline: float | None) -> ClusterInfo:
        while True:
            remaining = self._remaining(deadline, default=60)
            current = self.inspect_cluster(name, timeout=remaining or 60)
            if current is not None and isinstance(current.life, ClusterUp):
                return current
            if current is None or not isinstance(current.life, ClusterInitializing):
                observed = "absent" if current is None else describe_cluster(current.life)
                raise SkyPilotClusterNotReadyError.became_unavailable(name, observed)
            self._pause(deadline, f"SkyPilot cluster {name!r} remained INIT")

    def _discover_job_id(self, cluster_name: str, job_name: str, deadline: float | None) -> int:
        discovery_deadline = self._threads.now() + _JOB_DISCOVERY_TIMEOUT_SECONDS
        if deadline is not None:
            discovery_deadline = min(discovery_deadline, deadline)
        while True:
            found = self.query_job(
                cluster_name,
                job_name=job_name,
                timeout=self._remaining(discovery_deadline, default=60) or 60,
            )
            if found is not None:
                return found.job_id
            self._pause(discovery_deadline, f"SkyPilot did not expose job {job_name!r}")

    @staticmethod
    def _queue_records(stdout: str, cluster_name: str) -> list[dict[str, object]]:
        payload = decode_json(
            stdout, _QUEUE_DOCUMENT, source="sky queue", max_bytes=_CONTROL_JSON_MAX_BYTES
        )
        if isinstance(payload, Unreadable):
            raise _queue_error(payload)
        records = payload.get(cluster_name)
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise SkyPilotOutputError.queue_missing_job_list()
        return records

    def _pause(self, deadline: float | None, detail: str) -> None:
        if deadline is not None:
            remaining = deadline - self._threads.now()
            if remaining <= 0:
                raise SkyPilotTimeoutError.waiting_timed_out(detail)
            self._threads.sleep(min(self._poll_interval, remaining))
        else:
            self._threads.sleep(self._poll_interval)

    def _remaining(self, deadline: float | None, *, default: float | None = None) -> float | None:
        if deadline is None:
            return default
        remaining = deadline - self._threads.now()
        if remaining <= 0:
            raise SkyPilotTimeoutError.operation_deadline_exceeded()
        return remaining

    def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:
        """Cancel a remote job by numeric SkyPilot job identifier."""
        self._control(["cancel", "-y", cluster_name, str(job_id)], timeout=timeout)

    def release(self, cluster_name: str, *, timeout: float = 60) -> None:
        """Tear down a named cluster."""
        self._control(["down", "-y", cluster_name], timeout=timeout)

    def _control(self, arguments: Sequence[str], *, timeout: float | None) -> ProcessResult:
        result = self._invoke([self._executable, *arguments], timeout=timeout)
        if result.returncode != 0:
            operation = arguments[0] if arguments else "command"
            raise SkyPilotControlPlaneError.operation_failed(operation, result.returncode)
        return result

    def _invoke(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None,
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        try:
            return self._command_runner.run(
                argv,
                timeout=timeout,
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
            )
        except FileNotFoundError as exc:
            raise SkyPilotCLIError.executable_not_found(self._executable) from exc
        except ProcessTimeoutError as exc:
            raise SkyPilotTimeoutError.command_timed_out(timeout) from exc

    @staticmethod
    @contextmanager
    def _task_file(document: dict[str, object]) -> Generator[Path]:
        with tempfile.TemporaryDirectory(prefix="vibesys-skypilot-") as directory:
            path = Path(directory) / "task.yaml"
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            yield path
