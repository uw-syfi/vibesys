"""Host-owned Unix-socket bridge for sequential SkyPilot evaluations."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vs_project.api import validate_socket_path
from vs_sandbox.skypilot_protocol import (
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
from vs_sandbox.skypilot_recovery import (
    ArtifactRecord,
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationPhase,
    InvocationProvenance,
    InvocationRecord,
    InvocationResultRecord,
)
from vs_sandbox.skypilot_runner import (
    CancellableOutputSink,
    SkyPilotControlPlaneError,
    SkyPilotJobStateError,
)
from vs_sandbox.skypilot_states import (
    JobStatus,
    RemoteJobInfo,
    allocation_gone,
    is_driver_failure,
)
from vs_sim.api import CloseOnce, NothingLeft, OsThreads, Released, UnixNetwork

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vs_project.api import StateNamespace
    from vs_sandbox.skypilot_config import ResolvedSkyPilotResources
    from vs_sandbox.skypilot_runner import SkyPilotJobRunner
    from vs_sim.api import Connection, Event, Listener, Lock, Network, Threads, Worker

_MAX_REQUEST_BYTES = 4096
_SEND_BOUND_SECONDS = 30.0
_OUTPUT_CHUNK_CHARACTERS = 64 * 1024
_LOG_SPOOL_READ_CHARACTERS = 64 * 1024
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_STAGED_FILES = 50_000
_MAX_STAGED_BYTES = 512 * 1024 * 1024
_FRAMEWORK_ARGUMENT_COUNT = 2
_FRAMEWORK_EVALUATOR_ARGV0 = "vibesys-framework-evaluator"
_FRAMEWORK_ARTIFACT = re.compile(r"^/tmp/vibesys-framework-benchmark-[a-zA-Z0-9._-]+\.json$")
_STAGING_EXCLUDED_NAMES = frozenset(
    {
        ".cache",
        ".bin",
        ".env",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pip",
        ".pytest_cache",
        ".ruff_cache",
        ".skyignore",
        ".tox",
        ".uv-cache",
        ".venv",
        ".vibesys-evaluator-package",
        ".vibesys-evaluator-toolchains",
        ".vibesys-evaluator-tools",
        "__pycache__",
        "agent.toml",
        "build",
        "dist",
        "node_modules",
    }
)


class _Wire:
    """Line-framed reads and whole-frame writes over one connection."""

    def __init__(self, connection: Connection, log: Callable[[str], None]) -> None:
        self._connection = connection
        self._log = log
        self._buffer = b""

    def has_room(self, limit: int) -> bool:
        """Whether fewer than ``limit`` bytes are buffered."""
        return len(self._buffer) < limit

    def read_ahead(self, limit: int, timeout: float) -> bool:
        """Buffer what the peer sends within ``timeout``, up to ``limit`` bytes; ``False`` once it hung up.

        Reads rather than peeks: a peer that closed with bytes still unread is not
        at end of stream, so only reading past those bytes reveals the hangup.
        What is read stays buffered for ``readline``; check ``has_room`` first so
        a flooding peer cannot grow the buffer past ``limit``.
        """
        try:
            chunk = self._connection.recv(limit - len(self._buffer), timeout)
        except TimeoutError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            return False
        self._buffer += chunk
        return True

    def readline(self, limit: int) -> bytes:
        """Up to ``limit`` bytes, ending at the first newline; fewer when the peer closes first."""
        while b"\n" not in self._buffer[:limit] and len(self._buffer) < limit:
            chunk = self._connection.recv(limit - len(self._buffer))
            if not chunk:
                break
            self._buffer += chunk
        end = self._buffer.find(b"\n") + 1
        if not 0 < end <= limit:
            end = min(limit, len(self._buffer))
        line, self._buffer = self._buffer[:end], self._buffer[end:]
        return line

    def write(self, data: bytes) -> None:
        """Send all of ``data``, or disconnect a client that read nothing for ``_SEND_BOUND_SECONDS``.

        Raises:
            BrokenPipeError: the client stopped reading; the connection is closed, so the
                request ends as if the client had hung up (part of ``data`` may have gone).
        """
        try:
            self._connection.send(data, _SEND_BOUND_SECONDS)
        except TimeoutError as exc:
            self._log(
                f"[warn] SkyPilot bridge client read nothing for {_SEND_BOUND_SECONDS:g} s; "
                "disconnecting it"
            )
            self._connection.close()
            message = "the SkyPilot bridge client stopped reading"
            raise BrokenPipeError(message) from exc


class _BridgeOutputSink:
    """Serialize one stream and let runner cleanup interrupt a blocked socket write."""

    def __init__(
        self,
        writer: _Wire,
        connection: Connection,
        stream: Literal["stdout", "stderr"],
        lock: Lock,
    ) -> None:
        self._writer = writer
        self._connection = connection
        self._stream = stream
        self._lock = lock

    def __call__(self, data: str) -> None:
        for start in range(0, len(data), _OUTPUT_CHUNK_CHARACTERS):
            frame = OutputFrame(
                type=self._stream,
                data=data[start : start + _OUTPUT_CHUNK_CHARACTERS],
            )
            with self._lock:
                self._writer.write(encode_message(frame))

    def cancel(self) -> None:
        """Close the request connection to wake an active Unix ``sendall``."""
        self._connection.close()


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
                self._encoded_characters += len(marker)
                if self._encoded_characters > (_MAX_ARTIFACT_BYTES * 2):
                    self._oversized = True
                    self._encoded.clear()
                elif not self._oversized:
                    self._encoded.append(marker)
            else:
                self._sink(line)

    def __call__(self, data: str) -> None:
        """Accept a segment from the command runner."""
        self.feed(data)

    def cancel(self) -> None:
        """Propagate delivery cancellation to the underlying transport sink."""
        if isinstance(self._sink, CancellableOutputSink):
            self._sink.cancel()

    def result(self) -> bytes | None:
        if not self._expected:
            return None
        if self._capturing or self._oversized or self._captures != 1 or not self._encoded:
            message = "remote evaluator artifact was missing or invalid"
            raise ValueError(message)
        try:
            data = base64.b64decode("".join(self._encoded), validate=True)
        except ValueError as exc:
            message = "remote evaluator artifact was invalid"
            raise ValueError(message) from exc
        if len(data) > _MAX_ARTIFACT_BYTES:
            message = "remote evaluator artifact is too large"
            raise ValueError(message)
        return data


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
        self._replay_byte_offset = 0
        self._delivery_failed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch()
        self._persisted_characters = self._count_persisted_characters()
        record = self._record()
        if self._persisted_characters < record.remote_read_offset:
            message = "SkyPilot log spool is shorter than its journal offset"
            raise ValueError(message)
        if self._persisted_characters > record.remote_read_offset:
            record = self._journal.offsets(
                record,
                remote_read=self._persisted_characters,
                client_delivered=record.client_delivered_offset,
            )
        self._replay_undelivered(record.client_delivered_offset)

    def feed(self, data: str) -> None:
        """Accept Sky's from-origin log stream and deliver only its new suffix."""
        overlap = min(len(data), max(0, self._persisted_characters - self._seen))
        self._compare_persisted(data, overlap)
        suffix = data[overlap:]
        self._seen += len(data)
        if not suffix:
            return
        with self._path.open("a", encoding="utf-8") as spool:
            spool.write(suffix)
            spool.flush()
            os.fsync(spool.fileno())
        self._persisted_characters += len(suffix)
        try:
            current = self._record()
            record = self._journal.offsets(
                current,
                remote_read=self._persisted_characters,
                client_delivered=current.client_delivered_offset,
            )
            if self._delivery_failed:
                return
            delivered = record.client_delivered_offset
            for start in range(0, len(suffix), _LOG_SPOOL_READ_CHARACTERS):
                chunk = suffix[start : start + _LOG_SPOOL_READ_CHARACTERS]
                self._sink(chunk)
                delivered += len(chunk)
                record = self._journal.offsets(
                    record,
                    remote_read=self._persisted_characters,
                    client_delivered=delivered,
                )
        except Exception:
            self._delivery_failed = True
            raise

    def __call__(self, data: str) -> None:
        """Accept a decoded segment from the artifact stream."""
        self.feed(data)

    def cancel(self) -> None:
        """Propagate delivery cancellation to the client transport."""
        if isinstance(self._sink, CancellableOutputSink):
            self._sink.cancel()

    def finish(self) -> None:
        """Require a terminal from-origin stream to cover the durable prefix."""
        if self._seen < self._persisted_characters:
            message = "SkyPilot terminal log replay was truncated"
            raise ValueError(message)

    def _count_persisted_characters(self) -> int:
        characters = 0
        with self._path.open("r", encoding="utf-8") as persisted:
            while chunk := persisted.read(_LOG_SPOOL_READ_CHARACTERS):
                characters += len(chunk)
        return characters

    def _replay_undelivered(self, delivered: int) -> None:
        if delivered >= self._persisted_characters:
            return
        position = 0
        record = self._record()
        with self._path.open("r", encoding="utf-8") as persisted:
            while chunk := persisted.read(_LOG_SPOOL_READ_CHARACTERS):
                end = position + len(chunk)
                if end > delivered:
                    suffix = chunk[max(0, delivered - position) :]
                    self._sink(suffix)
                    delivered += len(suffix)
                    record = self._journal.offsets(
                        record,
                        remote_read=self._persisted_characters,
                        client_delivered=delivered,
                    )
                position = end

    def _compare_persisted(self, data: str, overlap: int) -> None:
        if overlap == 0:
            return
        compared = 0
        with self._path.open("rb") as persisted:
            persisted.seek(self._replay_byte_offset)
            while compared < overlap:
                width = min(_LOG_SPOOL_READ_CHARACTERS, overlap - compared)
                expected = data[compared : compared + width].encode()
                if persisted.read(len(expected)) != expected:
                    message = "SkyPilot replayed log prefix changed"
                    raise ValueError(message)
                self._replay_byte_offset += len(expected)
                compared += width

    def _record(self) -> InvocationRecord:
        record = self._journal.load(self._invocation_id)
        if record is None:
            message = "SkyPilot invocation journal disappeared"
            raise ValueError(message)
        return record


