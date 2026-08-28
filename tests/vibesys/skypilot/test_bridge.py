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
from vibesys.skypilot.runner import (
    ClusterInfo,
    ClusterStatus,
    JobResult,
    JobStatus,
    SkyPilotJobRunner,
)
from vs_project import StateNamespace

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vibesys.skypilot.runner import RemoteJobInfo


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
        self.cluster_status: ClusterStatus | None = ClusterStatus.UP

    def ensure_cluster(
        self,
        name: str,
        resources: ResolvedSkyPilotResources,  # noqa: ARG002
        *,
        timeout: float | None = 300,  # noqa: ARG002
    ) -> ClusterInfo:
        self.ensure_calls += 1
        return ClusterInfo(name, ClusterStatus.UP)

    def inspect_cluster(self, name: str, *, timeout: float = 60) -> ClusterInfo | None:  # noqa: ARG002
        if self.cluster_status is None:
            return None
        return ClusterInfo(name, self.cluster_status)

    def run(  # noqa: PLR0913
        self,
        cluster_name: str,
        resources: ResolvedSkyPilotResources,  # noqa: ARG002
        *,
        workdir: Path,
        command: Sequence[str],
        timeout: float | None = None,  # noqa: ARG002
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
        job_started: Callable[[int], None] | None = None,
        job_name: str | None = None,
        existing_job_id: int | None = None,
        log_tail: int = 0,  # noqa: ARG002
    ) -> JobResult:
        assert stdout_sink is not None
        assert stderr_sink is not None
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
        cluster_name: str,  # noqa: ARG002
        *,
        job_name: str,  # noqa: ARG002
        job_id: int | None = None,  # noqa: ARG002
        timeout: float = 60,  # noqa: ARG002
    ) -> RemoteJobInfo | None:
        return None

    def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:  # noqa: ARG002
        self.cancel_calls.append((cluster_name, job_id))

    def release(self, cluster_name: str, *, timeout: float = 60) -> None:  # noqa: ARG002
        self.release_calls += 1
        self.release_names.append(cluster_name)


def _namespace(tmp_path: Path) -> StateNamespace:
    root = tmp_path / ".vibesys" / "state" / "skypilot"
    root.mkdir(parents=True, exist_ok=True)
    return StateNamespace(project_root=tmp_path, root=root, portable=False)


def test_decoded_log_spool_resumes_from_durable_character_offset(tmp_path: Path) -> None:
    namespace = _namespace(tmp_path)
    journal = InvocationJournal(namespace)
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
    namespace = _namespace(tmp_path)
    journal = InvocationJournal(namespace)
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
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)
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


