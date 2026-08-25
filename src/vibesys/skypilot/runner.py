"""Focused external-CLI adapter for SkyPilot cluster jobs."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys.skypilot.config import ResolvedSkyPilotResources

_CLUSTER_COMPONENT = re.compile(r"[^a-z0-9-]+")
_ANSI_ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MAX_CLUSTER_NAME = 28
_JOB_DISCOVERY_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 2.0
_SKY_JOB_FAILED = 100
_SKY_JOB_NOT_FINISHED = 101
_SKY_JOB_NOT_FOUND = 102
_SKY_JOB_CANCELLED = 103


class SkyPilotCLIError(RuntimeError):
    """Base error for the SkyPilot external process boundary."""


class SkyPilotTimeoutError(SkyPilotCLIError):
    """Raised when a SkyPilot control-plane command times out."""


class SkyPilotControlPlaneError(SkyPilotCLIError):
    """Raised when a SkyPilot lifecycle command exits unsuccessfully."""


class SkyPilotOutputError(SkyPilotCLIError):
    """Raised when SkyPilot emits malformed machine-readable output."""


class SkyPilotClusterNotReadyError(SkyPilotCLIError):
    """Raised when a cluster does not reach a reusable state in time."""


class SkyPilotJobStateError(SkyPilotCLIError):
    """Raised when SkyPilot reports an invalid or indeterminate remote job state."""


class ClusterStatus(StrEnum):
    """Observed lifecycle state of a named cluster."""

    INIT = "INIT"
    UP = "UP"
    STOPPED = "STOPPED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


class JobStatus(StrEnum):
    """Outcome of a submitted application command."""

    COMPLETED = "COMPLETED"
    APPLICATION_FAILED = "APPLICATION_FAILED"
    CANCELLED = "CANCELLED"


class RemoteJobStatus(StrEnum):
    """SkyPilot queue status used for recovery decisions."""

    INIT = "INIT"
    PENDING = "PENDING"
    SETTING_UP = "SETTING_UP"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FAILED_SETUP = "FAILED_SETUP"
    FAILED_DRIVER = "FAILED_DRIVER"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ProcessResult:
    """Captured result from one external process."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


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
        """Run one command and return its captured result."""
        ...