class SkyPilotBridge:
    """Serve a fixed evaluator allowlist over a host-owned Unix socket."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-008191 [PLR0913]; these keyword-only bridge dependencies are the public construction API and bundling them would add wrapper state.
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
        socket_path: Path,
        log: Callable[[str], None],
        framework_setup_command: str | None = None,
        max_infrastructure_retries: int = 1,
        network: Network | None = None,
        threads: Threads | None = None,
    ) -> None:
        """Bind fixed host policy and trusted evaluator commands."""
        self._runner = runner
        self._cluster_name = cluster_name
        self._resources = resources
        self._workspace = workspace
        self._evaluator_package_root = evaluator_package_root
        self._hidden_paths = tuple(hidden_paths)
        self._commands = {kind: tuple(command) for kind, command in commands.items()}
        self._benchmark_output_argument = benchmark_output_argument
        self._framework_setup_command = framework_setup_command
        self._state_namespace = state_namespace
        self._journal = InvocationJournal(state_namespace)
        self.socket_path = socket_path
        self._log = log
        self._max_infrastructure_retries = max_infrastructure_retries
        self._network: Network = network or UnixNetwork()
        self._threads: Threads = threads or OsThreads()
        self._listener: Listener | None = None
        self._acceptor: Worker | None = None
        self._close = CloseOnce()
        self._active_jobs: set[tuple[str, int]] = set()
        self._active_lock = self._threads.lock()
        self._handler_condition = self._threads.condition()
        self._active_handlers = 0
        self._evaluation_lock = self._threads.lock()
        self._closing = self._threads.event()
        self._cluster_replaced_on_start = False
        self._locally_prepared_invocations: set[str] = set()
        self._touched_clusters = {cluster_name}

    def start(self) -> None:
        """Allocate or reuse compute, then start accepting requests."""
        if self._listener is not None:
            return
        if self._close.started:
            message = "SkyPilot bridge cannot be restarted after close"
            raise RuntimeError(message)
        validate_socket_path(self.socket_path)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        try:
            previous_cluster = self._runner.inspect_cluster(self._cluster_name)
            self._cluster_replaced_on_start = allocation_gone(previous_cluster)
            self._runner.ensure_cluster(self._cluster_name, self._resources)
            listener = self._network.listen(str(self.socket_path))
            self._listener = listener
            # The socket is a file only on a filesystem network; any other has nothing to restrict.
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.chmod(0o600)
            self._acceptor = self._threads.spawn(
                lambda: self._accept_loop(listener),
                name=f"skypilot-bridge-{self._cluster_name}",
            )
        except BaseException as start_error:
            try:
                self.close()
            except Exception as close_error:  # noqa: BLE001  # lint-waiver: LW-008199 [BLE001]; the start failure is the error to raise, and any failed teardown is attached to it.
                # > Narrowing to NotReleasedError would let a failed listener close replace the
                # > start failure; letting close raise loses the reason start failed.
                start_error.add_note(f"closing the bridge also failed: {close_error!r}")
            raise

    def close(self) -> None:
        """Stop serving, release the allocation, and remove the socket.

        A cluster whose release failed stays tracked: close raises naming it, and the
        next call releases it again.
        """
        self._close.run(self._release)

    def _release(self) -> Released:
        self._closing.set()
        if self._listener is not None:
            self._listener.close()
        self._cancel_active_jobs()
        if self._acceptor is not None:
            self._acceptor.join(timeout=5)
        with self._handler_condition:
            if not self._handler_condition.wait_for(lambda: self._active_handlers == 0, timeout=10):
                self._log("[warn] SkyPilot bridge handler did not stop before allocation release")
        self._cancel_active_jobs()
        with self._active_lock:
            touched_clusters = tuple(self._touched_clusters)
        for cluster_name in touched_clusters:
            try:
                self._runner.release(cluster_name)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-008192 [BLE001]; teardown must continue after any runner failure and reports each failed release.
                self._log(f"[warn] SkyPilot allocation release failed: {type(exc).__name__}")
            else:
                with self._active_lock:
                    self._touched_clusters.discard(cluster_name)
        self.socket_path.unlink(missing_ok=True)
        with self._active_lock:
            unreleased = sorted(self._touched_clusters)
        return Released.shown_by(
            NothingLeft(unreleased, f"unreleased SkyPilot clusters {unreleased}")
        )

    def _accept_loop(self, listener: Listener) -> None:
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # the listener was closed
            self._threads.spawn(
                lambda c=connection: self._serve(c),
                name=f"skypilot-bridge-request-{self._cluster_name}",
            )

    def _serve(self, connection: Connection) -> None:
        wire = _Wire(connection, self._log)
        try:
            self._handle(wire, wire, connection)
        finally:
            connection.close()

    def _cancel_active_jobs(self) -> None:
        """Best-effort cancel every job known at this point in teardown."""
        with self._active_lock:
            active_jobs = tuple(self._active_jobs)
        for cluster_name, job_id in active_jobs:
            try:
                self._runner.cancel(cluster_name, job_id)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-008193 [BLE001]; teardown must continue after any runner failure and reports each failed cancellation.
                self._log(f"[warn] SkyPilot job cancellation failed: {type(exc).__name__}")

    def _handle(self, reader: _Wire, writer: _Wire, connection: Connection) -> None:
        with self._handler_condition:
            self._active_handlers += 1
        try:
            if self._report_closing(writer):
                return
            with self._evaluation_lock:
                if self._report_closing(writer):
                    return
                self._handle_request(reader, writer, connection)
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-008195 [BLE001]; the request boundary attempts to convert every failure to a protocol ErrorFrame, but a disconnected client cannot receive it.
            with contextlib.suppress(OSError):
                self._write(writer, ErrorFrame(error=type(exc).__name__))
        finally:
            with self._handler_condition:
                self._active_handlers -= 1
                self._handler_condition.notify_all()

    def _report_closing(self, writer: _Wire) -> bool:
        """Write the existing wire error when shutdown has started."""
        if not self._close.started:
            return False
        self._write(writer, ErrorFrame(error=ValueError.__name__))
        return True

    def _handle_request(self, reader: _Wire, writer: _Wire, connection: Connection) -> None:
        payload = reader.readline(_MAX_REQUEST_BYTES + 1)
        if not payload or len(payload) > _MAX_REQUEST_BYTES or not payload.endswith(b"\n"):
            message = "invalid bridge request framing"
            raise ValueError(message)
        request = decode_request(payload)
        command = self._commands.get(request.kind)
        if command is None:
            message = f"evaluator {request.kind!r} is not configured"
            raise ValueError(message)
        if request.arguments:
            valid_dynamic_output = (
                request.kind == "benchmark"
                and len(request.arguments) == _FRAMEWORK_ARGUMENT_COUNT
                and request.arguments[0] == self._benchmark_output_argument
                and request.artifacts == (request.arguments[1],)
                and _FRAMEWORK_ARTIFACT.fullmatch(request.arguments[1]) is not None
            )
            if not valid_dynamic_output:
                message = "invalid evaluator arguments"
                raise ValueError(message)
        elif request.artifacts:
            message = "invalid evaluator artifact"
            raise ValueError(message)
        effective_command = (*command, *request.arguments)
        staging = self._snapshot(request.invocation_id)
        snapshot_digest = self._snapshot_digest(staging)
        request_digest = self._request_digest(request, effective_command)
        existing_record = self._journal.load(request.invocation_id)
        record = self._journal.prepare(request.invocation_id, request_digest, snapshot_digest)
        if existing_record is None:
            self._locally_prepared_invocations.add(request.invocation_id)
        if record.phase in {InvocationPhase.COMPLETED, InvocationPhase.ACKNOWLEDGED}:
            self._track_record_cluster(record)
            self._deliver(record, reader, writer)
            return
        self._run(request, effective_command, record, staging, reader, writer, connection)

    def _request_digest(
        self,
        request: EvaluationRequest,
        command: tuple[str, ...],
    ) -> str:
        """Bind recovery identity to the request, evaluator argv, and trusted setup."""
        request_document: dict[str, object] = {
            "request": request.model_dump(mode="json", exclude={"invocation_id"}),
            "command": command,
        }
        if self._framework_setup_command is not None:
            request_document["framework_setup_command"] = self._framework_setup_command
        return hashlib.sha256(
            json.dumps(
                request_document,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def _run(  # noqa: C901, PLR0913, PLR0915  # lint-waiver: LW-008194 [C901, PLR0913, PLR0915]; this invocation state machine keeps journal, retry, stream, and cleanup transitions visible together.
        self,
        request: EvaluationRequest,
        command: tuple[str, ...],
        record: InvocationRecord,
        staging: Path,
        reader: _Wire,
        writer: _Wire,
        connection: Connection,
    ) -> None:
        self._log(f"[skypilot] running trusted {request.kind} evaluator")
        write_lock = self._threads.lock()
        disconnected = self._threads.event()
        finished = self._threads.event()

        def monitor_disconnect() -> None:
            # The only reader of the connection until ``finished``: ``_deliver`` reads
            # the acknowledgement after this monitor is joined.
            while not finished.is_set():
                if self._closing.is_set():
                    disconnected.set()
                    return
                if not reader.has_room(_MAX_REQUEST_BYTES + 1):
                    finished.wait(0.1)
                elif not reader.read_ahead(_MAX_REQUEST_BYTES + 1, 0.1):
                    disconnected.set()
                    return

        try:
            monitor = self._threads.spawn(monitor_disconnect, name="skypilot-bridge-monitor")
            active_record = record
            try:
                while True:
                    self._require_open_for_remote_action()
                    nonce = hashlib.sha256(
                        f"{request.invocation_id}:{active_record.job_name}".encode()
                    ).hexdigest()[:32]
                    begin_marker = f"__VIBESYS_SKYPILOT_ARTIFACT_BEGIN_{nonce}__"
                    end_marker = f"__VIBESYS_SKYPILOT_ARTIFACT_END_{nonce}__"
                    stdout_output = _BridgeOutputSink(
                        writer,
                        connection,
                        "stdout",
                        write_lock,
                    )
                    stderr_output = _BridgeOutputSink(
                        writer,
                        connection,
                        "stderr",
                        write_lock,
                    )
                    log_spool = _DecodedLogSpool(
                        path=self._state_namespace.external_directory(
                            f"logs/{request.invocation_id}/{active_record.job_name}"
                        )
                        / "stdout",
                        journal=self._journal,
                        invocation_id=request.invocation_id,
                        sink=stdout_output,
                    )
                    artifact_stream = _ArtifactStream(
                        log_spool,
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
                    effective_command = self._with_framework_setup(effective_command)
                    try:
                        found = self._reconcile(active_record)
                        self._require_open_for_remote_action()
                        if found is not None and is_driver_failure(found.life):
                            failed_cluster = active_record.active_cluster_name or self._cluster_name
                            active_record = self._retry_after_infrastructure_failure(active_record)
                            self._runner.release(failed_cluster)
                            self._ensure_current_cluster_for_work()
                            continue
                        if found is None:
                            if active_record.phase is InvocationPhase.SUBMITTING:
                                if not self._allocation_was_replaced(active_record):
                                    message = (
                                        "SkyPilot submission outcome is ambiguous; reconcile later"
                                    )
                                    raise RuntimeError(message)
                                active_record = self._retry_after_infrastructure_failure(
                                    active_record
                                )
                                continue
                            if active_record.phase in {
                                InvocationPhase.SUBMITTED,
                                InvocationPhase.RUNNING,
                            }:
                                if not self._allocation_was_replaced(active_record):
                                    message = "persisted SkyPilot job disappeared from an active allocation"
                                    raise RuntimeError(message)
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
                            stdout_sink=artifact_stream,
                            stderr_sink=stderr_output,
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
                        if not allocation_gone(cluster):
                            raise
                        self._require_open_for_remote_action()
                        active_record = self._recover_after_allocation_loss(latest)
                        self._ensure_current_cluster_for_work()
            finally:
                finished.set()
                monitor.join(timeout=1)
            with self._active_lock:
                self._active_jobs.discard((result.cluster_name, result.remote_job_id))
            artifact = artifact_stream.result() if result.status is JobStatus.COMPLETED else None
            latest = self._journal.load(request.invocation_id) or record
            attempt_resources = latest.attempt_resources
            if attempt_resources is None:
                message = "completed invocation is missing attempt resources"
                raise ValueError(message)
            artifact_record = (
                ArtifactRecord.create(request.artifacts[0], artifact)
                if artifact is not None
                else None
            )
            terminal = InvocationResultRecord(
                status=result.status,
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
            message = "SkyPilot infrastructure retry limit was exhausted"
            raise RuntimeError(message)
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
        return allocation_gone(cluster)

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
            message = "SkyPilot bridge is closing"
            raise RuntimeError(message)

    def _ensure_current_cluster_for_work(self) -> None:
        """Ensure compute unless teardown started, cleaning up a racing launch."""
        self._require_open_for_remote_action()
        self._runner.ensure_cluster(self._cluster_name, self._resources)
        if not self._closing.is_set():
            return
        try:
            self._runner.release(self._cluster_name)
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-008196 [BLE001]; best-effort allocation cleanup must log and continue for any runner failure.
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
        reader: _Wire,
        writer: _Wire,
        lock: Lock | None = None,
    ) -> None:
        """Replay a durable terminal payload and persist explicit acknowledgement."""
        result = record.result
        if result is None:
            message = "terminal invocation is missing its result"
            raise ValueError(message)
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
        payload = reader.readline(_MAX_REQUEST_BYTES + 1)
        acknowledgement = decode_ack(payload)
        if acknowledgement.invocation_id != record.invocation_id:
            message = "acknowledgement invocation mismatch"
            raise ValueError(message)
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
            (staging / ".skyignore").write_text(
                "# VibeSys already filtered this trusted evaluator snapshot.\n",
                encoding="utf-8",
            )
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
                message = "snapshot contains an unsupported file type"
                raise ValueError(message)
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

    def _with_framework_setup(self, command: tuple[str, ...]) -> tuple[str, ...]:
        """Run trusted setup before ``command`` without interpolating its argv."""
        if self._framework_setup_command is None:
            return command
        script = f'set -e\n{self._framework_setup_command}\nexec "$@"'
        return (
            "sh",
            "-c",
            script,
            _FRAMEWORK_EVALUATOR_ARGV0,
            *command,
        )

    def _job_started(
        self,
        record: InvocationRecord,
        job_id: int,
        cluster_name: str,
        disconnected: Event,
    ) -> None:
        self._journal.submitted(record, job_id, cluster_name)
        with self._active_lock:
            self._active_jobs.add((cluster_name, job_id))
            self._touched_clusters.add(cluster_name)
        if self._closing.is_set():
            try:
                self._runner.cancel(cluster_name, job_id)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-008197 [BLE001]; best-effort job cancellation must log and continue for any runner failure.
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
                message = "workspace symlink escapes the project"
                raise ValueError(message)

    def _stage_workspace(self, staging: Path) -> None:
        hidden = frozenset(self._hidden_paths)
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
                if not self._excluded(relative_parent / name, hidden)
            ]
            for name in (*directory_names, *file_names):
                relative = relative_parent / name
                if self._excluded(relative, hidden):
                    continue
                path = current / name
                if path.is_symlink():
                    continue
                if path.is_dir():
                    continue
                if not path.is_file():
                    message = "workspace contains a special file"
                    raise ValueError(message)
                file_count += 1
                total_bytes += path.stat().st_size
                if file_count > _MAX_STAGED_FILES or total_bytes > _MAX_STAGED_BYTES:
                    message = "workspace exceeds remote staging limits"
                    raise ValueError(message)

        def ignore(current_text: str, names: list[str]) -> set[str]:
            parent = Path(current_text).relative_to(self._workspace)
            return {name for name in names if self._excluded(parent / name, hidden)}

        shutil.copytree(
            self._workspace,
            staging,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=ignore,
        )

    @staticmethod
    def _excluded(relative: Path, hidden: frozenset[Path]) -> bool:
        local_state_or_logs = relative.parts[:2] in {
            (".vibesys", "logs"),
            (".vibesys", "state"),
        }
        return (
            relative.name in _STAGING_EXCLUDED_NAMES
            or relative.name.startswith(".env")
            or local_state_or_logs
            or any(relative == path or relative.is_relative_to(path) for path in hidden)
        )

    def _write(
        self,
        writer: _Wire,
        message: OutputFrame | ArtifactFrame | ResultFrame | AckedFrame | ErrorFrame,
        lock: Lock | None = None,
    ) -> None:
        context = lock or self._threads.lock()
        with context:
            writer.write(encode_message(message))