def test_terminal_replay_tracks_persisted_cluster_for_release(
    tmp_path: Path
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
    journal = InvocationJournal(namespace)
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


def test_framework_setup_wraps_new_job_argv_and_runs_first_in_workdir(
    tmp_path: Path,
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
        log=lambda _: None,
        framework_setup_command=setup,
    )
    bridge.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
            invocation_id = "4" * 32
            client.sendall(
                encode_message(EvaluationRequest(kind="accuracy", invocation_id=invocation_id))
            )
            reader = client.makefile("rb")
            frames = [decode_response(reader.readline()) for _ in range(3)]
            client.sendall(encode_message(AckRequest(invocation_id=invocation_id)))
            assert decode_response(reader.readline()).type == "acked"

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
        assert runner.workdirs[0] == bridge._snapshot(invocation_id)  # noqa: SLF001

        execution_root = tmp_path / "wrapper-execution"
        execution_root.mkdir()
        result = subprocess.run(  # noqa: S603
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
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = EvaluationRequest(kind="accuracy", invocation_id="5" * 32)
    command = ("python", "checker.py", "argument")

    def make_bridge(setup: str | None) -> SkyPilotBridge:
        return SkyPilotBridge(
            runner=FakeRunner(),
            cluster_name="lease",
            resources=_resources(),
            workspace=workspace,
            evaluator_package_root=None,
            hidden_paths=(),
            commands={"accuracy": command},
            benchmark_output_argument=None,
            state_namespace=_namespace(tmp_path),
            log=lambda _: None,
            framework_setup_command=setup,
        )

    legacy = hashlib.sha256(
        json.dumps(
            {
                "request": request.model_dump(mode="json", exclude={"invocation_id"}),
                "command": command,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    without_setup = make_bridge(None)
    with_setup = make_bridge("prepare one")
    changed_setup = make_bridge("prepare two")

    assert without_setup._request_digest(request, command) == legacy  # noqa: SLF001
    assert with_setup._request_digest(request, command) != legacy  # noqa: SLF001
    assert with_setup._request_digest(  # noqa: SLF001
        request, command
    ) != changed_setup._request_digest(request, command)  # noqa: SLF001


def test_framework_setup_failure_prevents_evaluator_execution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = SkyPilotBridge(
        runner=FakeRunner(),
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
        log=lambda _: None,
        framework_setup_command="exit 23",
    )
    command = bridge._with_framework_setup(  # noqa: SLF001
        ("sh", "-c", "touch evaluator-ran")
    )

    result = subprocess.run(  # noqa: S603
        command,
        cwd=workspace,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 23
    assert not (workspace / "evaluator-ran").exists()


def test_job_discovered_during_close_is_cancelled_and_released(
    tmp_path: Path
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
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)
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


def test_bridge_stages_allowlisted_command_streams_and_cleans_up(  # noqa: PLR0915
    tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("candidate")
    (workspace / ".env").write_text("SECRET=x")
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "large-cache").write_text("excluded")
    (workspace / ".vibesys-evaluator-tools").mkdir()
    (workspace / ".vibesys-evaluator-tools" / "poisoned").write_text("candidate")
    (workspace / ".vibesys-evaluator-toolchains").mkdir()
    (workspace / ".vibesys-evaluator-toolchains" / "poisoned").write_text("candidate")
    (workspace / ".bin").mkdir()
    (workspace / ".bin" / "cargo").write_text("candidate")
    (workspace / ".pip").mkdir()
    (workspace / ".pip" / "uv.py").write_text("candidate")
    (workspace / ".uv-cache").mkdir()
    (workspace / ".uv-cache" / "archive").write_text("candidate")
    (workspace / ".vibesys-evaluator-package").mkdir()
    (workspace / ".vibesys-evaluator-package" / "checker.py").write_text("candidate")
    (workspace / ".skyignore").write_text(".vibesys-evaluator-package\n")
    (workspace / "private").mkdir()
    (workspace / "private" / "token").write_text("secret")
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
        hidden_paths=(Path("private"),),
        commands={"benchmark": ("python", ".vibesys-evaluator-package/checker.py")},
        benchmark_output_argument="--output-json",
        state_namespace=_namespace(tmp_path),
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
        staged = runner.workdirs[0]
        assert not (staged / ".vibesys-evaluator-tools").exists()
        assert not (staged / ".vibesys-evaluator-toolchains").exists()
        assert not (staged / ".bin").exists()
        assert not (staged / ".pip").exists()
        assert not (staged / ".uv-cache").exists()
        assert (staged / ".vibesys-evaluator-package" / "checker.py").read_text() == "checker"
        assert staged.joinpath(".skyignore").read_text().startswith("# VibeSys")
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
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
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
        runner=runner,
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
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
    assert not bridge.socket_path.exists()


def test_bridge_rejects_workspace_symlink_escape(tmp_path: Path) -> None:
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
        runner=FakeRunner(),
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=hidden_paths,
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(tmp_path),
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


def _bridge_for(
    tmp_path: Path, state_root: Path | None = None, **kwargs: object
) -> SkyPilotBridge:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return SkyPilotBridge(
        runner=FakeRunner(),
        cluster_name="lease",
        resources=_resources(),
        workspace=workspace,
        evaluator_package_root=None,
        hidden_paths=(),
        commands={"accuracy": ("true",)},
        benchmark_output_argument=None,
        state_namespace=_namespace(state_root or tmp_path),
        log=lambda _: None,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def test_socket_binds_short_under_a_deep_state_namespace_path(tmp_path: Path) -> None:
    """The socket must not inherit length from the durable run state tree."""
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

    bridge = _bridge_for(tmp_path, deep_state_root)
    bridge.start()
    try:
        assert bridge.socket_path is not None
        assert len(os.fsencode(str(bridge.socket_path))) <= bridge_module._MAX_SOCKET_PATH_BYTES  # noqa: SLF001
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
    finally:
        bridge.close()


def test_close_removes_the_socket_directory(tmp_path: Path) -> None:
    bridge = _bridge_for(tmp_path)
    bridge.start()
    assert bridge.socket_path is not None
    socket_dir = bridge.socket_path.parent
    assert socket_dir.is_dir()

    bridge.close()

    assert not socket_dir.exists()


def test_start_raises_a_clear_error_when_even_the_runtime_dir_is_too_long(
    tmp_path: Path,
) -> None:
    """Guard against environments where even a fresh runtime dir is too deep."""
    socket_root = tmp_path / ("x" * 150)
    socket_root.mkdir()
    bridge = _bridge_for(tmp_path, socket_root=socket_root)

    with pytest.raises(OSError, match="sun_path"):
        bridge.start()

    assert list(socket_root.iterdir()) == []
