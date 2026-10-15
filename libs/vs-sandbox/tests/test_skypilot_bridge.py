"""The SkyPilot bridge, driven over an in-memory network with a recording runner Fake.

Guarantees under test (``vs_sandbox.api.skypilot.SkyPilotBridge``):

* An evaluation request is validated (framing, configured kind, allowed arguments
  and artifacts) before any compute is touched; a bad request gets one typed
  ``ErrorFrame`` and runs nothing.
* The workspace snapshot excludes secrets, caches and hidden paths, rejects
  special files, symlink escapes and oversized trees, and is hashed into the
  recovery digest together with the request and the framework setup command.
* Output streams to the client through a durable decoded-log spool: a restart
  resumes from the persisted offset, replays the undelivered suffix in bounded
  chunks, and refuses a changed prefix, a short spool or a missing journal.
* A terminal result is replayed rather than rerun, tracks the cluster that
  produced it for release, and is delivered only against a matching acknowledgement.
* An unresolved prior attempt is never resubmitted; infrastructure retries are
  bounded by the persisted attempt count.
* Closing cancels every in-flight job (including one discovered mid-close),
  releases every touched cluster, logs failures and continues; a cluster whose
  release failed makes close raise and is released again by the next close; and
  the bridge cannot be restarted.
* The one real-socket test pins the filesystem contract: the socket file is
  owner-only and removed on close.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import socket
import tempfile
import threading
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.support import run_test_command

import vs_sandbox.skypilot_bridge as bridge_module  # test-isolation: durable restart-offset behavior belongs to the bridge's private spool state machine.
from vs_project.api import MAX_SOCKET_PATH_BYTES, SocketPathTooLongError, StateNamespace
from vs_project.api.testing import scratch_state_directory
from vs_sandbox.api.skypilot import (
    AckRequest,
    ArtifactFrame,
    AttemptResourcesRecord,
    CancellableOutputSink,
    ClusterInfo,
    ClusterLife,
    ClusterUp,
    ErrorFrame,
    EvaluationRequest,
    InvocationJournal,
    InvocationPhase,
    InvocationProvenance,
    InvocationRecord,
    InvocationResultRecord,
    JobResult,
    JobStatus,
    ResolvedSkyPilotResources,
    ResponseFrame,
    SkyPilotBridge,
    SkyPilotJobRunner,
    SkyPilotOutputError,
    SubprocessCommandRunner,
    decode_response,
    encode_message,
)
from vs_sim.api import NotReleasedError, ProcessTimeoutError, Threads
from vs_sim.api.testing import HANG_GUARD_S, FakePipedProcessLauncher, SimNetwork, SimThreads

from .skypilot_world import MemoryTransport

# The scripted bridge runs on a thread: an exception there must fail the test, not warn.
pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from vs_sandbox.api.skypilot import RemoteJobInfo
    from vs_sim.api import Condition, Connection, Event, Listener, Lock, ThreadLocal, Worker


@pytest.fixture
def transport() -> MemoryTransport:
    return MemoryTransport()


def _resources() -> ResolvedSkyPilotResources:
    return ResolvedSkyPilotResources(
        profile_name="test",
        infra="slurm/example/gpu",
        nodes=1,
        accelerator_backend="rocm",
        accelerator_type="MI300A",
        accelerators_per_node=4,
        exclusive=True,
        remote_artifact_root="/remote/vibesys",
    )


def _attempt_resources() -> AttemptResourcesRecord:
    return AttemptResourcesRecord(
        profile_name="test",
        infra="slurm/example/gpu",
        accelerator_type="MI300A",
        nodes=1,
        accelerators_per_node=4,
    )


class FakeRunner(SkyPilotJobRunner):
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.release_calls = 0
        self.release_names: list[str] = []
        self.cancel_calls: list[tuple[str, int]] = []
        self.workdirs: list[Path] = []
        self.commands: list[tuple[str, ...]] = []
        self.cluster_life: ClusterLife | None = ClusterUp()
        self.before_start: Callable[[], None] = lambda: None
        """Runs before the job starts, as a remote queue or a launch would delay it."""

    def ensure_cluster(
        self,
        name: str,
        resources: ResolvedSkyPilotResources,
        *,
        timeout: float | None = 300,
    ) -> ClusterInfo:
        del resources, timeout
        self.ensure_calls += 1
        return ClusterInfo(name, ClusterUp())

    def inspect_cluster(self, name: str, *, timeout: float = 60) -> ClusterInfo | None:
        del timeout
        if self.cluster_life is None:
            return None
        return ClusterInfo(name, self.cluster_life)

    def run(  # noqa: PLR0913  # lint-waiver: LW-010051 [PLR0913]; this fake preserves the public SkyPilotJobRunner.run callback and resume controls so the bridge exercises the real runner contract.
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
        del resources, timeout, log_tail
        assert stdout_sink is not None
        assert stderr_sink is not None
        assert isinstance(stdout_sink, CancellableOutputSink)
        assert isinstance(stderr_sink, CancellableOutputSink)
        assert job_started is not None
        self.workdirs.append(workdir)
        self.commands.append(tuple(command))
        assert job_name is not None
        assert job_name.startswith("vibesys-inv-")
        assert existing_job_id is None
        assert not (workdir / ".env").exists()
        assert not (workdir / ".venv").exists()
        assert not (workdir / "private").exists()
        assert (workdir / "candidate.py").read_text() == "candidate"
        assert (workdir / ".vibesys-evaluator-package" / "checker.py").exists()
        self.before_start()
        job_started(9)
        artifact_script = next(
            (argument for argument in command if "__VIBESYS_SKYPILOT_ARTIFACT_BEGIN_" in argument),
            None,
        )
        if artifact_script is not None:
            begin = re.search(r"__VIBESYS_SKYPILOT_ARTIFACT_BEGIN_[0-9a-f]+__", artifact_script)
            end = re.search(r"__VIBESYS_SKYPILOT_ARTIFACT_END_[0-9a-f]+__", artifact_script)
            assert begin is not None
            assert end is not None
            stdout_sink(f"out\n{begin.group()}\neyJsYXRlbmN5IjoxfQ==\n{end.group()}\n")
        else:
            stdout_sink("out\n")
        stderr_sink("err\n")
        return JobResult(JobStatus.COMPLETED, 0, 9, "out\n", "err\n", cluster_name)

    def query_job(
        self,
        cluster_name: str,
        *,
        job_name: str,
        job_id: int | None = None,
        timeout: float = 60,
    ) -> RemoteJobInfo | None:
        del cluster_name, job_name, job_id, timeout
        return None

    def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:
        del timeout
        self.cancel_calls.append((cluster_name, job_id))

    def release(self, cluster_name: str, *, timeout: float = 60) -> None:
        del timeout
        self.release_calls += 1
        self.release_names.append(cluster_name)


class _RecordingThreads:
    def __init__(self) -> None:
        self._threads = SimThreads()
        self.workers: list[Worker] = []

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        worker = self._threads.spawn(target, name=name, daemon=daemon)
        self.workers.append(worker)
        return worker

    def lock(self) -> Lock:
        return self._threads.lock()

    def rlock(self) -> Lock:
        return self._threads.rlock()

    def condition(self, lock: Lock | None = None) -> Condition:
        return self._threads.condition(lock)

    def event(self) -> Event:
        return self._threads.event()

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        return self._threads.local(initial)

    def sleep(self, seconds: float) -> None:
        self._threads.sleep(seconds)

    def now(self) -> float:
        return self._threads.now()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        return self._threads.run_async(main)

    def run[T](self, main: Callable[[], T]) -> T:
        return self._threads.run(main)


class _BlockedBridgeConnection:
    """A request whose output transport remains blocked until the server closes it."""

    def __init__(self, request: bytes, threads: Threads) -> None:
        self._request = bytearray(request)
        self.send_started = threads.event()
        self.closed = threads.event()

    def send(self, data: bytes, timeout: float | None = None) -> None:
        del data, timeout
        self.send_started.set()
        if not self.closed.wait(timeout=1):
            raise AssertionError
        raise BrokenPipeError

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del timeout
        if self.closed.is_set():
            raise OSError
        chunk = bytes(self._request[:max_bytes])
        del self._request[:max_bytes]
        return chunk

    def peer_closed(self) -> bool:
        return self.closed.is_set()

    def close(self) -> None:
        self.closed.set()


class _SingleConnectionListener:
    def __init__(self, connection: _BlockedBridgeConnection, threads: Threads) -> None:
        self._connection = connection
        self._closed = threads.event()
        self._accepted = False

    def accept(self, timeout: float | None = None) -> Connection:
        del timeout
        if not self._accepted:
            self._accepted = True
            return self._connection
        if not self._closed.wait(timeout=1):
            raise AssertionError
        raise OSError

    def close(self) -> None:
        self._closed.set()


class _SingleConnectionNetwork:
    def __init__(self, connection: _BlockedBridgeConnection, threads: Threads) -> None:
        self._listener = _SingleConnectionListener(connection, threads)

    def listen(self, address: str) -> Listener:
        del address
        return self._listener

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        del address, timeout
        raise AssertionError


class _TimeoutAfterBridgeWriteProcess:
    def __init__(self, connection: _BlockedBridgeConnection) -> None:
        self._connection = connection
        self.stdout = StringIO("output\n")
        self.stderr = StringIO()
        self.waits = 0
        self.group_kills = 0

    def wait(self, timeout: float | None) -> int:
        self.waits += 1
        if self.waits == 1:
            if not self._connection.send_started.wait(timeout=1):
                raise AssertionError
            assert timeout is not None
            raise ProcessTimeoutError(("sky",), timeout)
        return -9

    def kill_group(self) -> None:
        self.group_kills += 1

    def terminate_group(self) -> None:
        """These runners stop a process group only with SIGKILL."""
        raise AssertionError

    def cancel_output(self) -> None:
        pass


class _BlockingBridgeRunner(FakeRunner):
    def __init__(
        self,
        process: _TimeoutAfterBridgeWriteProcess,
        threads: _RecordingThreads,
    ) -> None:
        super().__init__()
        self.process = process
        self.threads = threads
        self.finished = threads.event()
        self._command = SubprocessCommandRunner(
            threads,
            processes=FakePipedProcessLauncher(lambda _start: process),
            cleanup_timeout=1,
        )

    def run(  # noqa: PLR0913  # lint-waiver: LW-010052 [PLR0913]; this integration fake preserves the runner callback surface while driving the real subprocess/bridge cancellation boundary.
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
        del cluster_name, resources, workdir, command, timeout, job_name, existing_job_id, log_tail
        assert job_started is not None
        job_started(9)
        try:
            self._command.run(
                ("sky", "logs"),
                timeout=0,
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
            )
        finally:
            self.finished.set()
        raise AssertionError


def _namespace(tmp_path: Path) -> StateNamespace:
    root = scratch_state_directory(tmp_path, "skypilot")
    return StateNamespace(project_root=tmp_path, root=root, portable=False)


def test_timeout_cancels_blocked_bridge_transport_and_releases_subprocess_workers(
    tmp_path: Path,
) -> None:
    threads = _RecordingThreads()
    request = encode_message(EvaluationRequest(kind="accuracy", invocation_id="a" * 32))
    connection = _BlockedBridgeConnection(request, threads)
    process = _TimeoutAfterBridgeWriteProcess(connection)
    runner = _BlockingBridgeRunner(process, threads)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        log=lambda _: None,
        network=_SingleConnectionNetwork(connection, threads),
        threads=threads,
    )

    def exercise() -> None:
        try:
            bridge.start()
            if not runner.finished.wait(timeout=1):
                raise AssertionError
        finally:
            connection.close()
            bridge.close()

    threads.run(exercise)

    assert connection.send_started.is_set()
    assert connection.closed.is_set()
    assert process.group_kills == 1
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(
        not worker.is_alive() for worker in threads.workers if worker.name.startswith("sky-")
    )


def test_decoded_log_spool_resumes_from_durable_character_offset(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)
    journal = InvocationJournal(namespace)
    invocation_id = "a" * 32
    record = journal.prepare(invocation_id, "b" * 64, "e" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "state" / "logs" / "stdout"
    first_output: list[str] = []
    first = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-010022; tests the private spool's restart offset against the durable invocation journal
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=first_output.append,
    )
    first.feed("one\n")

    resumed_output: list[str] = []
    resumed = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-010023; verifies replay resumes at the persisted character boundary
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=resumed_output.append,
    )
    resumed.feed("one\ntwo\n")
    resumed.finish()

    assert first_output == ["one\n"]
    assert resumed_output == ["two\n"]
    recovered = journal.load(invocation_id)
    assert recovered is not None
    assert recovered.remote_read_offset == len("one\ntwo\n")
    assert recovered.client_delivered_offset == len("one\ntwo\n")


def test_decoded_log_spool_replays_persisted_undelivered_suffix(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)
    journal = InvocationJournal(namespace)
    invocation_id = "c" * 32
    record = journal.prepare(invocation_id, "d" * 64, "e" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "state" / "logs" / "stdout"

    def disconnect(_: str) -> None:
        raise BrokenPipeError

    spool = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-010024; injects a disconnect into the spool to test durable undelivered output
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=disconnect,
    )
    with pytest.raises(BrokenPipeError):
        spool.feed("durable\n")

    replayed: list[str] = []
    bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-010025; verifies a new private spool replays bytes persisted before sink failure
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=replayed.append,
    )
    assert replayed == ["durable\n"]


def test_decoded_log_spool_replays_large_history_in_bounded_chunks(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)
    journal = InvocationJournal(namespace)
    invocation_id = "4" * 32
    record = journal.prepare(invocation_id, "5" * 64, "6" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "state" / "logs" / "stdout"
    history = ("界" * (64 * 1024 * 3)) + "unterminated"

    def disconnect(_: str) -> None:
        raise BrokenPipeError

    spool = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-994701; exercises the private durable replay implementation's fixed comparison window
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=disconnect,
    )
    with pytest.raises(BrokenPipeError):
        spool.feed(history)

    replayed: list[str] = []
    resumed = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-994702; verifies persisted replay is delivered in bounded chunks without retaining the complete file
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=replayed.append,
    )
    resumed.feed(history)
    resumed.finish()

    assert "".join(replayed) == history
    assert max(map(len, replayed)) <= 64 * 1024


def test_decoded_log_spool_holds_client_offset_after_delivery_gap(tmp_path: Path) -> None:
    journal, path, _ = _spool_fixture(tmp_path, "7" * 32)
    attempts: list[str] = []

    def fail_once(data: str) -> None:
        attempts.append(data)
        if len(attempts) == 1:
            raise BrokenPipeError

    spool = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-994703; injects a fail-first sink to verify the private spool preserves contiguous delivery offsets
        path=path,
        journal=journal,
        invocation_id="7" * 32,
        sink=fail_once,
    )
    with pytest.raises(BrokenPipeError):
        spool.feed("first\n")
    spool.feed("second\n")

    interrupted = journal.load("7" * 32)
    assert interrupted is not None
    assert interrupted.remote_read_offset == len("first\nsecond\n")
    assert interrupted.client_delivered_offset == 0
    assert attempts == ["first\n"]

    replayed: list[str] = []
    resumed = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-994704; verifies restart replays the complete contiguous suffix after a latched delivery gap
        path=path,
        journal=journal,
        invocation_id="7" * 32,
        sink=replayed.append,
    )
    resumed.feed("first\nsecond\n")
    resumed.finish()

    assert replayed == ["first\nsecond\n"]


class _FailFirstOffsetsJournal(InvocationJournal):
    """Exercise recovery from one post-append journal write failure."""

    def __init__(self, namespace: StateNamespace) -> None:
        super().__init__(namespace)
        self._fail_next_offsets = True

    def offsets(
        self,
        record: InvocationRecord,
        *,
        remote_read: int,
        client_delivered: int,
    ) -> InvocationRecord:
        if self._fail_next_offsets:
            self._fail_next_offsets = False
            message = "journal unavailable"
            raise OSError(message)
        return super().offsets(
            record,
            remote_read=remote_read,
            client_delivered=client_delivered,
        )


def test_decoded_log_spool_latches_post_append_journal_failure(tmp_path: Path) -> None:
    journal = _FailFirstOffsetsJournal(_namespace(tmp_path))
    invocation_id = "8" * 32
    record = journal.prepare(invocation_id, "9" * 64, "a" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "state" / "logs" / "stdout"
    delivered: list[str] = []
    spool = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-994705; fault-injects journal bookkeeping after durable append in the private spool state machine
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=delivered.append,
    )

    with pytest.raises(OSError, match="journal unavailable"):
        spool.feed("AAAA")
    spool.feed("BBBB")

    interrupted = journal.load(invocation_id)
    assert interrupted is not None
    assert interrupted.remote_read_offset == len("AAAABBBB")
    assert interrupted.client_delivered_offset == 0
    assert delivered == []

    replayed: list[str] = []
    resumed = bridge_module._DecodedLogSpool(  # noqa: SLF001  # LW-994706; proves restart replays the exact contiguous suffix after journal recovery
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=replayed.append,
    )
    resumed.feed("AAAABBBB")
    resumed.finish()

    assert replayed == ["AAAABBBB"]


def test_startup_replacement_evidence_applies_only_to_preexisting_invocations(
    tmp_path: Path,
    transport: MemoryTransport,
) -> None:
    namespace = _namespace(tmp_path)
    runner = FakeRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)
    invocation_id = "f" * 32
    prepared = journal.prepare(invocation_id, "1" * 64, "2" * 64)
    runner.cluster_life = None
    bridge.start()
    runner.cluster_life = ClusterUp()
    bridge._locally_prepared_invocations.add(invocation_id)  # noqa: SLF001  # LW-010027; marks an invocation prepared in this process for recovery discrimination

    assert bridge._recover_after_allocation_loss(prepared) is prepared  # noqa: SLF001  # LW-010028; verifies an unsubmitted journal record is retained after allocation loss
    submitting = journal.submitting(prepared, "lease", _attempt_resources())
    assert not bridge._allocation_was_replaced(submitting)  # noqa: SLF001  # LW-010029; ensures local preparation prevents a false replacement verdict

    bridge._locally_prepared_invocations.clear()  # noqa: SLF001  # LW-010030; removes the local-preparation evidence to model restart recovery
    assert bridge._allocation_was_replaced(submitting)  # noqa: SLF001  # LW-010031; verifies stale persisted attempts detect the replaced allocation
    bridge.close()
    assert set(runner.release_names) == {"lease"}


def test_terminal_replay_tracks_persisted_cluster_for_release(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    namespace = _namespace(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    package = tmp_path / "package"
    package.mkdir()
    (package / "checker.py").write_text("checker")
    request = EvaluationRequest(kind="accuracy", invocation_id="e" * 32)
    first_runner = FakeRunner()
    first_bridge = SkyPilotBridge(
        runner=first_runner,
        cluster_name="old-lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=package,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,
        socket_path=tmp_path / "first.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    first_bridge.start()
    first_frames = transport.evaluate(first_bridge, request)
    first_bridge.close()

    replay_runner = FakeRunner()
    replay_bridge = SkyPilotBridge(
        runner=replay_runner,
        cluster_name="new-lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=package,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,
        socket_path=tmp_path / "replay.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    replay_bridge.start()
    replay_frames = transport.evaluate(replay_bridge, request)
    replay_bridge.close()

    assert [frame.type for frame in first_frames] == ["stdout", "stderr", "result"]
    assert [frame.type for frame in replay_frames] == ["result"]
    assert first_runner.ensure_calls == 1
    assert replay_runner.ensure_calls == 1
    assert set(replay_runner.release_names) == {"new-lease", "old-lease"}


def test_framework_setup_wraps_new_job_argv_and_runs_first_in_workdir(
    tmp_path: Path,
    transport: MemoryTransport,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    package = tmp_path / "package"
    package.mkdir()
    (package / "checker.py").write_text("checker")
    runner = FakeRunner()
    setup = "printf '%s' ready > .framework-setup"
    evaluator = (
        "sh",
        "-c",
        'test "$(cat .framework-setup)" = ready && printf "%s" "$1" > evaluator-output',
        "evaluator",
        "value; touch injected",
    )
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=package,
        hidden_paths=(),
        commands={"accuracy": evaluator},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
        framework_setup_command=setup,
    )
    bridge.start()
    try:
        frames = transport.evaluate(
            bridge, EvaluationRequest(kind="accuracy", invocation_id="4" * 32)
        )

        assert [frame.type for frame in frames] == ["stdout", "stderr", "result"]
        submitted = runner.commands[0]
        assert submitted == (
            "sh",
            "-c",
            f'set -e\n{setup}\nexec "$@"',
            "vibesys-framework-evaluator",
            *evaluator,
        )
        assert "value; touch injected" not in submitted[2]
        assert (runner.workdirs[0] / "candidate.py").read_text() == "candidate"

        execution_root = tmp_path / "wrapper-execution"
        execution_root.mkdir()
        result = run_test_command(
            submitted,
            cwd=execution_root,
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0
        assert (execution_root / "evaluator-output").read_text() == "value; touch injected"
        assert not (execution_root / "injected").exists()
    finally:
        bridge.close()


def test_framework_setup_participates_in_recovery_digest_without_changing_legacy_digest(
    tmp_path: Path,
    transport: MemoryTransport,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = EvaluationRequest(kind="accuracy", invocation_id="5" * 32)
    command = ("python", "checker.py", "argument")

    (workspace / "candidate.py").write_text("candidate")
    package = tmp_path / "package"
    package.mkdir()
    (package / "checker.py").write_text("checker")

    def make_bridge(setup: str | None, socket_name: str) -> tuple[SkyPilotBridge, FakeRunner]:
        runner = FakeRunner()
        return SkyPilotBridge(
            runner=runner,
            cluster_name="lease",
            resources=_resources(),
            workspace=workspace,
            evaluator_package_root=package,
            hidden_paths=(),
            commands={"accuracy": command},
            benchmark_output_argument=None,
            state_namespace=_namespace(tmp_path),
            socket_path=tmp_path / socket_name,
            network=transport.network,
            threads=transport.threads,
            log=lambda _: None,
            framework_setup_command=setup,
        ), runner

    without_setup, original_runner = make_bridge(None, "original.sock")
    without_setup.start()
    original_frames = transport.evaluate(without_setup, request)
    without_setup.close()
    with_setup, changed_runner = make_bridge("prepare one", "changed.sock")
    with_setup.start()
    conflict_frames = transport.evaluate(with_setup, request)
    with_setup.close()

    assert [frame.type for frame in original_frames] == ["stdout", "stderr", "result"]
    assert len(conflict_frames) == 1
    assert isinstance(conflict_frames[0], ErrorFrame)
    assert conflict_frames[0].error == "ValueError"
    assert original_runner.ensure_calls == changed_runner.ensure_calls == 1
    assert changed_runner.workdirs == []


def test_framework_setup_failure_prevents_evaluator_execution(
    tmp_path: Path,
    transport: MemoryTransport,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    package = tmp_path / "package"
    package.mkdir()
    (package / "checker.py").write_text("checker")
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=package,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
        framework_setup_command="exit 23",
    )

    bridge.start()
    try:
        frames = transport.evaluate(
            bridge,
            EvaluationRequest(kind="accuracy", invocation_id="6" * 32),
        )
        result = run_test_command(
            runner.commands[0],
            cwd=runner.workdirs[0],
            capture_output=True,
            check=False,
            text=True,
        )
    finally:
        bridge.close()

    assert [frame.type for frame in frames] == ["stdout", "stderr", "result"]
    assert result.returncode == 23
    assert not (runner.workdirs[0] / "evaluator-ran").exists()


def test_job_discovered_during_close_is_cancelled_and_released(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    namespace = _namespace(tmp_path)
    runner = FakeRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="new-lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)
    record = journal.prepare("d" * 32, "1" * 64, "2" * 64)
    record = journal.submitting(record, "old-lease", _attempt_resources())
    bridge._closing.set()  # noqa: SLF001  # LW-010033; deterministically places teardown in progress before a late scheduler callback

    with pytest.raises(RuntimeError, match="closing"):
        bridge._job_started(  # noqa: SLF001  # LW-010034; exercises the late job callback race and its cancel/release behavior
            record,
            11,
            "old-lease",
            threading.Event(),
        )
    bridge.close()

    assert ("old-lease", 11) in runner.cancel_calls
    assert set(runner.release_names) == {"new-lease", "old-lease"}


def test_bridge_stages_allowlisted_command_streams_and_cleans_up(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    workspace, package = _staging_fixture(tmp_path)
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=package,
        hidden_paths=(Path("private"),),
        commands={"benchmark": ("python", ".vibesys-evaluator-package/checker.py")},
        benchmark_output_argument="--output-json",
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    bridge.start()
    try:
        # The protocol fixes this remote path; it is not derived from the host's temp root.
        remote_result = "/tmp/vibesys-framework-benchmark-1-1.json"  # noqa: S108  # lint-waiver: LW-010209 [S108]; the bridge protocol requires this fixed remote artifact path.
        frames = transport.evaluate(
            bridge,
            EvaluationRequest(
                kind="benchmark",
                invocation_id="1" * 32,
                arguments=("--output-json", remote_result),
                artifacts=(remote_result,),
            ),
        )
        assert [frame.type for frame in frames] == ["stdout", "stderr", "artifact", "result"]
        assert isinstance(frames[2], ArtifactFrame)
        assert runner.commands[0][:2] == ("sh", "-c")
        assert runner.commands[0][2].index("rm -f --") < runner.commands[0][2].index("python")
        assert ".vibesys-evaluator-package/checker.py" in runner.commands[0][2]
        assert remote_result in runner.commands[0][2]
        staged = runner.workdirs[0]
        assert not (staged / ".vibesys-evaluator-tools").exists()
        assert not (staged / ".vibesys-evaluator-toolchains").exists()
        assert not (staged / ".bin").exists()
        assert not (staged / ".pip").exists()
        assert not (staged / ".uv-cache").exists()
        assert (staged / ".vibesys-evaluator-package" / "checker.py").read_text() == "checker"
        assert staged.joinpath(".skyignore").read_text().startswith("# VibeSys")
    finally:
        bridge.close()


def _staging_fixture(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    (workspace / ".env").write_text("SECRET=x")
    for directory, name in (
        (".venv", "large-cache"),
        (".vibesys-evaluator-tools", "poisoned"),
        (".vibesys-evaluator-toolchains", "poisoned"),
        (".bin", "cargo"),
        (".pip", "uv.py"),
        (".uv-cache", "archive"),
    ):
        hidden = workspace / directory
        hidden.mkdir()
        (hidden / name).write_text("excluded")
    package = workspace / ".vibesys-evaluator-package"
    package.mkdir()
    (package / "checker.py").write_text("candidate")
    (workspace / ".skyignore").write_text(".vibesys-evaluator-package\n")
    private = workspace / "private"
    private.mkdir()
    (private / "token").write_text("secret")
    evaluator_package = tmp_path / "package"
    evaluator_package.mkdir()
    (evaluator_package / "checker.py").write_text("checker")
    return workspace, evaluator_package


def test_bridge_releases_cluster_when_socket_startup_fails(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()

    class BrokenNetwork:
        def listen(self, address: str) -> Listener:
            raise OSError("bind failed " + address)

        def connect(self, address: str, timeout: float | None = None) -> Connection:
            raise ConnectionRefusedError(address, timeout)

    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        threads=transport.threads,
        log=lambda _: None,
        network=BrokenNetwork(),
    )

    with pytest.raises(OSError, match="bind failed"):
        bridge.start()

    assert runner.ensure_calls == 1
    assert runner.release_calls == 1


def test_bridge_rejects_an_unservable_socket_path_before_allocating_compute(
    tmp_path: Path,
) -> None:
    """A log directory too deep for ``sun_path`` must not cost a cluster lease."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / ("d" * MAX_SOCKET_PATH_BYTES) / "bridge.sock",
        log=lambda _: None,
    )

    with pytest.raises(SocketPathTooLongError):
        bridge.start()

    assert runner.ensure_calls == 0
    assert runner.release_calls == 0
    assert not bridge.socket_path.exists()


