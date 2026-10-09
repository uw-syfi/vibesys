"""Contract tests for the host command broker and the client that talks to it.

The broker serves an agent's Slurm requests from the submit host. Everything
here goes through its public surface and a real Unix socket, with a recording
launcher and gate runner standing in for ``srun`` and the cluster.
"""

from __future__ import annotations

import ast
import base64
import json
import posixpath
import socket
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    HOST_COMMAND_CLIENT,
    GateKind,
    Gates,
    GpuCommand,
    GpuCommands,
    GpuJobRequest,
    HostCommandBroker,
    RunRoots,
    SlurmGpuConfig,
)

# test-isolation: main is the CLI entry point and is intentionally absent from the library API.
from vs_sandbox.benchmark_output import BenchmarkOutputKind, classify_benchmark_output
from vs_sandbox.host_command_client import main as client_main

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

_OUTPUT_ARGUMENT = "--vs-output"
_FRAMEWORK_RESULT = "/tmp/vibesys-framework-benchmark-0123456789abcdef.json"  # noqa: S108  # lint-waiver: LW-954372 [S108]; the framework's fixed result path shape under test.


class _RecordingLauncher:
    """Fake launcher: records the job and optionally blocks until it is cancelled."""

    def __init__(self, *, block: bool = False) -> None:
        self.commands: list[GpuCommand] = []
        self.requests: list[GpuJobRequest] = []
        self.cancelled = threading.Event()
        self._block = block

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: threading.Event,
    ) -> int:
        self.requests.append(request)
        self.commands.append(command)
        write(b"started\n")
        if self._block and cancel.wait(10):
            self.cancelled.set()
        return 5


class _RecordingGates:
    """Fake gate runner: records each gate, can write the result file, can block."""

    def __init__(self, *, block: bool = False, result: bytes | None = None) -> None:
        self.runs: list[tuple[GateKind, tuple[str, ...], Path]] = []
        self.cancelled = threading.Event()
        self._block = block
        self._result = result

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: threading.Event,
    ) -> int:
        self.runs.append((kind, tuple(arguments), cwd))
        write(f"gate {kind.value}\n".encode())
        if self._result is not None and arguments:
            Path(arguments[-1]).write_bytes(self._result)
        if self._block and cancel.wait(10):
            self.cancelled.set()
        return 3


