from __future__ import annotations

import io
import os
import re
import socket
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from tests.support import run_test_command

import vibesys.skypilot.bridge as bridge_module
from vibesys.skypilot.bridge import SkyPilotBridge
from vibesys.skypilot.config import ResolvedSkyPilotResources
from vibesys.skypilot.protocol import (
    AckRequest,
    ArtifactFrame,
    ErrorFrame,
    EvaluationRequest,
    ResponseFrame,
    decode_response,
    encode_message,
)
from vibesys.skypilot.recovery import (
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationPhase,
    InvocationProvenance,
    InvocationRecord,
    InvocationResultRecord,
)
from vibesys.skypilot.runner import (
    ClusterInfo,
    ClusterStatus,
    JobResult,
    JobStatus,
    SkyPilotJobRunner,
)
from vs_project.api import MAX_SOCKET_PATH_BYTES, SocketPathTooLongError, StateNamespace

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


def _send_evaluation(bridge: SkyPilotBridge, request: EvaluationRequest) -> list[ResponseFrame]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(bridge.socket_path))
        client.sendall(encode_message(request))
        reader = client.makefile("rb")
        frames: list[ResponseFrame] = []
        while True:
            line = reader.readline()
            assert line
            frame = decode_response(line)
            frames.append(frame)
            if frame.type in {"result", "error"}:
                break
        if frames[-1].type == "result":
            client.sendall(encode_message(AckRequest(invocation_id=request.invocation_id)))
            assert decode_response(reader.readline()).type == "acked"
    return frames


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
        resources: ResolvedSkyPilotResources,
        *,
        timeout: float | None = 300,
    ) -> ClusterInfo:
        del resources, timeout
        self.ensure_calls += 1
        return ClusterInfo(name, ClusterStatus.UP)

    def inspect_cluster(self, name: str, *, timeout: float = 60) -> ClusterInfo | None:
        del timeout
        if self.cluster_status is None:
            return None
        return ClusterInfo(name, self.cluster_status)

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


def test_startup_replacement_evidence_applies_only_to_preexisting_invocations(
    tmp_path: Path,
    socket_dir: Path,
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
        socket_path=socket_dir / "bridge.sock",
        log=lambda _: None,
    )
    journal = InvocationJournal(namespace)
    invocation_id = "f" * 32
    prepared = journal.prepare(invocation_id, "1" * 64, "2" * 64)
    runner.cluster_status = None
    bridge.start()
    runner.cluster_status = ClusterStatus.UP
    bridge._locally_prepared_invocations.add(invocation_id)  # noqa: SLF001  # LW-010027; marks an invocation prepared in this process for recovery discrimination

    assert bridge._recover_after_allocation_loss(prepared) is prepared  # noqa: SLF001  # LW-010028; verifies an unsubmitted journal record is retained after allocation loss
    submitting = journal.submitting(prepared, "lease", _attempt_resources())
    assert not bridge._allocation_was_replaced(submitting)  # noqa: SLF001  # LW-010029; ensures local preparation prevents a false replacement verdict

    bridge._locally_prepared_invocations.clear()  # noqa: SLF001  # LW-010030; removes the local-preparation evidence to model restart recovery
    assert bridge._allocation_was_replaced(submitting)  # noqa: SLF001  # LW-010031; verifies stale persisted attempts detect the replaced allocation
    bridge.close()
    assert set(runner.release_names) == {"lease"}


def test_terminal_replay_tracks_persisted_cluster_for_release(
    tmp_path: Path, socket_dir: Path
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
        socket_path=socket_dir / "first.sock",
        log=lambda _: None,
    )
    first_bridge.start()
    first_frames = _send_evaluation(first_bridge, request)
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
        socket_path=socket_dir / "replay.sock",
        log=lambda _: None,
    )
    replay_bridge.start()
    replay_frames = _send_evaluation(replay_bridge, request)
    replay_bridge.close()

    assert [frame.type for frame in first_frames] == ["stdout", "stderr", "result"]
    assert [frame.type for frame in replay_frames] == ["result"]
    assert first_runner.ensure_calls == 1
    assert replay_runner.ensure_calls == 1
    assert set(replay_runner.release_names) == {"new-lease", "old-lease"}