class SubprocessCommandRunner:
    """Run one process without a shell while forwarding both output streams."""

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
        process = subprocess.Popen(  # noqa: S603  # fixed external CLI boundary
            normalized,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        sink_errors: list[Exception] = []
        sink_errors_lock = threading.Lock()

        def forward(
            stream: IO[str],
            parts: list[str],
            sink: Callable[[str], None] | None,
        ) -> None:
            while line := stream.readline():
                parts.append(line)
                if sink is not None:
                    try:
                        sink(line)
                    except Exception as exc:  # noqa: BLE001  # keep draining both pipes
                        with sink_errors_lock:
                            if not sink_errors:
                                sink_errors.append(exc)

        assert process.stdout is not None  # noqa: S101  # requested PIPE
        assert process.stderr is not None  # noqa: S101  # requested PIPE
        stdout_thread = threading.Thread(
            target=forward, args=(process.stdout, stdout_parts, stdout_sink), daemon=True
        )
        stderr_thread = threading.Thread(
            target=forward, args=(process.stderr, stderr_parts, stderr_sink), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            stdout_thread.join()
            stderr_thread.join()
        if sink_errors:
            raise sink_errors[0]
        return ProcessResult(normalized, returncode, "".join(stdout_parts), "".join(stderr_parts))


@dataclass(frozen=True)
class ClusterInfo:
    """Identity and observed lifecycle state of one cluster."""

    name: str
    status: ClusterStatus


@dataclass(frozen=True)
class JobResult:
    """Application result returned from a remote cluster."""

    status: JobStatus
    sky_exit_code: int
    remote_job_id: int
    stdout: str
    stderr: str
    cluster_name: str


@dataclass(frozen=True)
class RemoteJobInfo:
    """Recoverable identity and queue state for one remote job."""

    job_id: int
    job_name: str
    status: RemoteJobStatus


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
        raise ValueError("SkyPilot task command must not be empty")  # noqa: TRY003
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


def _decode_json_stdout(stdout: str, *, error_message: str) -> object:
    """Extract and decode the trailing ``--output json`` payload from sky CLI stdout.

    The sky CLI (0.13.0) logs informational lines to stdout ahead of JSON output
    even when ``--output json`` is requested: e.g. a "Cluster(s) not found: ..."
    notice for ``status`` on an absent cluster, or a "Fetching job queue for:
    ..." notice for ``queue``. These lines may also carry ANSI styling. The JSON
    payload itself is always written last, as one ``json.dumps`` block starting
    at the first character of a line with ``[`` or ``{`` and running to the end
    of stdout, so this locates that block and decodes it while ignoring any
    banner lines before it.
    """
    lines = stdout.splitlines()
    payload_start = next(
        (
            index
            for index, line in enumerate(lines)
            if _ANSI_ESCAPE_SEQUENCE.sub("", line).lstrip()[:1] in ("[", "{")
        ),
        None,
    )
    if payload_start is None:
        raise SkyPilotOutputError(error_message)
    payload_text = "\n".join(_ANSI_ESCAPE_SEQUENCE.sub("", line) for line in lines[payload_start:])
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise SkyPilotOutputError(error_message) from exc


class SkyPilotJobRunner:
    """Inspect, launch, use, cancel, and release named SkyPilot clusters."""

    def __init__(  # noqa: PLR0913
        self,
        command_runner: CommandRunner | None = None,
        *,
        executable: str = "sky",
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        job_name_factory: Callable[[], str] | None = None,
    ) -> None:
        """Create a runner around an injectable external-process boundary."""
        self._command_runner = command_runner or SubprocessCommandRunner()
        self._executable = executable
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_interval = poll_interval
        self._job_name_factory = job_name_factory or (lambda: f"vibesys-job-{uuid.uuid4().hex}")

    def inspect_cluster(self, name: str, *, timeout: float = 60) -> ClusterInfo | None:
        """Return the named cluster's state, or ``None`` when it is absent."""
        result = self._control(["status", "--refresh", "--output", "json", name], timeout=timeout)
        payload = _decode_json_stdout(
            result.stdout, error_message="SkyPilot status returned invalid JSON"
        )
        entries = (
            payload
            if isinstance(payload, list)
            else payload.get("clusters", [])
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(entries, list):
            raise SkyPilotOutputError(  # noqa: TRY003
                "SkyPilot status JSON must contain a cluster list"
            )
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") != name:
                continue
            raw_status = entry.get("status")
            try:
                status = ClusterStatus(str(raw_status).upper())
            except ValueError:
                status = ClusterStatus.UNKNOWN
            return ClusterInfo(name=name, status=status)
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
        return ClusterInfo(name=name, status=ClusterStatus.UP)

    def ensure_cluster(
        self,
        name: str,
        resources: ResolvedSkyPilotResources,
        *,
        timeout: float | None = 300,
    ) -> ClusterInfo:
        """Reuse an active named cluster or replace an inactive one."""
        deadline = None if timeout is None else self._monotonic() + timeout
        current = self.inspect_cluster(name, timeout=self._remaining(deadline, default=60) or 60)
        if current is not None and current.status is ClusterStatus.UP:
            return current
        if current is not None and current.status is ClusterStatus.INIT:
            return self._wait_for_cluster(name, deadline)
        if current is not None and current.status is ClusterStatus.UNKNOWN:
            raise SkyPilotClusterNotReadyError(  # noqa: TRY003
                f"SkyPilot cluster {name!r} has an unknown status"
            )
        if current is not None:
            self.release(name, timeout=self._remaining(deadline, default=60) or 60)
        return self.launch(name, resources, timeout=self._remaining(deadline))

    def run(  # noqa: PLR0913
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
            raise ValueError("SkyPilot log tail must be nonnegative")  # noqa: TRY003
        deadline = None if timeout is None else self._monotonic() + timeout
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
        if result.returncode == 0:
            status = JobStatus.COMPLETED
        elif result.returncode == _SKY_JOB_FAILED:
            status = JobStatus.APPLICATION_FAILED
        elif result.returncode == _SKY_JOB_CANCELLED:
            status = JobStatus.CANCELLED
        elif result.returncode in {_SKY_JOB_NOT_FINISHED, _SKY_JOB_NOT_FOUND}:
            raise SkyPilotJobStateError(  # noqa: TRY003
                f"SkyPilot logs returned job state code {result.returncode} for job {job_id}"
            )
        else:
            raise SkyPilotControlPlaneError(  # noqa: TRY003
                f"SkyPilot logs failed with exit code {result.returncode}"
            )
        return JobResult(
            status=status,
            sky_exit_code=result.returncode,
            remote_job_id=job_id,
            stdout=result.stdout,
            stderr=result.stderr,
            cluster_name=cluster_name,
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
        records = self._queue_records(result.stdout, cluster_name)
        matches = [record for record in records if record.get("job_name") == job_name]
        if not matches:
            return None
        if len(matches) != 1:
            raise SkyPilotOutputError(  # noqa: TRY003
                f"SkyPilot queue returned duplicate jobs named {job_name!r}"
            )
        record = matches[0]
        remote_id = record.get("job_id")
        if not isinstance(remote_id, int) or isinstance(remote_id, bool):
            raise SkyPilotOutputError("SkyPilot queue returned a non-integer job ID")  # noqa: TRY003
        if job_id is not None and remote_id != job_id:
            raise SkyPilotOutputError(  # noqa: TRY003
                f"SkyPilot job {job_name!r} changed ID from {job_id} to {remote_id}"
            )
        try:
            status = RemoteJobStatus(str(record.get("status", "RUNNING")).upper())
        except ValueError as exc:
            raise SkyPilotOutputError("SkyPilot queue returned an unknown job status") from exc  # noqa: TRY003
        return RemoteJobInfo(remote_id, job_name, status)

    def _wait_for_cluster(self, name: str, deadline: float | None) -> ClusterInfo:
        while True:
            remaining = self._remaining(deadline, default=60)
            current = self.inspect_cluster(name, timeout=remaining or 60)
            if current is not None and current.status is ClusterStatus.UP:
                return current
            if current is None or current.status is not ClusterStatus.INIT:
                observed = "absent" if current is None else current.status.value
                raise SkyPilotClusterNotReadyError(  # noqa: TRY003
                    f"SkyPilot cluster {name!r} became {observed} while initializing"
                )
            self._pause(deadline, f"SkyPilot cluster {name!r} remained INIT")

    def _discover_job_id(self, cluster_name: str, job_name: str, deadline: float | None) -> int:
        discovery_deadline = self._monotonic() + _JOB_DISCOVERY_TIMEOUT_SECONDS
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
        payload = _decode_json_stdout(stdout, error_message="SkyPilot queue returned invalid JSON")
        records = payload.get(cluster_name) if isinstance(payload, dict) else None
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise SkyPilotOutputError(  # noqa: TRY003
                "SkyPilot queue JSON must map the cluster name to a job list"
            )
        return records

    def _cancel_or_release(self, cluster_name: str, job_id: int) -> None:
        try:
            self.cancel(cluster_name, job_id)
        except SkyPilotCLIError:
            self.release(cluster_name)

    def _pause(self, deadline: float | None, detail: str) -> None:
        if deadline is not None:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SkyPilotTimeoutError(detail)
            self._sleep(min(self._poll_interval, remaining))
        else:
            self._sleep(self._poll_interval)

    def _remaining(self, deadline: float | None, *, default: float | None = None) -> float | None:
        if deadline is None:
            return default
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise SkyPilotTimeoutError("SkyPilot operation exceeded its deadline")  # noqa: TRY003
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
            raise SkyPilotControlPlaneError(  # noqa: TRY003
                f"SkyPilot {operation} failed with exit code {result.returncode}"
            )
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
            raise SkyPilotCLIError(  # noqa: TRY003
                f"SkyPilot executable {self._executable!r} was not found"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SkyPilotTimeoutError(  # noqa: TRY003
                f"SkyPilot command timed out after {timeout} seconds"
            ) from exc

    @staticmethod
    def _task_file(document: dict[str, object]):  # noqa: ANN205
        class _TaskFile:
            def __init__(self, value: dict[str, object]) -> None:
                self._value = value
                self._temporary: tempfile.TemporaryDirectory[str] | None = None

            def __enter__(self) -> Path:
                self._temporary = tempfile.TemporaryDirectory(prefix="vibesys-skypilot-")
                path = Path(self._temporary.name) / "task.yaml"
                path.write_text(yaml.safe_dump(self._value, sort_keys=False), encoding="utf-8")
                return path

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                if self._temporary is not None:
                    self._temporary.cleanup()

        return _TaskFile(document)
