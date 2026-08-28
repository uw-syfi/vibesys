from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import vibesys.skypilot.bridge as bridge_module
from vibesys.skypilot.bridge import SkyPilotBridge
from vibesys.skypilot.config import ResolvedSkyPilotResources
from vibesys.skypilot.protocol import (
    AckRequest,
    ArtifactFrame,
    ErrorFrame,
    EvaluationRequest,
    decode_response,
    encode_message,
)
from vibesys.skypilot.recovery import (
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationProvenance,
    InvocationResultRecord,
)
from vibesys.skypilot.runner import ClusterInfo, ClusterStatus, JobResult, JobStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


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


class FakeRunner:
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.release_calls = 0
        self.release_names: list[str] = []
        self.cancel_calls: list[tuple[str, int]] = []
        self.workdirs: list[Path] = []
        self.commands: list[tuple[str, ...]] = []
        self.cluster_status: ClusterStatus | None = ClusterStatus.UP

    def ensure_cluster(self, name: str, resources: object) -> None:  # noqa: ARG002
        self.ensure_calls += 1

    def inspect_cluster(self, name: str) -> ClusterInfo | None:
        if self.cluster_status is None:
            return None
        return ClusterInfo(name, self.cluster_status)

    def run(  # noqa: PLR0913
        self,
        cluster_name: str,
        resources: object,  # noqa: ARG002
        *,
        workdir: Path,
        command: Sequence[str],
        stdout_sink: Callable[[str], None],
        stderr_sink: Callable[[str], None],
        job_started: Callable[[int], None],
        job_name: str,
        existing_job_id: int | None,
    ) -> JobResult:
        self.workdirs.append(workdir)
        self.commands.append(tuple(command))
        assert job_name.startswith("vibesys-inv-")
        assert existing_job_id is None
        assert not (workdir / ".env").exists()
        assert not (workdir / ".venv").exists()
        assert not (workdir / "private").exists()
        assert (workdir / "candidate.py").read_text() == "candidate"
        assert (workdir / ".vibesys-evaluator-package" / "checker.py").exists()
        job_started(9)
        if tuple(command[:2]) == ("sh", "-c"):
            begin = re.search(r"__VIBESYS_SKYPILOT_ARTIFACT_BEGIN_[0-9a-f]+__", command[2])
            end = re.search(r"__VIBESYS_SKYPILOT_ARTIFACT_END_[0-9a-f]+__", command[2])
            assert begin is not None
            assert end is not None
            stdout_sink(f"out\n{begin.group()}\neyJsYXRlbmN5IjoxfQ==\n{end.group()}\n")
        else:
            stdout_sink("out\n")
        stderr_sink("err\n")
        return JobResult(JobStatus.COMPLETED, 0, 9, "out\n", "err\n", cluster_name)

    def query_job(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        return None

    def cancel(self, cluster_name: str, job_id: int) -> None:
        self.cancel_calls.append((cluster_name, job_id))

    def release(self, cluster_name: str) -> None:
        self.release_calls += 1
        self.release_names.append(cluster_name)


class _Slot:
    def __init__(self) -> None:
        self.value: object | None = None

    def load_optional(self) -> object | None:
        return self.value

    def save(self, value: object) -> None:
        self.value = value


class _Namespace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.slots: dict[str, _Slot] = {}

    def slot(self, path: str, model: object) -> _Slot:  # noqa: ARG002
        return self.slots.setdefault(path, _Slot())

    def external_directory(self, relative: str) -> Path:
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        return path


def test_decoded_log_spool_resumes_from_durable_character_offset(tmp_path: Path) -> None:
    namespace = _Namespace(tmp_path / "state")
    journal = InvocationJournal(namespace)  # pyright: ignore[reportArgumentType]
    invocation_id = "a" * 32
    record = journal.prepare(invocation_id, "b" * 64, "e" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "state" / "logs" / "stdout"
    first_output: list[str] = []
    first = bridge_module._DecodedLogSpool(  # noqa: SLF001
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=first_output.append,
    )
    first.feed("one\n")

    resumed_output: list[str] = []
    resumed = bridge_module._DecodedLogSpool(  # noqa: SLF001
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
    namespace = _Namespace(tmp_path / "state")
    journal = InvocationJournal(namespace)  # pyright: ignore[reportArgumentType]
    invocation_id = "c" * 32
    record = journal.prepare(invocation_id, "d" * 64, "e" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())
    journal.submitted(record, 9, "lease")
    path = tmp_path / "state" / "logs" / "stdout"

    def disconnect(_: str) -> None:
        raise BrokenPipeError

    spool = bridge_module._DecodedLogSpool(  # noqa: SLF001
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=disconnect,
    )
    with pytest.raises(BrokenPipeError):
        spool.feed("durable\n")

    replayed: list[str] = []
    bridge_module._DecodedLogSpool(  # noqa: SLF001
        path=path,
        journal=journal,
        invocation_id=invocation_id,
        sink=replayed.append,
    )
    assert replayed == ["durable\n"]


def test_startup_replacement_evidence_applies_only_to_preexisting_invocations(
    tmp_path: Path,
) -> None:
    namespace = _Namespace(tmp_path / "state")
    runner = FakeRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)  # pyright: ignore[reportArgumentType]
    invocation_id = "f" * 32
    prepared = journal.prepare(invocation_id, "1" * 64, "2" * 64)
    bridge._cluster_replaced_on_start = True  # noqa: SLF001
    bridge._locally_prepared_invocations.add(invocation_id)  # noqa: SLF001

    assert bridge._recover_after_allocation_loss(prepared) is prepared  # noqa: SLF001
    submitting = journal.submitting(prepared, "lease", _attempt_resources())
    assert not bridge._allocation_was_replaced(submitting)  # noqa: SLF001

    bridge._locally_prepared_invocations.clear()  # noqa: SLF001
    assert bridge._allocation_was_replaced(submitting)  # noqa: SLF001
    bridge._touched_clusters.add("old-lease")  # noqa: SLF001
    bridge.close()
    assert set(runner.release_names) == {"lease", "old-lease"}


def test_terminal_replay_tracks_persisted_cluster_for_release(tmp_path: Path) -> None:
    namespace = _Namespace(tmp_path / "state")
    runner = FakeRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="new-lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    request = EvaluationRequest(kind="accuracy", invocation_id="e" * 32)
    staging = bridge._snapshot(request.invocation_id)  # noqa: SLF001
    snapshot_digest = bridge._snapshot_digest(staging)  # noqa: SLF001
    request_digest = hashlib.sha256(
        json.dumps(
            {
                "request": request.model_dump(mode="json", exclude={"invocation_id"}),
                "command": ("true",),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    journal = InvocationJournal(namespace)  # pyright: ignore[reportArgumentType]
    record = journal.prepare(request.invocation_id, request_digest, snapshot_digest)
    record = journal.submitting(record, "old-lease", _attempt_resources())
    record = journal.submitted(record, 9, "old-lease")
    journal.completed(
        record,
        InvocationResultRecord(
            status="COMPLETED",
            sky_exit_code=0,
            provenance=InvocationProvenance(
                profile_name="old-profile",
                infra="slurm/old/gpu",
                cluster_name="old-lease",
                job_name=record.job_name,
                remote_job_id=9,
                attempt=1,
                accelerator_type="MI300A",
                nodes=1,
                accelerators_per_node=4,
            ),
        ),
    )
    reader = io.BytesIO(
        encode_message(request) + encode_message(AckRequest(invocation_id=request.invocation_id))
    )

    connection, peer = socket.socketpair()
    try:
        bridge._handle_request(reader, io.BytesIO(), connection)  # noqa: SLF001
    finally:
        connection.close()
        peer.close()
    bridge.close()

    assert set(runner.release_names) == {"new-lease", "old-lease"}


def test_job_discovered_during_close_is_cancelled_and_released(tmp_path: Path) -> None:
    namespace = _Namespace(tmp_path / "state")
    runner = FakeRunner()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="new-lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=namespace,  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)  # pyright: ignore[reportArgumentType]
    record = journal.prepare("d" * 32, "1" * 64, "2" * 64)
    record = journal.submitting(record, "old-lease", _attempt_resources())
    bridge._closing.set()  # noqa: SLF001

    with pytest.raises(RuntimeError, match="closing"):
        bridge._job_started(  # noqa: SLF001
            record,
            11,
            "old-lease",
            threading.Event(),
        )
    bridge.close()

    assert ("old-lease", 11) in runner.cancel_calls
    assert set(runner.release_names) == {"new-lease", "old-lease"}


def test_bridge_stages_allowlisted_command_streams_and_cleans_up(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    (workspace / ".env").write_text("SECRET=x")
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "large-cache").write_text("excluded")
    (workspace / "private").mkdir()
    (workspace / "private" / "token").write_text("secret")
    package = tmp_path / "package"
    package.mkdir()
    (package / "checker.py").write_text("checker")
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=package,
        hidden_paths=(Path("private"),),
        commands={"benchmark": ("python", ".vibesys-evaluator-package/checker.py")},
        benchmark_output_argument="--output-json",
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    bridge.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
            remote_result = "/tmp/vibesys-framework-benchmark-1-1.json"  # noqa: S108
            client.sendall(
                encode_message(
                    EvaluationRequest(
                        kind="benchmark",
                        invocation_id="1" * 32,
                        arguments=("--output-json", remote_result),
                        artifacts=(remote_result,),
                    )
                )
            )
            reader = client.makefile("rb")
            frames = [decode_response(reader.readline()) for _ in range(4)]
            client.sendall(encode_message(AckRequest(invocation_id="1" * 32)))
            assert decode_response(reader.readline()).type == "acked"
        assert [frame.type for frame in frames] == ["stdout", "stderr", "artifact", "result"]
        assert isinstance(frames[2], ArtifactFrame)
        assert runner.commands[0][:2] == ("sh", "-c")
        assert runner.commands[0][2].index("rm -f --") < runner.commands[0][2].index("python")
        assert ".vibesys-evaluator-package/checker.py" in runner.commands[0][2]
        assert remote_result in runner.commands[0][2]
        assert bridge.socket_path is not None
        assert bridge.socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        bridge.close()


def test_bridge_releases_cluster_when_socket_startup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()

    class BrokenServer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise OSError("bind failed")  # noqa: TRY003

    monkeypatch.setattr(bridge_module, "_BridgeServer", BrokenServer)
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )

    with pytest.raises(OSError, match="bind failed"):
        bridge.start()

    assert runner.ensure_calls == 1
    assert runner.release_calls == 1
    assert bridge.socket_path is not None
    assert not bridge.socket_path.exists()


def test_bridge_rejects_special_workspace_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    bridge.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
            client.sendall(
                encode_message(EvaluationRequest(kind="accuracy", invocation_id="2" * 32))
            )
            frame = decode_response(client.makefile("rb").readline())
        assert isinstance(frame, ErrorFrame)
        assert frame.error == "ValueError"
        assert frame.message == "workspace contains a special file"
        assert runner.commands == []
    finally:
        bridge.close()
        bridge.close()

    assert runner.ensure_calls == 1
    assert runner.release_calls == 1
    assert bridge.socket_path is not None
    assert not bridge.socket_path.exists()


def test_bridge_rejects_workspace_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (workspace / "escape").symlink_to(outside)
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("python", "checker.py")},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    bridge.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
            client.sendall(
                encode_message(EvaluationRequest(kind="accuracy", invocation_id="3" * 32))
            )
            frame = decode_response(client.makefile("rb").readline())
        assert isinstance(frame, ErrorFrame)
        assert frame.error == "ValueError"
        assert frame.message == "workspace symlink escapes the project"
        assert runner.commands == []
    finally:
        bridge.close()


def test_bridge_logs_full_traceback_on_handler_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")
    runner = FakeRunner()
    logs: list[str] = []
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=logs.append,
    )
    bridge.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
            client.sendall(
                encode_message(EvaluationRequest(kind="accuracy", invocation_id="4" * 32))
            )
            frame = decode_response(client.makefile("rb").readline())
        assert isinstance(frame, ErrorFrame)
    finally:
        bridge.close()

    traceback_logs = [entry for entry in logs if "Traceback" in entry]
    assert len(traceback_logs) == 1
    assert "workspace contains a special file" in traceback_logs[0]


def _bridge_for_staging(
    tmp_path: Path, workspace: Path, *, hidden_paths: Sequence[Path] = ()
) -> SkyPilotBridge:
    return SkyPilotBridge(
        runner=FakeRunner(),  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=hidden_paths,
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )


def _staged_relative_paths(staging: Path) -> set[str]:
    return {path.relative_to(staging).as_posix() for path in staging.rglob("*") if path.is_file()}


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)  # noqa: S607


def test_stage_workspace_excludes_gitignored_directory_in_a_git_repo(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / ".gitignore").write_text("rust/target/\n")
    (workspace / "keep.py").write_text("candidate")
    (workspace / "rust").mkdir()
    (workspace / "rust" / "target").mkdir()
    (workspace / "rust" / "target" / "big.rlib").write_text("x" * 1000)

    bridge = _bridge_for_staging(tmp_path, workspace)
    staging = tmp_path / "staged"
    bridge._stage_workspace(staging)  # noqa: SLF001

    assert _staged_relative_paths(staging) == {"keep.py", ".gitignore"}


def test_stage_workspace_keeps_force_added_tracked_files_matching_gitignore(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / ".gitignore").write_text("*.log\n")
    (workspace / "keep.log").write_text("tracked despite matching *.log")
    subprocess.run(["git", "add", "-f", "keep.log"], cwd=workspace, check=True)  # noqa: S607

    bridge = _bridge_for_staging(tmp_path, workspace)
    staging = tmp_path / "staged"
    bridge._stage_workspace(staging)  # noqa: SLF001

    assert "keep.log" in _staged_relative_paths(staging)


def test_stage_workspace_excluding_gitignored_bytes_stays_under_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A build cache large enough to overflow the cap on its own must not
    # count against the cap once it is Git-ignored: this is the regression
    # covered here (e.g. a 761 MB `rust/target/` cache that previously
    # overflowed `_MAX_STAGED_BYTES` even though it carries no candidate
    # signal).
    monkeypatch.setattr(bridge_module, "_MAX_STAGED_BYTES", 10_000)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / ".gitignore").write_text("cache/\n")
    (workspace / "keep.py").write_text("candidate")
    (workspace / "cache").mkdir()
    (workspace / "cache" / "big.bin").write_bytes(b"x" * 50_000)

    bridge = _bridge_for_staging(tmp_path, workspace)
    staging = tmp_path / "staged"
    bridge._stage_workspace(staging)  # noqa: SLF001

    assert _staged_relative_paths(staging) == {"keep.py", ".gitignore"}