def test_framework_setup_wraps_new_job_argv_and_runs_first_in_workdir(
    tmp_path: Path,
    socket_dir: Path,
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
        socket_path=socket_dir / "bridge.sock",
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
    socket_dir: Path,
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
            socket_path=socket_dir / socket_name,
            log=lambda _: None,
            framework_setup_command=setup,
        ), runner

    without_setup, original_runner = make_bridge(None, "original.sock")
    without_setup.start()
    original_frames = _send_evaluation(without_setup, request)
    without_setup.close()
    with_setup, changed_runner = make_bridge("prepare one", "changed.sock")
    with_setup.start()
    conflict_frames = _send_evaluation(with_setup, request)
    with_setup.close()

    assert [frame.type for frame in original_frames] == ["stdout", "stderr", "result"]
    assert len(conflict_frames) == 1
    assert isinstance(conflict_frames[0], ErrorFrame)
    assert conflict_frames[0].error == "ValueError"
    assert original_runner.ensure_calls == changed_runner.ensure_calls == 1
    assert changed_runner.workdirs == []


def test_framework_setup_failure_prevents_evaluator_execution(
    tmp_path: Path,
    socket_dir: Path,
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
        socket_path=socket_dir / "bridge.sock",
        log=lambda _: None,
        framework_setup_command="exit 23",
    )

    bridge.start()
    try:
        frames = _send_evaluation(
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
    tmp_path: Path, socket_dir: Path
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
        socket_path=socket_dir / "bridge.sock",
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
    tmp_path: Path, socket_dir: Path
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
        socket_path=socket_dir / "bridge.sock",
        log=lambda _: None,
    )
    bridge.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.socket_path))
            remote_result = str(
                Path(tempfile.gettempdir()) / "vibesys-framework-benchmark-1-1.json"
            )
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
        assert bridge.socket_path.stat().st_mode & 0o777 == 0o600
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
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = FakeRunner()

    class BrokenServer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            _failure_message = "bind failed"
            raise OSError(_failure_message)

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
        socket_path=socket_dir / "bridge.sock",
        log=lambda _: None,
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


def test_bridge_rejects_special_workspace_file(tmp_path: Path, socket_dir: Path) -> None:
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
        socket_path=socket_dir / "bridge.sock",
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
        assert runner.commands == []
    finally:
        bridge.close()
        bridge.close()

    assert runner.ensure_calls == 1
    assert runner.release_calls == 1
    assert not bridge.socket_path.exists()


def test_bridge_rejects_workspace_symlink_escape(tmp_path: Path, socket_dir: Path) -> None:
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
        socket_path=socket_dir / "bridge.sock",
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


def test_artifact_stream_decodes_and_bounds_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _artifact_stream()
    good.feed(f"out\n{_BEGIN}\ne30=\n{_END}\n")
    assert good.result() == b"{}"
    assert _artifact_stream(expected=False).result() is None

    monkeypatch.setattr(bridge_module, "_MAX_ARTIFACT_BYTES", 2)
    big = _artifact_stream()
    big.feed(f"{_BEGIN}\nAAAA\n{_END}\n")
    with pytest.raises(ValueError, match="too large"):
        big.result()


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
    socket_dir: Path,
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
        socket_path=socket_dir / "bridge.sock",
        log=log or (lambda _: None),
        max_infrastructure_retries=max_retries,
    )


def _send_raw(bridge: SkyPilotBridge, payload: bytes) -> ResponseFrame:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(bridge.socket_path))
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        return decode_response(client.makefile("rb").readline())


@pytest.mark.parametrize(
    "payload",
    [
        b"no newline framing",
        encode_message(EvaluationRequest(kind="benchmark", invocation_id="4" * 32)),
        encode_message(
            EvaluationRequest(kind="accuracy", invocation_id="4" * 32, arguments=("--x",))
        ),
        encode_message(
            EvaluationRequest(kind="accuracy", invocation_id="4" * 32, artifacts=("artifact.json",))
        ),
    ],
    ids=["framing", "unconfigured-kind", "arguments", "artifacts"],
)
def test_bridge_rejects_malformed_or_unauthorized_requests(
    tmp_path: Path, socket_dir: Path, payload: bytes
) -> None:
    runner = FakeRunner()
    bridge = _bridge(tmp_path, socket_dir, runner)
    bridge.start()
    try:
        frame = _send_raw(bridge, payload)
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "ValueError"
    assert runner.commands == []


def test_bridge_cannot_restart_after_close(tmp_path: Path, socket_dir: Path) -> None:
    bridge = _bridge(tmp_path, socket_dir)
    bridge.close()

    with pytest.raises(RuntimeError, match="cannot be restarted after close"):
        bridge.start()