def test_bridge_rejects_special_workspace_file(tmp_path: Path, transport: MemoryTransport) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    bridge.start()
    try:
        frame = _send_raw(
            transport,
            bridge,
            encode_message(EvaluationRequest(kind="accuracy", invocation_id="2" * 32)),
        )
        assert isinstance(frame, ErrorFrame)
        assert frame.error == "ValueError"
        assert runner.commands == []
    finally:
        bridge.close()
        bridge.close()

    assert runner.ensure_calls == 1
    assert runner.release_calls == 1


def test_bridge_rejects_workspace_symlink_escape(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (workspace / "escape").symlink_to(outside)
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("python", "checker.py")},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=lambda _: None,
    )
    bridge.start()
    try:
        frame = _send_raw(
            transport,
            bridge,
            encode_message(EvaluationRequest(kind="accuracy", invocation_id="3" * 32)),
        )
        assert isinstance(frame, ErrorFrame)
        assert frame.error == "ValueError"
        assert runner.commands == []
    finally:
        bridge.close()


def _private(target: object, name: str) -> _Dynamic:
    """Reach a private attribute the tests deliberately drive or seed."""
    return getattr(target, name)


def _set_private(target: object, name: str, *, value: object) -> None:
    setattr(target, name, value)


_Dynamic = Any
_BEGIN = "__BEGIN__"
_END = "__END__"