def test_stage_workspace_still_enforces_cap_for_tracked_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "_MAX_STAGED_BYTES", 10)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_repo(workspace)
    (workspace / "keep.py").write_text("this tracked file is well over the patched cap")

    bridge = _bridge_for_staging(tmp_path, workspace)

    with pytest.raises(ValueError, match="excluding Git-ignored paths"):
        bridge._stage_workspace(tmp_path / "staged")  # noqa: SLF001


def test_stage_workspace_falls_back_unchanged_for_a_non_git_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.py").write_text("candidate")
    (workspace / "build").mkdir()  # excluded by name, not by gitignore
    (workspace / "build" / "artifact.txt").write_text("built")
    (workspace / "not_a_cache").mkdir()
    (workspace / "not_a_cache" / "data.txt").write_text("kept even without git")

    bridge = _bridge_for_staging(tmp_path, workspace)
    staging = tmp_path / "staged"
    bridge._stage_workspace(staging)  # noqa: SLF001

    assert _staged_relative_paths(staging) == {"keep.py", "not_a_cache/data.txt"}


def test_stage_workspace_queries_git_ignores_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _init_git_repo(workspace)
    for index in range(5):
        (workspace / f"file{index}.py").write_text("candidate")

    calls: list[Sequence[str]] = []
    real_run = subprocess.run

    def spy_run(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "git":
            calls.append(argv)
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bridge_module.subprocess, "run", spy_run)
    bridge = _bridge_for_staging(tmp_path, workspace)
    bridge._stage_workspace(tmp_path / "staged")  # noqa: SLF001

    assert len(calls) == 1


