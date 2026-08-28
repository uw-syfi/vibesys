"""Host-owned Unix-socket bridge for sequential SkyPilot evaluations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vibesys.skypilot.protocol import (
    AckedFrame,
    ArtifactFrame,
    ErrorFrame,
    EvaluationRequest,
    OutputFrame,
    ResultFrame,
    decode_ack,
    decode_request,
    encode_message,
)
from vibesys.skypilot.recovery import (
    ArtifactRecord,
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationPhase,
    InvocationProvenance,
    InvocationRecord,
    InvocationResultRecord,
)
from vibesys.skypilot.runner import (
    ClusterStatus,
    RemoteJobInfo,
    RemoteJobStatus,
    SkyPilotControlPlaneError,
    SkyPilotJobStateError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vibesys.skypilot.config import ResolvedSkyPilotResources
    from vibesys.skypilot.runner import SkyPilotJobRunner
    from vs_project import StateNamespace

_MAX_REQUEST_BYTES = 4096
_OUTPUT_CHUNK_CHARACTERS = 64 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_STAGED_FILES = 50_000
_MAX_STAGED_BYTES = 512 * 1024 * 1024
_FRAMEWORK_ARGUMENT_COUNT = 2
_SOCKET_DIR_PREFIX = "vibesys-skypilot-"
_SOCKET_FILE_NAME = "bridge.sock"
# Linux sockaddr_un.sun_path is 108 bytes including the NUL terminator; leave
# room for it so socket.bind never raises "AF_UNIX path too long".
_MAX_SOCKET_PATH_BYTES = 107
_FRAMEWORK_ARTIFACT = re.compile(r"^/tmp/vibesys-framework-benchmark-[a-zA-Z0-9._-]+\.json$")
# Non-terminal SkyPilot queue states: a job in one of these is still capable of
# producing fresh output, so its replay state must not be discarded out from
# under it. Every other status (terminal, or absent from the queue entirely)
# is safe evidence that a prior attempt is dead.
_ACTIVE_REMOTE_JOB_STATUSES = frozenset(
    {
        RemoteJobStatus.INIT,
        RemoteJobStatus.PENDING,
        RemoteJobStatus.SETTING_UP,
        RemoteJobStatus.RUNNING,
    }
)
_STAGING_EXCLUDED_NAMES = frozenset(
    {
        ".cache",
        ".env",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "agent.toml",
        "build",
        "dist",
        "node_modules",
    }
)


class _BridgeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def _allocate_socket_path(socket_root: Path | None) -> tuple[Path, Path]:
    """Create a short-lived, collision-safe directory for one bridge socket.

    The socket must live under a short runtime path, not the deep durable
    state tree, so its length stays well inside the AF_UNIX ``sun_path``
    limit regardless of how long the owning project key or run id are.
    Returns ``(socket_dir, socket_path)``; the caller owns removing
    ``socket_dir`` on close. Raises ``OSError`` naming the offending path if
    even a fresh runtime directory would still be too long.
    """
    socket_dir = Path(
        tempfile.mkdtemp(
            prefix=_SOCKET_DIR_PREFIX,
            dir=str(socket_root) if socket_root is not None else None,
        )
    )
    socket_path = socket_dir / _SOCKET_FILE_NAME
    encoded_length = len(os.fsencode(str(socket_path)))
    if encoded_length > _MAX_SOCKET_PATH_BYTES:
        shutil.rmtree(socket_dir, ignore_errors=True)
        raise OSError(  # noqa: TRY003
            f"SkyPilot bridge socket path {socket_path} is {encoded_length} bytes, "
            f"exceeding the {_MAX_SOCKET_PATH_BYTES}-byte AF_UNIX sun_path limit; "
            "set TMPDIR (or pass a shorter socket_root) to a shorter path"
        )
    return socket_dir, socket_path


class _ArtifactStream:
    def __init__(
        self,
        sink: Callable[[str], None],
        *,
        expected: bool,
        begin_marker: str,
        end_marker: str,
    ) -> None:
        self._sink = sink
        self._expected = expected
        self._begin_marker = begin_marker
        self._end_marker = end_marker
        self._capturing = False
        self._captures = 0
        self._encoded: list[str] = []
        self._encoded_characters = 0
        self._oversized = False

    def feed(self, data: str) -> None:
        for line in data.splitlines(keepends=True):
            marker = line.strip()
            if marker == self._begin_marker:
                if self._capturing or self._captures:
                    self._oversized = True
                self._capturing = True
            elif marker == self._end_marker and self._capturing:
                self._capturing = False
                self._captures += 1
            elif self._capturing:
                self._encoded.append(marker)
                self._encoded_characters += len(marker)
                if self._encoded_characters > (_MAX_ARTIFACT_BYTES * 2):
                    self._oversized = True
            else:
                self._sink(line)

    def result(self) -> bytes | None:
        if not self._expected:
            return None
        if self._capturing or self._oversized or self._captures != 1 or not self._encoded:
            raise ValueError("remote evaluator artifact was missing or invalid")  # noqa: TRY003
        try:
            data = base64.b64decode("".join(self._encoded), validate=True)
        except ValueError as exc:
            raise ValueError("remote evaluator artifact was invalid") from exc  # noqa: TRY003
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ValueError("remote evaluator artifact is too large")  # noqa: TRY003
        return data


class _ReplayInconsistentError(ValueError):
    """The durable log replay state for one invocation cannot be trusted.

    Raised only by :class:`_DecodedLogSpool`. Retrying the same replay can
    never succeed once the persisted spool and journal offsets disagree, so
    the caller treats this as evidence the prior dispatch is unrecoverable
    (see ``_run``'s handling below) rather than a transient failure.
    """


class _DecodedLogSpool:
    """Persist decoded remote stdout and suppress replayed Sky log prefixes."""

    def __init__(
        self,
        *,
        path: Path,
        journal: InvocationJournal,
        invocation_id: str,
        sink: Callable[[str], None],
    ) -> None:
        self._path = path
        self._journal = journal
        self._invocation_id = invocation_id
        self._sink = sink
        self._seen = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._persisted = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._persisted = ""
        record = self._record()
        if len(self._persisted) < record.remote_read_offset:
            raise _ReplayInconsistentError(  # noqa: TRY003
                "SkyPilot log spool is shorter than its journal offset"
            )
        if len(self._persisted) > record.remote_read_offset:
            record = self._journal.offsets(
                record,
                remote_read=len(self._persisted),
                client_delivered=record.client_delivered_offset,
            )
        if record.client_delivered_offset < len(self._persisted):
            self._sink(self._persisted[record.client_delivered_offset :])
            self._journal.offsets(
                self._record(),
                remote_read=len(self._persisted),
                client_delivered=len(self._persisted),
            )

    def feed(self, data: str) -> None:
        """Accept Sky's from-origin log stream and deliver only its new suffix."""
        overlap = min(len(data), max(0, len(self._persisted) - self._seen))
        if data[:overlap] != self._persisted[self._seen : self._seen + overlap]:
            raise _ReplayInconsistentError("SkyPilot replayed log prefix changed")  # noqa: TRY003
        suffix = data[overlap:]
        self._seen += len(data)
        if not suffix:
            return
        with self._path.open("a", encoding="utf-8") as spool:
            spool.write(suffix)
            spool.flush()
            os.fsync(spool.fileno())
        self._persisted += suffix
        record = self._journal.offsets(
            self._record(),
            remote_read=len(self._persisted),
            client_delivered=self._record().client_delivered_offset,
        )
        self._sink(suffix)
        self._journal.offsets(
            record,
            remote_read=len(self._persisted),
            client_delivered=len(self._persisted),
        )

    def finish(self) -> None:
        """Require a terminal from-origin stream to cover the durable prefix."""
        if self._seen < len(self._persisted):
            raise _ReplayInconsistentError("SkyPilot terminal log replay was truncated")  # noqa: TRY003

    def _record(self) -> InvocationRecord:
        record = self._journal.load(self._invocation_id)
        if record is None:
            raise _ReplayInconsistentError("SkyPilot invocation journal disappeared")  # noqa: TRY003
        return record


class SkyPilotBridge:
    """Serve a fixed evaluator allowlist over a host-owned Unix socket."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        runner: SkyPilotJobRunner,
        cluster_name: str,
        resources: ResolvedSkyPilotResources,
        workspace: Path,
        evaluator_package_root: Path | None,
        hidden_paths: Sequence[Path],
        commands: Mapping[str, Sequence[str]],
        benchmark_output_argument: str | None,
        state_namespace: StateNamespace,
        log: Callable[[str], None],
        max_infrastructure_retries: int = 1,
        socket_root: Path | None = None,
    ) -> None:
        """Bind fixed host policy and trusted evaluator commands.

        The socket is allocated on `start()` under a short-lived runtime
        directory (see `_allocate_socket_path`), independent of
        `state_namespace`'s durable, potentially deep path. `socket_root`
        overrides the runtime directory's parent (primarily for tests); it
        defaults to the system temp directory.
        """
        self._runner = runner
        self._cluster_name = cluster_name
        self._resources = resources
        self._workspace = workspace
        self._evaluator_package_root = evaluator_package_root
        self._hidden_paths = tuple(hidden_paths)
        self._commands = {kind: tuple(command) for kind, command in commands.items()}
        self._benchmark_output_argument = benchmark_output_argument
        self._state_namespace = state_namespace
        self._journal = InvocationJournal(state_namespace)
        self._socket_root = socket_root
        self._socket_dir: Path | None = None
        self.socket_path: Path | None = None
        self._log = log
        self._max_infrastructure_retries = max_infrastructure_retries
        self._server: _BridgeServer | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._active_jobs: set[tuple[str, int]] = set()
        self._active_lock = threading.Lock()
        self._handler_condition = threading.Condition()
        self._active_handlers = 0
        self._evaluation_lock = threading.Lock()
        self._closing = threading.Event()
        self._cluster_replaced_on_start = False
        self._locally_prepared_invocations: set[str] = set()
        self._touched_clusters = {cluster_name}

    def start(self) -> None:
        """Allocate or reuse compute, then start accepting requests."""
        if self._server is not None:
            return
        if self._closed:
            raise RuntimeError(  # noqa: TRY003
                "SkyPilot bridge cannot be restarted after close"
            )
        try:
            self._socket_dir, self.socket_path = _allocate_socket_path(self._socket_root)
            previous_cluster = self._runner.inspect_cluster(self._cluster_name)
            self._cluster_replaced_on_start = (
                previous_cluster is None
                or previous_cluster.status
                in {
                    ClusterStatus.DOWN,
                    ClusterStatus.STOPPED,
                }
            )
            self._runner.ensure_cluster(self._cluster_name, self._resources)
            bridge = self

            class Handler(socketserver.StreamRequestHandler):
                def handle(self) -> None:
                    bridge._handle(self.rfile, self.wfile, self.request)

            self._server = _BridgeServer(str(self.socket_path), Handler)
            self.socket_path.chmod(0o600)
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name=f"skypilot-bridge-{self._cluster_name}",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Stop serving, release the allocation, and remove the socket directory."""
        if self._closed:
            return
        self._closed = True
        self._closing.set()
        if self._server is not None and self._thread is not None and self._thread.is_alive():
            self._server.shutdown()
        self._cancel_active_jobs()
        if self._server is not None:
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._handler_condition:
            if not self._handler_condition.wait_for(lambda: self._active_handlers == 0, timeout=10):
                self._log("[warn] SkyPilot bridge handler did not stop before allocation release")
        self._cancel_active_jobs()
        with self._active_lock:
            touched_clusters = tuple(self._touched_clusters)
        for cluster_name in touched_clusters:
            try:
                self._runner.release(cluster_name)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[warn] SkyPilot allocation release failed: {type(exc).__name__}")
        if self._socket_dir is not None:
            shutil.rmtree(self._socket_dir, ignore_errors=True)

    def _cancel_active_jobs(self) -> None:
        """Best-effort cancel every job known at this point in teardown."""
        with self._active_lock:
            active_jobs = tuple(self._active_jobs)
        for cluster_name, job_id in active_jobs:
            try:
                self._runner.cancel(cluster_name, job_id)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[warn] SkyPilot job cancellation failed: {type(exc).__name__}")

    def _handle(self, reader: object, writer: object, connection: socket.socket) -> None:
        with self._handler_condition:
            self._active_handlers += 1
        try:
            if self._closed:
                raise ValueError("SkyPilot bridge is closing")  # noqa: TRY003, TRY301
            with self._evaluation_lock:
                if self._closed:
                    raise ValueError("SkyPilot bridge is closing")  # noqa: TRY003, TRY301
                self._handle_request(reader, writer, connection)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[error] SkyPilot bridge handler failed:\n{traceback.format_exc()}")
            self._write(writer, ErrorFrame(error=type(exc).__name__, message=str(exc)))
        finally:
            with self._handler_condition:
                self._active_handlers -= 1
                self._handler_condition.notify_all()

    def _handle_request(self, reader: object, writer: object, connection: socket.socket) -> None:
        payload = reader.readline(_MAX_REQUEST_BYTES + 1)  # type: ignore[attr-defined]
        if not payload or len(payload) > _MAX_REQUEST_BYTES or not payload.endswith(b"\n"):
            raise ValueError("invalid bridge request framing")  # noqa: TRY003
        request = decode_request(payload)
        command = self._commands.get(request.kind)
        if command is None:
            raise ValueError(  # noqa: TRY003
                f"evaluator {request.kind!r} is not configured"
            )
        if request.arguments:
            valid_dynamic_output = (
                request.kind == "benchmark"
                and len(request.arguments) == _FRAMEWORK_ARGUMENT_COUNT
                and request.arguments[0] == self._benchmark_output_argument
                and request.artifacts == (request.arguments[1],)
                and _FRAMEWORK_ARTIFACT.fullmatch(request.arguments[1]) is not None
            )
            if not valid_dynamic_output:
                raise ValueError("invalid evaluator arguments")  # noqa: TRY003
        elif request.artifacts:
            raise ValueError("invalid evaluator artifact")  # noqa: TRY003
        effective_command = (*command, *request.arguments)
        staging = self._snapshot(request.invocation_id)
        snapshot_digest = self._snapshot_digest(staging)
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "request": request.model_dump(mode="json", exclude={"invocation_id"}),
                    "command": effective_command,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing_record = self._journal.load(request.invocation_id)
        record = self._journal.prepare(request.invocation_id, request_digest, snapshot_digest)
        if existing_record is None:
            self._locally_prepared_invocations.add(request.invocation_id)
        if record.phase in {InvocationPhase.COMPLETED, InvocationPhase.ACKNOWLEDGED}:
            self._track_record_cluster(record)
            self._deliver(record, reader, writer)
            return
        self._run(request, effective_command, record, staging, reader, writer, connection)

    def _run(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        request: EvaluationRequest,
        command: tuple[str, ...],
        record: InvocationRecord,
        staging: Path,
        reader: object,
        writer: object,
        connection: socket.socket,
    ) -> None:
        self._log(f"[skypilot] running trusted {request.kind} evaluator")
        write_lock = threading.Lock()
        disconnected = threading.Event()
        finished = threading.Event()

        def monitor_disconnect() -> None:
            while not finished.wait(0.1):
                if self._closing.is_set():
                    disconnected.set()
                    return
                readable, _, _ = select.select([connection], [], [], 0)
                if not readable:
                    continue
                try:
                    if connection.recv(1, socket.MSG_PEEK):
                        continue
                except OSError:
                    pass
                disconnected.set()
                return

        monitor = threading.Thread(target=monitor_disconnect, daemon=True)
        try:
            monitor.start()
            active_record = record
            try:
                while True:
                    self._require_open_for_remote_action()
                    nonce = hashlib.sha256(
                        f"{request.invocation_id}:{active_record.job_name}".encode()
                    ).hexdigest()[:32]
                    begin_marker = f"__VIBESYS_SKYPILOT_ARTIFACT_BEGIN_{nonce}__"
                    end_marker = f"__VIBESYS_SKYPILOT_ARTIFACT_END_{nonce}__"
                    try:
                        log_spool = _DecodedLogSpool(
                            path=self._state_namespace.external_directory(
                                f"logs/{request.invocation_id}/{active_record.job_name}"
                            )
                            / "stdout",
                            journal=self._journal,
                            invocation_id=request.invocation_id,
                            sink=lambda data: self._write_output(
                                writer, "stdout", data, write_lock
                            ),
                        )
                        artifact_stream = _ArtifactStream(
                            log_spool.feed,
                            expected=bool(request.artifacts),
                            begin_marker=begin_marker,
                            end_marker=end_marker,
                        )
                        effective_command = self._with_artifact_transport(
                            command,
                            request.artifacts,
                            begin_marker=begin_marker,
                            end_marker=end_marker,
                        )
                        found = self._reconcile(active_record)
                        self._require_open_for_remote_action()
                        if found is not None and found.status is RemoteJobStatus.FAILED_DRIVER:
                            failed_cluster = active_record.active_cluster_name or self._cluster_name
                            active_record = self._retry_after_infrastructure_failure(active_record)
                            self._runner.release(failed_cluster)
                            self._ensure_current_cluster_for_work()
                            continue
                        if found is None:
                            if active_record.phase is InvocationPhase.SUBMITTING:
                                if not self._allocation_was_replaced(active_record):
                                    raise RuntimeError(  # noqa: TRY003
                                        "SkyPilot submission outcome is ambiguous; reconcile later"
                                    )
                                active_record = self._retry_after_infrastructure_failure(
                                    active_record
                                )
                                continue
                            if active_record.phase in {
                                InvocationPhase.SUBMITTED,
                                InvocationPhase.RUNNING,
                            }:
                                if not self._allocation_was_replaced(active_record):
                                    raise RuntimeError(  # noqa: TRY003
                                        "persisted SkyPilot job disappeared from an active allocation"
                                    )
                                active_record = self._retry_after_infrastructure_failure(
                                    active_record
                                )
                                continue
                            self._require_open_for_remote_action()
                            active_record = self._journal.submitting(
                                active_record,
                                self._cluster_name,
                                self._attempt_resources(),
                            )
                            self._track_cluster(self._cluster_name)
                        self._require_open_for_remote_action()
                        result = self._runner.run(
                            active_record.active_cluster_name or self._cluster_name,
                            self._resources,
                            workdir=staging,
                            command=effective_command,
                            stdout_sink=artifact_stream.feed,
                            stderr_sink=lambda data: self._write_output(
                                writer, "stderr", data, write_lock
                            ),
                            job_started=lambda job_id, invocation=active_record: self._job_started(
                                invocation,
                                job_id,
                                invocation.active_cluster_name or self._cluster_name,
                                disconnected,
                            ),
                            job_name=active_record.job_name,
                            existing_job_id=found.job_id if found is not None else None,
                        )
                        log_spool.finish()
                        break
                    except (SkyPilotControlPlaneError, SkyPilotJobStateError):
                        latest = self._journal.load(request.invocation_id) or active_record
                        attempted_cluster = latest.active_cluster_name or self._cluster_name
                        cluster = self._runner.inspect_cluster(attempted_cluster)
                        allocation_expired = cluster is None or cluster.status in {
                            ClusterStatus.DOWN,
                            ClusterStatus.STOPPED,
                        }
                        if not allocation_expired:
                            raise
                        self._require_open_for_remote_action()
                        active_record = self._recover_after_allocation_loss(latest)
                        self._ensure_current_cluster_for_work()
                    except _ReplayInconsistentError:
                        latest = self._journal.load(request.invocation_id) or active_record
                        found = self._reconcile(latest)
                        if found is not None and found.status in _ACTIVE_REMOTE_JOB_STATUSES:
                            # The prior attempt is still producing output; the
                            # local replay state is corrupt for an unrelated
                            # reason, so surface the error instead of
                            # discarding a job that could still complete.
                            raise
                        self._require_open_for_remote_action()
                        active_record = self._recover_after_allocation_loss(latest)
                        self._ensure_current_cluster_for_work()
            finally:
                finished.set()
                monitor.join(timeout=1)
            with self._active_lock:
                self._active_jobs.discard((result.cluster_name, result.remote_job_id))
            artifact = artifact_stream.result() if result.status.value == "COMPLETED" else None
            latest = self._journal.load(request.invocation_id) or record
            attempt_resources = latest.attempt_resources
            if attempt_resources is None:
                raise ValueError("completed invocation is missing attempt resources")  # noqa: TRY003
            artifact_record = (
                ArtifactRecord.create(request.artifacts[0], artifact)
                if artifact is not None
                else None
            )
            terminal = InvocationResultRecord(
                status=result.status.value,
                sky_exit_code=result.sky_exit_code,
                artifact=artifact_record,
                provenance=InvocationProvenance(
                    profile_name=attempt_resources.profile_name,
                    infra=attempt_resources.infra,
                    cluster_name=result.cluster_name,
                    job_name=latest.job_name,
                    remote_job_id=result.remote_job_id,
                    attempt=latest.attempt,
                    accelerator_type=attempt_resources.accelerator_type,
                    nodes=attempt_resources.nodes,
                    accelerators_per_node=attempt_resources.accelerators_per_node,
                    runtime_image=attempt_resources.runtime_image,
                ),
            )
            completed = self._journal.completed(latest, terminal)
            self._deliver(completed, reader, writer, write_lock)
        finally:
            finished.set()

    def _retry_after_infrastructure_failure(self, record: InvocationRecord) -> InvocationRecord:
        """Create one evidence-backed retry while enforcing a restart-stable bound."""
        if record.attempt >= 1 + self._max_infrastructure_retries:
            raise RuntimeError("SkyPilot infrastructure retry limit was exhausted")  # noqa: TRY003
        return self._journal.retry(record)

    def _recover_after_allocation_loss(self, record: InvocationRecord) -> InvocationRecord:
        """Keep an unsubmitted request, or advance a remotely attempted request."""
        if record.phase is InvocationPhase.PREPARED:
            return record
        return self._retry_after_infrastructure_failure(record)

    def _allocation_was_replaced(self, record: InvocationRecord) -> bool:
        """Return whether the persisted attempt's allocation is provably gone."""
        cluster_name = record.active_cluster_name or self._cluster_name
        if (
            cluster_name == self._cluster_name
            and self._cluster_replaced_on_start
            and record.invocation_id not in self._locally_prepared_invocations
        ):
            return True
        cluster = self._runner.inspect_cluster(cluster_name)
        return cluster is None or cluster.status in {ClusterStatus.DOWN, ClusterStatus.STOPPED}

    def _reconcile(self, record: InvocationRecord) -> RemoteJobInfo | None:
        """Attach to an exact prior job before any new submission."""
        cluster_name = record.active_cluster_name or self._cluster_name
        self._track_cluster(cluster_name)
        return self._runner.query_job(
            cluster_name,
            job_name=record.job_name,
            job_id=record.remote_job_id,
        )

    def _track_record_cluster(self, record: InvocationRecord) -> None:
        """Retain allocation ownership carried by a persisted invocation."""
        cluster_name = record.active_cluster_name
        if cluster_name is None and record.result is not None:
            cluster_name = record.result.provenance.cluster_name
        if cluster_name is not None:
            self._track_cluster(cluster_name)

    def _track_cluster(self, cluster_name: str) -> None:
        with self._active_lock:
            self._touched_clusters.add(cluster_name)

    def _require_open_for_remote_action(self) -> None:
        if self._closing.is_set():
            raise RuntimeError("SkyPilot bridge is closing")  # noqa: TRY003

    def _ensure_current_cluster_for_work(self) -> None:
        """Ensure compute unless teardown started, cleaning up a racing launch."""
        self._require_open_for_remote_action()
        self._runner.ensure_cluster(self._cluster_name, self._resources)
        if not self._closing.is_set():
            return
        try:
            self._runner.release(self._cluster_name)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[warn] SkyPilot allocation release failed: {type(exc).__name__}")
        self._require_open_for_remote_action()

    def _attempt_resources(self) -> AttemptResourcesRecord:
        """Freeze the effective resources used by the next submission attempt."""
        return AttemptResourcesRecord(
            profile_name=self._resources.profile_name,
            infra=self._resources.infra,
            accelerator_type=self._resources.accelerator_type,
            nodes=self._resources.nodes,
            accelerators_per_node=self._resources.accelerators_per_node,
            runtime_image=self._resources.remote_runtime_image,
        )

    def _deliver(
        self,
        record: InvocationRecord,
        reader: object,
        writer: object,
        lock: threading.Lock | None = None,
    ) -> None:
        """Replay a durable terminal payload and persist explicit acknowledgement."""
        result = record.result
        if result is None:
            raise ValueError("terminal invocation is missing its result")  # noqa: TRY003
        if result.artifact is not None:
            artifact = result.artifact
            self._write(
                writer,
                ArtifactFrame(
                    path=artifact.path,
                    size=artifact.size,
                    sha256=artifact.sha256,
                    data_base64=artifact.data_base64,
                ),
                lock,
            )
        self._write(
            writer,
            ResultFrame(
                status=result.status,
                sky_exit_code=result.sky_exit_code,
                remote_job_id=result.provenance.remote_job_id,
            ),
            lock,
        )
        payload = reader.readline(_MAX_REQUEST_BYTES + 1)  # type: ignore[attr-defined]
        acknowledgement = decode_ack(payload)
        if acknowledgement.invocation_id != record.invocation_id:
            raise ValueError("acknowledgement invocation mismatch")  # noqa: TRY003
        if record.phase is InvocationPhase.COMPLETED:
            record = self._journal.acknowledge(record)
        self._write(
            writer,
            AckedFrame(invocation_id=record.invocation_id),
            lock,
        )

    def _snapshot(self, invocation_id: str) -> Path:
        """Create or reuse the immutable machine-local input snapshot."""
        root = self._state_namespace.external_directory(f"snapshots/{invocation_id}")
        ready = root / ".ready"
        if ready.exists():
            return root
        if root.exists():
            shutil.rmtree(root)
        root.parent.mkdir(parents=True, exist_ok=True)
        staging = root.parent / f".{invocation_id}.staging-{secrets.token_hex(8)}"
        staging.mkdir()
        try:
            self._validate_workspace_symlinks()
            self._stage_workspace(staging)
            if self._evaluator_package_root is not None:
                shutil.copytree(
                    self._evaluator_package_root,
                    staging / ".vibesys-evaluator-package",
                    symlinks=True,
                )
            (staging / ".ready").write_text("1\n", encoding="utf-8")
            staging.replace(root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return root

    @staticmethod
    def _snapshot_digest(root: Path) -> str:
        """Hash the staged path names, symlink targets, and regular-file contents."""
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            if path.is_symlink():
                target = str(path.readlink()).encode()
                digest.update(b"L")
                digest.update(len(target).to_bytes(8, "big"))
                digest.update(target)
            elif path.is_file():
                digest.update(b"F")
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
            elif path.is_dir():
                digest.update(b"D")
            else:
                raise ValueError("snapshot contains an unsupported file type")  # noqa: TRY003
        return digest.hexdigest()

    @staticmethod
    def _with_artifact_transport(
        command: tuple[str, ...],
        artifacts: tuple[str, ...],
        *,
        begin_marker: str,
        end_marker: str,
    ) -> tuple[str, ...]:
        if not artifacts:
            return command
        path = shlex.quote(artifacts[0])
        script = "\n".join(
            [
                f"rm -f -- {path}",
                shlex.join(command),
                "status=$?",
                f'if [ -f {path} ] && [ "$(wc -c < {path})" -le {_MAX_ARTIFACT_BYTES} ]; then',
                f"  printf '\\n{begin_marker}\\n'",
                f"  base64 < {path} | tr -d '\\n'",
                f"  printf '\\n{end_marker}\\n'",
                "fi",
                'exit "$status"',
            ]
        )
        return ("sh", "-c", script)

    def _job_started(
        self,
        record: InvocationRecord,
        job_id: int,
        cluster_name: str,
        disconnected: threading.Event,
    ) -> None:
        self._journal.submitted(record, job_id, cluster_name)
        with self._active_lock:
            self._active_jobs.add((cluster_name, job_id))
            self._touched_clusters.add(cluster_name)
        if self._closing.is_set():
            try:
                self._runner.cancel(cluster_name, job_id)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[warn] SkyPilot job cancellation failed: {type(exc).__name__}")
            self._require_open_for_remote_action()
        if disconnected.is_set():
            self._log(
                f"[skypilot] client disconnected; invocation {record.invocation_id} "
                "remains recoverable"
            )

    def _validate_workspace_symlinks(self) -> None:
        root = self._workspace.resolve()
        for path in self._workspace.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                raise ValueError("workspace symlink escapes the project")  # noqa: TRY003

    def _stage_workspace(self, staging: Path) -> None:
        hidden = frozenset(self._hidden_paths)
        ignored = self._gitignored_paths(self._workspace)
        file_count = 0
        total_bytes = 0
        for current_text, directory_names, file_names in os.walk(
            self._workspace, followlinks=False
        ):
            current = Path(current_text)
            relative_parent = current.relative_to(self._workspace)
            directory_names[:] = [
                name
                for name in directory_names
                if not self._excluded(relative_parent / name, hidden, ignored)
            ]
            for name in (*directory_names, *file_names):
                relative = relative_parent / name
                if self._excluded(relative, hidden, ignored):
                    continue
                path = current / name
                if path.is_symlink():
                    continue
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ValueError("workspace contains a special file")  # noqa: TRY003
                file_count += 1
                total_bytes += path.stat().st_size
                if file_count > _MAX_STAGED_FILES or total_bytes > _MAX_STAGED_BYTES:
                    raise ValueError(  # noqa: TRY003
                        "workspace exceeds remote staging limits even after excluding "
                        "Git-ignored paths (e.g. build caches); remove large tracked "
                        "files or untrack them instead"
                    )

        def ignore(current_text: str, names: list[str]) -> set[str]:
            parent = Path(current_text).relative_to(self._workspace)
            return {name for name in names if self._excluded(parent / name, hidden, ignored)}

        shutil.copytree(
            self._workspace,
            staging,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=ignore,
        )

    @staticmethod
    def _gitignored_paths(workspace: Path) -> frozenset[tuple[str, ...]]:
        """Return workspace-relative paths Git ignores, via one `ls-files` call.

        Untracked, Git-ignored paths (build caches, dependency directories,
        ...) are excluded from remote staging so they cannot overflow
        `_MAX_STAGED_BYTES`; a Git-tracked file is never returned here (only
        `--others` paths are considered), so tracked files are always staged.
        Falls back to no additional exclusions (empty result) when `git` is
        unavailable or `workspace` is not a Git repository, so staging still
        works for non-Git workspaces.
        """
        try:
            result = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "git",
                    "-C",
                    str(workspace),
                    "ls-files",
                    "--others",
                    "--ignored",
                    "--exclude-standard",
                    "--directory",
                    "-z",
                ],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return frozenset()
        if result.returncode != 0:
            return frozenset()
        return frozenset(
            Path(os.fsdecode(raw).rstrip("/")).parts for raw in result.stdout.split(b"\0") if raw
        )

    @staticmethod
    def _excluded(
        relative: Path,
        hidden: frozenset[Path],
        ignored: frozenset[tuple[str, ...]] = frozenset(),
    ) -> bool:
        local_state_or_logs = relative.parts[:2] in {
            (".vibesys", "logs"),
            (".vibesys", "state"),
        }
        parts = relative.parts
        gitignored = any(parts[:index] in ignored for index in range(1, len(parts) + 1))
        return (
            relative.name in _STAGING_EXCLUDED_NAMES
            or relative.name.startswith(".env")
            or local_state_or_logs
            or gitignored
            or any(relative == path or relative.is_relative_to(path) for path in hidden)
        )

    @classmethod
    def _write_output(
        cls,
        writer: object,
        stream: Literal["stdout", "stderr"],
        data: str,
        lock: threading.Lock,
    ) -> None:
        for start in range(0, len(data), _OUTPUT_CHUNK_CHARACTERS):
            cls._write(
                writer,
                OutputFrame(
                    type=stream,
                    data=data[start : start + _OUTPUT_CHUNK_CHARACTERS],
                ),
                lock,
            )

    @staticmethod
    def _write(
        writer: object,
        message: OutputFrame | ArtifactFrame | ResultFrame | AckedFrame | ErrorFrame,
        lock: threading.Lock | None = None,
    ) -> None:
        context = lock or threading.Lock()
        with context:
            writer.write(encode_message(message))  # type: ignore[attr-defined]
            writer.flush()  # type: ignore[attr-defined]