def test_bridge_close_logs_failed_cancel_and_release_and_continues(
    tmp_path: Path, socket_dir: Path
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
    bridge = _bridge(tmp_path, socket_dir, FailingRunner(), logs.append)
    bridge.start()
    _private(bridge, "_active_jobs").add(("lease", 3))

    bridge.close()

    assert "[warn] SkyPilot job cancellation failed: RuntimeError" in logs
    assert "[warn] SkyPilot allocation release failed: OSError" in logs
    assert not bridge.socket_path.exists()


def test_handler_reports_value_error_frame_once_shutdown_has_started(
    tmp_path: Path, socket_dir: Path
) -> None:
    bridge = _bridge(tmp_path, socket_dir)
    early = io.BytesIO()
    _set_private(bridge, "_closed", value=True)
    _private(bridge, "_handle")(io.BytesIO(), early, socket.socket())
    assert decode_response(early.getvalue()) == ErrorFrame(error="ValueError")

    class CloseWhileLocked:
        def __enter__(self) -> None:
            _set_private(bridge, "_closed", value=True)

        def __exit__(self, *_: object) -> None:
            return None

    _set_private(bridge, "_closed", value=False)
    _set_private(bridge, "_evaluation_lock", value=CloseWhileLocked())
    late = io.BytesIO()
    _private(bridge, "_handle")(io.BytesIO(), late, socket.socket())
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
    tmp_path: Path, socket_dir: Path, phase: InvocationPhase
) -> None:
    runner = FakeRunner()
    logs: list[str] = []
    bridge = _bridge(tmp_path, socket_dir, runner, logs.append)
    request = EvaluationRequest(kind="accuracy", invocation_id="5" * 32)
    request_digest, snapshot_digest = _digests(bridge, request)
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare(request.invocation_id, request_digest, snapshot_digest)
    record = journal.submitting(record, "lease", _attempt_resources())
    if phase is InvocationPhase.SUBMITTED:
        record = journal.submitted(record, 9, "lease")
    bridge.start()
    try:
        frame = _send_raw(bridge, encode_message(request))
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "RuntimeError"
    assert runner.commands == []
    persisted = journal.load(request.invocation_id)
    assert persisted is not None
    assert persisted.phase is record.phase


def test_bridge_rejects_completion_without_attempt_resources(
    tmp_path: Path, socket_dir: Path
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

    bridge = _bridge(tmp_path, socket_dir, ForgetfulRunner())
    bridge.start()
    try:
        frame = _send_raw(
            bridge, encode_message(EvaluationRequest(kind="accuracy", invocation_id="6" * 32))
        )
    finally:
        bridge.close()

    assert isinstance(frame, ErrorFrame)
    assert frame.error == "ValueError"
    assert InvocationJournal(namespace).load("6" * 32) is None


def test_retry_limit_is_enforced_from_persisted_attempt_count(
    tmp_path: Path, socket_dir: Path
) -> None:
    bridge = _bridge(tmp_path, socket_dir, max_retries=0)
    journal = InvocationJournal(_namespace(tmp_path))
    record = journal.prepare("7" * 32, "1" * 64, "2" * 64)
    record = journal.submitting(record, "lease", _attempt_resources())

    with pytest.raises(RuntimeError, match="retry limit was exhausted"):
        _private(bridge, "_retry_after_infrastructure_failure")(record)
    assert _private(bridge, "_recover_after_allocation_loss")(
        journal.prepare("8" * 32, "1" * 64, "2" * 64)
    )


def test_ensure_current_cluster_releases_racing_launch_and_reports_closing(
    tmp_path: Path, socket_dir: Path
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
    bridge = _bridge(tmp_path, socket_dir, runner, logs.append)
    runner.bridge = bridge

    with pytest.raises(RuntimeError, match="bridge is closing"):
        _private(bridge, "_ensure_current_cluster_for_work")()

    assert runner.release_names == ["lease"]
    assert logs == ["[warn] SkyPilot allocation release failed: OSError"]


def test_job_started_during_close_logs_failed_cancel_then_refuses(
    tmp_path: Path, socket_dir: Path
) -> None:
    class FailingCancel(FakeRunner):
        def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:
            del cluster_name, job_id, timeout
            message = "cancel broke"
            raise OSError(message)

    logs: list[str] = []
    bridge = _bridge(tmp_path, socket_dir, FailingCancel(), logs.append)
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
        status="COMPLETED",
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
    tmp_path: Path, socket_dir: Path
) -> None:
    bridge = _bridge(tmp_path, socket_dir)
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
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _bridge(tmp_path, socket_dir)
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    os.mkfifo(snapshot / "pipe")
    with pytest.raises(ValueError, match="unsupported file type"):
        _private(bridge, "_snapshot_digest")(snapshot)

    (tmp_path / "workspace" / "big.txt").write_text("x")
    monkeypatch.setattr(bridge_module, "_MAX_STAGED_FILES", 0)
    with pytest.raises(ValueError, match="exceeds remote staging limits"):
        _private(bridge, "_stage_workspace")(tmp_path / "stage")