def test_socket_binds_short_under_a_deep_state_namespace_path(tmp_path: Path) -> None:
    """The socket must not inherit length from the durable run state tree.

    Regression test for AF_UNIX "path too long": simulate the deep
    ``~/.vibesys/projects/<project-key>/runs/<run-id>/logs`` tree a real run
    produces and confirm the bridge still binds, with a socket path that
    stays comfortably under the sun_path limit.
    """
    deep_state_root = tmp_path
    for segment in (
        "projects",
        "sglang-multiturn-25b21a93e58e",
        "runs",
        "20260825-153042-a1b2c3d4-qwen35-mi300a-multiturn-eval",
        "logs",
    ):
        deep_state_root = deep_state_root / segment
    deep_state_root.mkdir(parents=True)
    assert len(os.fsencode(str(deep_state_root))) > bridge_module._MAX_SOCKET_PATH_BYTES  # noqa: SLF001

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(deep_state_root),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    bridge.start()
    try:
        assert len(os.fsencode(str(bridge.socket_path))) <= bridge_module._MAX_SOCKET_PATH_BYTES  # noqa: SLF001
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
    finally:
        bridge.close()


def test_close_removes_the_socket_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
    )
    bridge.start()
    socket_dir = bridge.socket_path.parent  # pyright: ignore[reportOptionalMemberAccess]
    assert socket_dir.is_dir()

    bridge.close()

    assert not socket_dir.exists()


def test_start_raises_a_clear_error_when_even_the_runtime_dir_is_too_long(
    tmp_path: Path,
) -> None:
    """Guard against environments where even a fresh runtime dir is too deep."""
    socket_root = tmp_path / ("x" * 150)
    socket_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()
    bridge = SkyPilotBridge(
        runner=runner,  # pyright: ignore[reportArgumentType]
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_Namespace(tmp_path / "state"),  # pyright: ignore[reportArgumentType]
        log=lambda _: None,
        socket_root=socket_root,
    )

    with pytest.raises(OSError, match="sun_path"):
        bridge.start()

    assert list(socket_root.iterdir()) == []