def _artifact_stream(*, expected: bool = True) -> _Dynamic:
    return _private(bridge_module, "_ArtifactStream")(
        lambda _: None, expected=expected, begin_marker=_BEGIN, end_marker=_END
    )


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ("plain output\n", "missing or invalid"),
        (f"{_BEGIN}\ne30=\n", "missing or invalid"),
        (f"{_BEGIN}\n{_END}\n", "missing or invalid"),
        (f"{_BEGIN}\n!!!\n{_END}\n", "artifact was invalid"),
    ],
)
def test_artifact_stream_rejects_missing_or_invalid_artifact(lines: str, message: str) -> None:
    stream = _artifact_stream()
    stream.feed(lines)

    with pytest.raises(ValueError, match=message):
        stream.result()


_ARTIFACT_LIMIT_BYTES = 512 * 1024  # the bridge's documented artifact cap


def test_artifact_stream_decodes_and_bounds_artifact() -> None:
    good = _artifact_stream()
    good.feed(f"out\n{_BEGIN}\ne30=\n{_END}\n")
    assert good.result() == b"{}"
    assert _artifact_stream(expected=False).result() is None

    at_limit = _artifact_stream()
    at_limit.feed(_framed_artifact(bytes(_ARTIFACT_LIMIT_BYTES)))
    assert len(at_limit.result() or b"") == _ARTIFACT_LIMIT_BYTES
    big = _artifact_stream()
    big.feed(_framed_artifact(bytes(_ARTIFACT_LIMIT_BYTES + 1)))
    with pytest.raises(ValueError, match="too large"):
        big.result()