class _PrefixConfinement:
    """Fake confinement that records the workspace it was asked to confine to."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        return ["confine", str(workspace), *argv]


def _config(**updates: object) -> SlurmGpuConfig:
    values: dict[str, object] = {
        "partitions": ("main",),
        "max_gpus": 8,
        "max_time_minutes": 120,
    }
    values.update(updates)
    return SlurmGpuConfig.model_validate(values)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    return root


def _gpu(launcher: _RecordingLauncher, host_env: Mapping[str, str] | None = None) -> GpuCommands:
    return GpuCommands(_config(), _PrefixConfinement(), host_env or {}, launcher=launcher)


def _gates(gates: _RecordingGates, output_argument: str | None = _OUTPUT_ARGUMENT) -> Gates:
    return Gates(gates, output_argument)


@contextmanager
def _serving(
    tmp_path: Path,
    workspace: Path,
    *,
    gpu: GpuCommands | None = None,
    gates: Gates | None = None,
) -> Iterator[HostCommandBroker]:
    broker = HostCommandBroker(
        tmp_path / "broker.sock",
        roots=RunRoots((workspace,), (tmp_path / "worktrees",)),
        gpu=gpu,
        gates=gates,
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


def _point_at(monkeypatch: pytest.MonkeyPatch, broker: HostCommandBroker) -> None:
    monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(broker.socket_path))
    monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, broker.token)


def _raw_request(broker: HostCommandBroker, request: Mapping[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(broker.socket_path))
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("rb") as frames:
            return json.loads(frames.readline())


def _gpu_call(broker: HostCommandBroker, cwd: str, **updates: object) -> dict[str, object]:
    call: dict[str, object] = {
        "op": "gpu",
        "token": broker.token,
        "argv": ["true"],
        "cwd": cwd,
        "gpus": 1,
        "time_minutes": 1,
        "env": {},
    }
    return {**call, **updates}


def _gate_call(broker: HostCommandBroker, cwd: str, **updates: object) -> dict[str, object]:
    call: dict[str, object] = {
        "op": "gate",
        "token": broker.token,
        "kind": "accuracy",
        "arguments": [],
        "cwd": cwd,
    }
    return {**call, **updates}


class TestGpuOperation:
    def test_confines_the_command_and_relays_output_and_status(
        self,
        tmp_path: Path,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        launcher = _RecordingLauncher()
        monkeypatch.chdir(workspace / "sub")
        monkeypatch.setenv("KEEP_ME", "1")
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
        with _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker:
            _point_at(monkeypatch, broker)
            status = client_main(["--gpus", "4", "--time", "9", "--", "nvidia-smi"])

        command = launcher.commands[0]
        assert status == 5
        assert capsysbinary.readouterr().out == b"started\n"
        assert launcher.requests == [GpuJobRequest(gpus=4, time_minutes=9)]
        assert command.argv[:2] == ("confine", str(workspace.resolve()))
        assert command.argv[-2:] == (str((workspace / "sub").resolve()), "nvidia-smi")
        assert command.env["KEEP_ME"] == "1"
        assert "CUDA_VISIBLE_DEVICES" not in command.env

    def test_a_candidate_worktree_is_confined_to_itself(
        self, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = tmp_path / "worktrees" / "candidate-1"
        (candidate / "src").mkdir(parents=True)
        launcher = _RecordingLauncher()
        monkeypatch.chdir(candidate / "src")
        with _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker:
            _point_at(monkeypatch, broker)
            client_main(["--gpus", "1", "--time", "1", "--", "true"])

        assert launcher.commands[0].argv[1] == str(candidate.resolve())

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"token": "wrong"}, "invalid command broker capability"),
            ({"cwd": "/"}, "inside the run's workspace"),
            ({"gpus": 9}, "operator limit of 8"),
        ],
    )
    def test_rejects_requests_outside_the_capability(
        self,
        tmp_path: Path,
        workspace: Path,
        overrides: Mapping[str, object],
        message: str,
    ) -> None:
        launcher = _RecordingLauncher()
        with _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker:
            reply = _raw_request(broker, {**_gpu_call(broker, str(workspace)), **overrides})

        assert message in str(reply["error"])
        assert launcher.commands == []

    def test_the_job_gets_the_host_baseline_and_none_of_the_containers_identity(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        launcher = _RecordingLauncher()
        host = {"PATH": "/host/bin", "HOME": "/home/host", "ANTHROPIC_API_KEY": "secret"}
        container = {
            "PATH": "/container/bin",
            "HOME": "/home/agent",
            "FOO": "bar",
            "SLURM_JOB_ID": "7",
            "VIBESYS_COMMAND_BROKER_TOKEN": "t",
            "XDG_CACHE_HOME": "/home/agent/.cache",
        }
        with _serving(tmp_path, workspace, gpu=_gpu(launcher, host)) as broker:
            _raw_request(broker, _gpu_call(broker, str(workspace), env=container))

        assert launcher.commands[0].env == {"PATH": "/host/bin", "HOME": "/home/host", "FOO": "bar"}

    def test_closing_the_connection_cancels_the_job(self, tmp_path: Path, workspace: Path) -> None:
        launcher = _RecordingLauncher(block=True)
        with _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(broker.socket_path))
                client.sendall(json.dumps(_gpu_call(broker, str(workspace))).encode() + b"\n")
                with client.makefile("rb") as frames:
                    first = json.loads(frames.readline())
            cancelled = launcher.cancelled.wait(10)

        assert base64.b64decode(first["output"]) == b"started\n"
        assert cancelled

    def test_closing_the_broker_cancels_running_jobs_and_waits_for_them(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """A run that exits leaves no job behind: close() returns after the cancel was acted on."""
        launcher = _RecordingLauncher(block=True)
        with (
            _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker,
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client,
        ):
            client.connect(str(broker.socket_path))
            client.sendall(json.dumps(_gpu_call(broker, str(workspace))).encode() + b"\n")
            with client.makefile("rb") as frames:
                frames.readline()  # the job is running, and this connection stays open
                broker.close()

                assert launcher.cancelled.is_set()

    def test_an_operation_the_run_does_not_offer_is_refused_by_name(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        with _serving(tmp_path, workspace, gates=_gates(_RecordingGates())) as broker:
            reply = _raw_request(broker, _gpu_call(broker, str(workspace)))

        assert "does not offer the gpu operation" in str(reply["error"])


class TestGateOperation:
    def test_runs_the_planned_gate_from_the_requested_directory(
        self,
        tmp_path: Path,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        gates = _RecordingGates()
        monkeypatch.chdir(workspace)
        with _serving(tmp_path, workspace, gates=_gates(gates)) as broker:
            _point_at(monkeypatch, broker)
            status = client_main(["--gate", "accuracy"])

        assert status == 3
        assert capsysbinary.readouterr().out == b"gate accuracy\n"
        assert gates.runs == [(GateKind.ACCURACY, (), workspace.resolve())]

    def test_a_workspace_result_path_passes_through_unchanged(
        self, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gates = _RecordingGates()
        monkeypatch.chdir(workspace)
        with _serving(tmp_path, workspace, gates=_gates(gates)) as broker:
            _point_at(monkeypatch, broker)
            client_main(["--gate", "benchmark", _OUTPUT_ARGUMENT, ".vibesys-benchmark-x1.json"])

        assert gates.runs[0][1] == (_OUTPUT_ARGUMENT, ".vibesys-benchmark-x1.json")

    def test_a_framework_result_is_written_on_the_host_and_relayed_to_the_caller(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """The gate writes a host file of the same allowed shape; the caller gets its contents."""
        gates = _RecordingGates(result=b'{"ok": true}')
        with _serving(tmp_path, workspace, gates=_gates(gates)) as broker:
            frames = _frames(
                broker,
                _gate_call(
                    broker,
                    str(workspace),
                    kind="benchmark",
                    arguments=[_OUTPUT_ARGUMENT, _FRAMEWORK_RESULT],
                ),
            )

        argument = gates.runs[0][1]
        assert argument[0] == _OUTPUT_ARGUMENT
        assert argument[1] != _FRAMEWORK_RESULT
        # The wrapper that runs the gate accepts exactly this shape, and the broker removed it.
        assert classify_benchmark_output(argument[1]) is BenchmarkOutputKind.FRAMEWORK
        assert not Path(argument[1]).exists()
        files = [frame["file"] for frame in frames if "file" in frame]
        assert len(files) == 1
        assert files[0]["path"] == _FRAMEWORK_RESULT
        assert base64.b64decode(files[0]["data"]) == b'{"ok": true}'
        assert frames[-1] == {"exit": 3}

    def test_the_client_writes_the_relayed_file_where_the_caller_asked(
        self, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gates = _RecordingGates(result=b"relayed")
        # The framework names this fixed /tmp shape; a fresh nonce keeps runs apart.
        target = Path(f"/tmp/vibesys-framework-benchmark-{uuid.uuid4().hex}.json")  # noqa: S108  # lint-waiver: LW-954374 [S108]; the framework's result path shape under test.
        monkeypatch.chdir(workspace)
        try:
            with _serving(tmp_path, workspace, gates=_gates(gates)) as broker:
                _point_at(monkeypatch, broker)
                status = client_main(["--gate", "benchmark", _OUTPUT_ARGUMENT, str(target)])

            assert status == 3
            assert target.read_bytes() == b"relayed"
            assert list(target.parent.glob(f".{target.name}*")) == []
        finally:
            target.unlink(missing_ok=True)

    def test_gate_arguments_are_accepted_only_in_the_planned_shapes(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        gates = _RecordingGates()
        valid_results = {".vibesys-benchmark-ok.json", _FRAMEWORK_RESULT}

        @settings(max_examples=60, deadline=None)
        @given(
            kind=st.sampled_from(list(GateKind)),
            arguments=st.lists(
                st.sampled_from(
                    [
                        _OUTPUT_ARGUMENT,
                        "--other",
                        "x",
                        ".vibesys-benchmark-ok.json",
                        ".vibesys-benchmark-/../escape.json",
                        _FRAMEWORK_RESULT,
                        "/tmp/vibesys-framework-benchmark-../x.json",  # noqa: S108  # lint-waiver: LW-954373 [S108]; a rejected path under test.
                        "/etc/passwd",
                    ]
                ),
                max_size=3,
            ),
        )
        def check(kind: GateKind, arguments: list[str]) -> None:
            runs_before = len(gates.runs)
            frames = _frames(
                broker,
                _gate_call(broker, str(workspace), kind=kind.value, arguments=arguments),
            )
            expected = (kind is GateKind.ACCURACY and arguments == []) or (
                kind is GateKind.BENCHMARK
                and len(arguments) == 2
                and arguments[0] == _OUTPUT_ARGUMENT
                and arguments[1] in valid_results
            )
            assert (len(gates.runs) > runs_before) == expected
            if expected and kind is GateKind.BENCHMARK:
                # What the broker hands the gate wrapper is itself an allowed result path:
                # the wrapper validates it again with the same function.
                assert classify_benchmark_output(gates.runs[-1][1][1]) is not None
            if not expected:
                assert "invalid arguments" in str(frames[0]["error"])

        with _serving(tmp_path, workspace, gates=_gates(gates)) as broker:
            check()

    def test_a_benchmark_without_a_declared_result_argument_takes_no_arguments(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        gates = _RecordingGates()
        with _serving(tmp_path, workspace, gates=_gates(gates, None)) as broker:
            refused = _frames(
                broker,
                _gate_call(broker, str(workspace), kind="benchmark", arguments=["--x", "y"]),
            )
            accepted = _frames(broker, _gate_call(broker, str(workspace), kind="benchmark"))

        assert "invalid arguments" in str(refused[0]["error"])
        assert accepted[-1] == {"exit": 3}

    def test_closing_the_connection_cancels_the_gate(self, tmp_path: Path, workspace: Path) -> None:
        gates = _RecordingGates(block=True)
        with _serving(tmp_path, workspace, gates=_gates(gates)) as broker:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(broker.socket_path))
                client.sendall(json.dumps(_gate_call(broker, str(workspace))).encode() + b"\n")
                with client.makefile("rb") as frames:
                    first = json.loads(frames.readline())
            cancelled = gates.cancelled.wait(10)

        assert base64.b64decode(first["output"]) == b"gate accuracy\n"
        assert cancelled


def _frames(broker: HostCommandBroker, request: Mapping[str, object]) -> list[dict[str, Any]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(broker.socket_path))
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("rb") as stream:
            return [json.loads(line) for line in stream]


class TestRoots:
    def test_a_directory_is_accepted_exactly_when_it_is_inside_the_runs_roots(
        self, tmp_path: Path
    ) -> None:
        """Whatever path the container names, the broker agrees with a path model of the roots."""
        workspace = tmp_path / "workspace"
        worktrees = tmp_path / "worktrees"
        workspace.mkdir()
        worktrees.mkdir()
        starts = {
            "workspace": str(workspace),
            "worktrees": str(worktrees),
            "other": str(tmp_path / "other"),
            "root": "/",
        }
        launcher = _RecordingLauncher()

        @settings(max_examples=80, deadline=None)
        @given(
            base=st.sampled_from(sorted(starts)),
            parts=st.lists(st.sampled_from(["a", "b", "..", "."]), max_size=4),
        )
        def check(base: str, parts: list[str]) -> None:
            commands_before = len(launcher.commands)
            named = posixpath.join(starts[base], *parts)
            reply = _frames(broker, _gpu_call(broker, named))
            cwd = posixpath.normpath(named)
            ws, wt = str(workspace.resolve()), str(worktrees.resolve())
            inside_workspace = cwd == ws or cwd.startswith(ws + "/")
            inside_candidate = cwd.startswith(wt + "/")
            started = launcher.commands[commands_before:]
            accepted = bool(started)
            assert accepted == (inside_workspace or inside_candidate)
            if accepted:
                # The job is confined to the run's workspace or to one candidate under the root.
                confined = started[0].argv[1]
                assert confined == ws or confined.startswith(wt + "/")
                assert reply[-1] == {"exit": 5}

        with _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker:
            check()

    def test_a_symlink_inside_the_workspace_cannot_reach_outside_it(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "link").symlink_to(outside)
        launcher = _RecordingLauncher()
        with _serving(tmp_path, workspace, gpu=_gpu(launcher)) as broker:
            reply = _frames(broker, _gpu_call(broker, str(workspace / "link")))

        assert "inside the run's workspace" in str(reply[0]["error"])
        assert launcher.commands == []


class TestClient:
    def test_without_a_broker_it_refuses(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(COMMAND_BROKER_SOCKET_ENV, raising=False)
        monkeypatch.delenv(COMMAND_BROKER_TOKEN_ENV, raising=False)
        assert client_main(["--", "nvidia-smi"]) == 2
        assert "no command broker" in capsys.readouterr().err

    def test_an_unknown_gate_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, "/nonexistent")
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, "t")
        assert client_main(["--gate", "profile"]) == 2
        assert "--gate takes one of" in capsys.readouterr().err

    def test_a_dropped_connection_is_a_failure_not_a_success(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "dead.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)

        def accept_and_drop() -> None:
            connection, _ = server.accept()
            connection.close()

        thread = threading.Thread(target=accept_and_drop)
        thread.start()
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(path))
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, "t")
        try:
            status = client_main(["--", "true"])
        finally:
            thread.join()
            server.close()

        assert status == 1
        assert "closed the connection" in capsys.readouterr().err

    def test_the_client_imports_only_the_standard_library(self) -> None:
        """It runs as one file under the agent image's python3, with no VibeSys packages."""
        tree = ast.parse(HOST_COMMAND_CLIENT.read_text())
        imported = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert imported <= set(__import__("sys").stdlib_module_names)