def _framed_artifact(payload: bytes) -> str:
    return f"{_BEGIN}\n{base64.b64encode(payload).decode('ascii')}\n{_END}\n"


def _spool_fixture(
    tmp_path: Path, invocation_id: str
) -> tuple[InvocationJournal, Path, Callable[[list[str]], _Dynamic]]:
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare(invocation_id, "b" * 64, "e" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "logs" / "stdout"

    def make(output: list[str]) -> _Dynamic:
        return _private(bridge_module, "_DecodedLogSpool")(
            path=path, journal=journal, invocation_id=invocation_id, sink=output.append
        )

    return journal, path, make


def test_decoded_log_spool_rejects_changed_replay_prefix_and_truncated_replay(
    tmp_path: Path,
) -> None:
    _, _, make = _spool_fixture(tmp_path, "1" * 32)
    make([]).feed("abc\n")

    changed = make([])
    with pytest.raises(ValueError, match="replayed log prefix changed"):
        changed.feed("xyz\n")

    truncated = make([])
    truncated.feed("abc\n")
    truncated.finish()
    with pytest.raises(ValueError, match="terminal log replay was truncated"):
        make([]).finish()


def test_decoded_log_spool_rejects_short_spool_and_missing_journal(tmp_path: Path) -> None:
    journal, _, make = _spool_fixture(tmp_path, "2" * 32)
    record = journal.load("2" * 32)
    assert record is not None
    journal.offsets(record, remote_read=5, client_delivered=0)

    with pytest.raises(ValueError, match="shorter than its journal offset"):
        make([])

    with pytest.raises(ValueError, match="journal disappeared"):
        _private(bridge_module, "_DecodedLogSpool")(
            path=tmp_path / "other" / "stdout",
            journal=journal,
            invocation_id="3" * 32,
            sink=lambda _: None,
        )


def _bridge(
    tmp_path: Path,
    transport: MemoryTransport,
    runner: FakeRunner | None = None,
    log: Callable[[str], None] | None = None,
    max_retries: int = 1,
) -> SkyPilotBridge:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SkyPilotBridge(
        runner=runner or FakeRunner(),
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        socket_path=tmp_path / "bridge.sock",
        network=transport.network,
        threads=transport.threads,
        log=log or (lambda _: None),
        max_infrastructure_retries=max_retries,
    )


def _send_raw(transport: MemoryTransport, bridge: SkyPilotBridge, payload: bytes) -> ResponseFrame:
    client = transport.dial(bridge)
    try:
        client.send(payload)
        return client.read_frame()
    finally:
        client.close()


@pytest.mark.parametrize(
    "payload",
    [
        encode_message(EvaluationRequest(kind="benchmark", invocation_id="4" * 32)),
        encode_message(
            EvaluationRequest(kind="accuracy", invocation_id="4" * 32, arguments=("--x",))
        ),
        encode_message(
            EvaluationRequest(kind="accuracy", invocation_id="4" * 32, artifacts=("artifact.json",))
        ),
    ],
    ids=["unconfigured-kind", "arguments", "artifacts"],
)
def test_bridge_rejects_malformed_or_unauthorized_requests(
    tmp_path: Path, transport: MemoryTransport, payload: bytes
) -> None:
    runner = FakeRunner()
    bridge = _bridge(tmp_path, transport, runner)
    bridge.start()
    try:
        frame = _send_raw(transport, bridge, payload)
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "ValueError"
    assert runner.commands == []


def test_bridge_cannot_restart_after_close(tmp_path: Path, transport: MemoryTransport) -> None:
    bridge = _bridge(tmp_path, transport)
    bridge.close()

    with pytest.raises(RuntimeError, match="cannot be restarted after close"):
        bridge.start()


def test_bridge_close_logs_failed_cancel_and_release_and_continues(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    class FailingRunner(FakeRunner):
        def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:
            del cluster_name, job_id, timeout
            message = "cancel broke"
            raise RuntimeError(message)

        def release(self, cluster_name: str, *, timeout: float = 60) -> None:
            del cluster_name, timeout
            message = "release broke"
            raise OSError(message)

    logs: list[str] = []
    bridge = _bridge(tmp_path, transport, FailingRunner(), logs.append)
    bridge.start()
    _private(bridge, "_active_jobs").add(("lease", 3))

    with pytest.raises(NotReleasedError, match="lease"):
        bridge.close()

    assert "[warn] SkyPilot job cancellation failed: RuntimeError" in logs
    assert "[warn] SkyPilot allocation release failed: OSError" in logs


@pytest.mark.parametrize("failures", [1, 2])
def test_a_cluster_whose_release_failed_is_released_again_on_the_next_close(
    tmp_path: Path, transport: MemoryTransport, failures: int
) -> None:
    """Regression: a failed release was logged and the bridge latched closed, leaking the cluster."""

    class FlakyRelease(FakeRunner):
        def release(self, cluster_name: str, *, timeout: float = 60) -> None:
            super().release(cluster_name, timeout=timeout)
            if cluster_name == "old-lease" and self.release_names.count(cluster_name) <= failures:
                message = "sky down was killed"
                raise OSError(message)

    runner = FlakyRelease()
    bridge = _bridge(tmp_path, transport, runner)
    bridge.start()
    _private(bridge, "_touched_clusters").add("old-lease")

    for _ in range(failures):
        with pytest.raises(NotReleasedError, match="old-lease"):
            bridge.close()
    bridge.close()
    bridge.close()

    assert runner.release_names.count("old-lease") == failures + 1
    assert runner.release_names.count("lease") == 1


def test_handler_reports_value_error_frame_once_shutdown_has_started(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    bridge = _bridge(tmp_path, transport)
    early = io.BytesIO()
    _set_private(bridge, "_closed", value=True)
    _private(bridge, "_handle")(
        io.BytesIO(), early, _BlockedBridgeConnection(b"", transport.threads)
    )
    assert decode_response(early.getvalue()) == ErrorFrame(error="ValueError")

    class CloseWhileLocked:
        def __enter__(self) -> None:
            _set_private(bridge, "_closed", value=True)

        def __exit__(self, *_: object) -> None:
            return None

    _set_private(bridge, "_closed", value=False)
    _set_private(bridge, "_evaluation_lock", value=CloseWhileLocked())
    late = io.BytesIO()
    _private(bridge, "_handle")(
        io.BytesIO(), late, _BlockedBridgeConnection(b"", transport.threads)
    )
    assert decode_response(late.getvalue()) == ErrorFrame(error="ValueError")


def _digests(bridge: SkyPilotBridge, request: EvaluationRequest) -> tuple[str, str]:
    staging = _private(bridge, "_snapshot")(request.invocation_id)
    return (
        _private(bridge, "_request_digest")(request, ("true",)),
        _private(bridge, "_snapshot_digest")(staging),
    )


@pytest.mark.parametrize(
    "phase",
    [InvocationPhase.SUBMITTING, InvocationPhase.SUBMITTED],
)
def test_bridge_refuses_to_resubmit_when_prior_attempt_is_unresolved(
    tmp_path: Path, transport: MemoryTransport, phase: InvocationPhase
) -> None:
    runner = FakeRunner()
    logs: list[str] = []
    bridge = _bridge(tmp_path, transport, runner, logs.append)
    request = EvaluationRequest(kind="accuracy", invocation_id="5" * 32)
    request_digest, snapshot_digest = _digests(bridge, request)
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare(request.invocation_id, request_digest, snapshot_digest)
    record = journal.submitting(record, "lease", _attempt_resources())
    if phase is InvocationPhase.SUBMITTED:
        record = journal.submitted(record, 9, "lease")
    bridge.start()
    try:
        frame = _send_raw(transport, bridge, encode_message(request))
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "RuntimeError"
    assert runner.commands == []
    persisted = journal.load(request.invocation_id)
    assert persisted is not None
    assert persisted.phase is record.phase


def test_bridge_rejects_completion_without_attempt_resources(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    namespace = _namespace(tmp_path)

    class ForgetfulRunner(FakeRunner):
        def run(self, cluster_name: str, *_: object, **kwargs: object) -> JobResult:
            started = kwargs["job_started"]
            assert callable(started)
            cast("Callable[[int], None]", started)(9)
            journals = list(tmp_path.rglob(f"{'6' * 32}.json"))
            assert journals
            for journal_file in journals:
                journal_file.unlink()
            return JobResult(JobStatus.COMPLETED, 0, 9, "", "", cluster_name)

    bridge = _bridge(tmp_path, transport, ForgetfulRunner())
    bridge.start()
    try:
        frame = _send_raw(
            transport,
            bridge,
            encode_message(EvaluationRequest(kind="accuracy", invocation_id="6" * 32)),
        )
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "ValueError"
    assert InvocationJournal(namespace).load("6" * 32) is None


def test_retry_limit_is_enforced_from_persisted_attempt_count(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    bridge = _bridge(tmp_path, transport, max_retries=0)
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare("7" * 32, "1" * 64, "2" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())

    with pytest.raises(RuntimeError, match="retry limit was exhausted"):
        _private(bridge, "_retry_after_infrastructure_failure")(record)
    assert _private(bridge, "_recover_after_allocation_loss")(
        journal.prepare("8" * 32, "1" * 64, "2" * 64)
    )


def test_ensure_current_cluster_releases_racing_launch_and_reports_closing(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    class ClosingRunner(FakeRunner):
        bridge: SkyPilotBridge

        def ensure_cluster(
            self, name: str, resources: ResolvedSkyPilotResources, *, timeout: float | None = 300
        ) -> ClusterInfo:
            _private(self.bridge, "_closing").set()
            return super().ensure_cluster(name, resources, timeout=timeout)

        def release(self, cluster_name: str, *, timeout: float = 60) -> None:
            super().release(cluster_name, timeout=timeout)
            message = "release broke"
            raise OSError(message)

    runner = ClosingRunner()
    logs: list[str] = []
    bridge = _bridge(tmp_path, transport, runner, logs.append)
    runner.bridge = bridge

    with pytest.raises(RuntimeError, match="bridge is closing"):
        _private(bridge, "_ensure_current_cluster_for_work")()

    assert runner.release_names == ["lease"]
    assert logs == ["[warn] SkyPilot allocation release failed: OSError"]


def test_job_started_during_close_logs_failed_cancel_then_refuses(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    class FailingCancel(FakeRunner):
        def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:
            del cluster_name, job_id, timeout
            message = "cancel broke"
            raise OSError(message)

    logs: list[str] = []
    bridge = _bridge(tmp_path, transport, FailingCancel(), logs.append)
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare("9" * 32, "1" * 64, "2" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    _private(bridge, "_closing").set()

    with pytest.raises(RuntimeError, match="bridge is closing"):
        _private(bridge, "_job_started")(record, 4, "lease", threading.Event())

    assert logs == ["[warn] SkyPilot job cancellation failed: OSError"]


def _terminal_record(tmp_path: Path) -> tuple[InvocationJournal, InvocationRecord]:
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare("a" * 32, "1" * 64, "2" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    record = journal.submitted(record, 9, "lease")
    result = InvocationResultRecord(
        status=JobStatus.COMPLETED,
        sky_exit_code=0,
        provenance=InvocationProvenance(
            profile_name="test",
            infra="slurm/example/gpu",
            cluster_name="lease",
            job_name=record.job_name,
            remote_job_id=9,
            attempt=1,
            accelerator_type="MI300A",
            nodes=1,
            accelerators_per_node=4,
        ),
    )
    return journal, journal.completed(record, result)


def test_deliver_rejects_missing_result_and_mismatched_acknowledgement(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    bridge = _bridge(tmp_path, transport)
    journal, completed = _terminal_record(tmp_path)
    prepared = journal.prepare("b" * 32, "1" * 64, "2" * 64)

    with pytest.raises(ValueError, match="missing its result"):
        _private(bridge, "_deliver")(prepared, io.BytesIO(), io.BytesIO())
    wrong_ack = io.BytesIO(encode_message(AckRequest(invocation_id="c" * 32)))
    out = io.BytesIO()
    with pytest.raises(ValueError, match="acknowledgement invocation mismatch"):
        _private(bridge, "_deliver")(completed, wrong_ack, out)

    assert decode_response(out.getvalue().splitlines()[0]).type == "result"
    assert not [line for line in out.getvalue().splitlines() if b"acked" in line]
    reloaded = journal.load("a" * 32)
    assert reloaded is not None
    assert reloaded.phase is InvocationPhase.COMPLETED


def test_snapshot_digest_and_staging_reject_special_files_and_oversized_workspaces(
    tmp_path: Path, transport: MemoryTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(tmp_path, transport)
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    os.mkfifo(snapshot / "pipe")
    with pytest.raises(ValueError, match="unsupported file type"):
        _private(bridge, "_snapshot_digest")(snapshot)

    (tmp_path / "workspace" / "big.txt").write_text("x")
    monkeypatch.setattr(bridge_module, "_MAX_STAGED_FILES", 0)
    with pytest.raises(ValueError, match="exceeds remote staging limits"):
        _private(bridge, "_stage_workspace")(tmp_path / "stage")


def test_a_queue_record_without_a_status_ends_the_request_in_a_typed_failure(
    tmp_path: Path, transport: MemoryTransport
) -> None:
    """An unreadable queue status is a permanent failure: no retry, no resubmission."""

    class UnreadableQueueRunner(FakeRunner):
        def query_job(
            self,
            cluster_name: str,
            *,
            job_name: str,
            job_id: int | None = None,
            timeout: float = 60,
        ) -> RemoteJobInfo | None:
            del cluster_name, job_name, job_id, timeout
            raise SkyPilotOutputError.unknown_job_status(None)

    runner = UnreadableQueueRunner()
    bridge = _bridge(tmp_path, transport, runner)
    bridge.start()
    try:
        frame = _send_raw(
            transport,
            bridge,
            encode_message(EvaluationRequest(kind="accuracy", invocation_id="7" * 32)),
        )
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "SkyPilotOutputError"
    assert runner.commands == []


def test_a_unix_socket_bridge_serves_an_evaluation_with_a_private_socket_file(
    tmp_path: Path,
) -> None:
    """The one wiring test over a real Unix socket: the file is owner-only and removed on close."""
    # A bindable Unix socket path must be shallow; tmp_path nests too deep.
    short_dir = tempfile.mkdtemp(prefix="vsb-", dir="/tmp")
    try:
        runner = FakeRunner()
        socket_path = Path(short_dir) / "bridge.sock"
        workspace, package = _staging_fixture(tmp_path)
        bridge = SkyPilotBridge(
            runner=runner,
            cluster_name="lease",
            resources=_resources(),
            workspace=workspace,
            evaluator_package_root=package,
            hidden_paths=(Path("private"),),
            commands={"accuracy": ("true",)},
            benchmark_output_argument=None,
            state_namespace=_namespace(tmp_path),
            socket_path=socket_path,
            log=lambda _: None,
        )
        bridge.start()
        try:
            mode = socket_path.stat().st_mode & 0o777
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(HANG_GUARD_S)
                client.connect(str(socket_path))
                client.sendall(
                    encode_message(EvaluationRequest(kind="accuracy", invocation_id="9" * 32))
                )
                reader = client.makefile("rb")
                frames = [decode_response(reader.readline()) for _ in range(3)]
                client.sendall(encode_message(AckRequest(invocation_id="9" * 32)))
                assert decode_response(reader.readline()).type == "acked"
        finally:
            bridge.close()

        assert mode == 0o600
        assert [frame.type for frame in frames] == ["stdout", "stderr", "result"]
        assert not socket_path.exists()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


@pytest.mark.parametrize("extra", [b"", b"x", b"more than nothing\n"], ids=["none", "byte", "line"])
def test_a_client_that_sent_extra_bytes_and_hung_up_is_seen_disconnected(
    tmp_path: Path, extra: bytes
) -> None:
    """Bytes a client leaves unread after its request cannot hide its hangup.

    The monitor asked ``peer_closed``, which is false while unread data
    remains, so a client that wrote anything past its request and then hung
    up was never seen as gone (the flaw #2323 fixed in the Unix transport).
    """
    threads = SimThreads(schedule_seed=0)
    logs: list[str] = []
    socket_path = Path(tempfile.mkdtemp(prefix="vs-sky-")) / "bridge.sock"

    def main() -> None:
        network = SimNetwork(threads)
        workspace, package = _staging_fixture(tmp_path)
        runner = FakeRunner()
        runner.before_start = lambda: threads.sleep(5.0)
        bridge = SkyPilotBridge(
            runner=runner,
            cluster_name="lease",
            resources=_resources(),
            workspace=workspace,
            evaluator_package_root=package,
            hidden_paths=(Path("private"),),
            commands={"benchmark": ("python", ".vibesys-evaluator-package/checker.py")},
            benchmark_output_argument="--output-json",
            state_namespace=_namespace(tmp_path),
            socket_path=socket_path,
            network=network,
            threads=threads,
            log=logs.append,
        )
        bridge.start()
        try:
            client = network.connect(str(socket_path), HANG_GUARD_S)
            request = EvaluationRequest(kind="benchmark", invocation_id="2" * 32)
            client.send(encode_message(request))
            threads.sleep(1.0)  # the bridge has read the request and is running it
            if extra:
                client.send(extra)
            client.close()
            threads.sleep(30.0)
        finally:
            bridge.close()

    threads.run(main)
    assert any("client disconnected" in line for line in logs), logs
