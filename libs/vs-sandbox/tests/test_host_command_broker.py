"""Contract tests for the host command broker and the client that talks to it.

The broker serves an agent's Slurm requests from the submit host. Everything
here goes through its public surface over the simulated network, with every
handler a simulated thread and a recording launcher and gate runner standing in
for ``srun`` and the cluster. The real Unix socket is covered by
``tests/e2e/test_host_command_broker_unix_socket.py``.
"""

from __future__ import annotations

import ast
import base64
import json
import posixpath
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    HOST_COMMAND_CLIENT,
    AgentGpuConfig,
    BenchmarkOutputKind,
    BrokerTransport,
    GateKind,
    Gates,
    GpuCommand,
    GpuCommands,
    GpuJobRequest,
    HostCommandBroker,
    RunRoots,
    classify_benchmark_output,
)

# test-isolation: the client is a single-file CLI that is intentionally absent from the library API.
from vs_sandbox.host_command_client import Stop, execute
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from vs_sim.api import Connection, Event
    from vs_sim.api.testing import Sim

SEEDS = st.one_of(st.none(), st.integers(0, 2**32))

_OUTPUT_ARGUMENT = "--vs-output"
_FRAMEWORK_RESULT = "/tmp/vibesys-framework-benchmark-0123456789abcdef.json"  # noqa: S108  # lint-waiver: LW-954372 [S108]; the framework's fixed result path shape under test.


class _RecordingLauncher:
    """Fake launcher: records the job and optionally blocks until it is cancelled."""

    def __init__(self, threads: SimThreads, *, block: bool = False) -> None:
        self.commands: list[GpuCommand] = []
        self.requests: list[GpuJobRequest] = []
        self.cancelled = threads.event()
        self._block = block

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        self.requests.append(request)
        self.commands.append(command)
        write(b"started\n")
        if self._block and cancel.wait(HANG_GUARD_S):
            self.cancelled.set()
        return 5


class _RecordingGates:
    """Fake gate runner: records each gate, can write the result file, can block."""

    def __init__(
        self, threads: SimThreads, *, block: bool = False, result: bytes | None = None
    ) -> None:
        self.runs: list[tuple[GateKind, tuple[str, ...], Path]] = []
        self.cancelled = threads.event()
        self._block = block
        self._result = result

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        self.runs.append((kind, tuple(arguments), cwd))
        write(f"gate {kind.value}\n".encode())
        if self._result is not None and arguments:
            Path(arguments[-1]).write_bytes(self._result)
        if self._block and cancel.wait(HANG_GUARD_S):
            self.cancelled.set()
        return 3


class _PrefixConfinement:
    """Fake confinement that records the workspace it was asked to confine to."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        return ["confine", str(workspace), *argv]


def _config(**updates: object) -> AgentGpuConfig:
    values: dict[str, object] = {
        "partitions": ("main",),
        "max_gpus": 8,
        "max_time_minutes": 120,
    }
    values.update(updates)
    return AgentGpuConfig.model_validate(values)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    return root


def _gpu(launcher: _RecordingLauncher, host_env: Mapping[str, str] | None = None) -> GpuCommands:
    return GpuCommands(_config(), _PrefixConfinement(), host_env or {}, launcher=launcher)


def _gates(gates: _RecordingGates, output_argument: str | None = _OUTPUT_ARGUMENT) -> Gates:
    return Gates(gates, output_argument)


@dataclass(frozen=True)
class _Network:
    """The simulated network the broker listens on and the clients dial."""

    threads: SimThreads
    network: SimNetwork

    @property
    def transport(self) -> BrokerTransport:
        return BrokerTransport(self.network, self.threads)

    def dial(self, address: str) -> Connection:
        return self.network.connect(address, HANG_GUARD_S)


def _network(threads: SimThreads) -> _Network:
    return _Network(threads, SimNetwork(threads))


@contextmanager
def _serving(
    net: _Network,
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
        transport=net.transport,
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


def _point_at(monkeypatch: pytest.MonkeyPatch, broker: HostCommandBroker) -> None:
    monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(broker.socket_path))
    monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, broker.token)


def _run_client(net: _Network, arguments: list[str]) -> int:
    """Run the agent-side client against the simulated network, as ``vibesys-gpu`` would."""
    return execute(arguments, stop=Stop(), dial=net.dial)


def _send_request(
    net: _Network, broker: HostCommandBroker, request: Mapping[str, object]
) -> Connection:
    connection = net.dial(str(broker.socket_path))
    connection.send(json.dumps(request).encode() + b"\n")
    return connection


def _read_frame(connection: Connection) -> dict[str, Any]:
    line = b""
    while not line.endswith(b"\n"):
        chunk = connection.recv(1, HANG_GUARD_S)
        assert chunk, "the broker closed the connection before finishing a frame"
        line += chunk
    return json.loads(line)


def _raw_request(
    net: _Network, broker: HostCommandBroker, request: Mapping[str, object]
) -> dict[str, object]:
    connection = _send_request(net, broker, request)
    try:
        return _read_frame(connection)
    finally:
        connection.close()


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


def _simulate[T](sim: Sim, program: Callable[[_Network], T]) -> T:
    """Run *program* as the main simulated thread, on a network all its threads share."""
    threads = sim.threads()
    net = _network(threads)
    return threads.run(lambda: program(net))


class TestGpuOperation:
    def test_confines_the_command_and_relays_output_and_status(
        self,
        sim: Sim,
        tmp_path: Path,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        monkeypatch.chdir(workspace / "sub")
        monkeypatch.setenv("KEEP_ME", "1")
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")

        def program(net: _Network) -> tuple[int, _RecordingLauncher]:
            launcher = _RecordingLauncher(net.threads)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                _point_at(monkeypatch, broker)
                status = _run_client(net, ["--gpus", "4", "--time", "9", "--", "nvidia-smi"])
            return status, launcher

        status, launcher = _simulate(sim, program)

        command = launcher.commands[0]
        assert status == 5
        assert capsysbinary.readouterr().out == b"started\n"
        assert launcher.requests == [GpuJobRequest(gpus=4, time_minutes=9)]
        assert command.argv[:2] == ("confine", str(workspace.resolve()))
        assert command.argv[-2:] == (str((workspace / "sub").resolve()), "nvidia-smi")
        assert command.env["KEEP_ME"] == "1"
        assert "CUDA_VISIBLE_DEVICES" not in command.env

    def test_a_candidate_worktree_is_confined_to_itself(
        self, sim: Sim, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = tmp_path / "worktrees" / "candidate-1"
        (candidate / "src").mkdir(parents=True)
        monkeypatch.chdir(candidate / "src")

        def program(net: _Network) -> _RecordingLauncher:
            launcher = _RecordingLauncher(net.threads)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                _point_at(monkeypatch, broker)
                _run_client(net, ["--gpus", "1", "--time", "1", "--", "true"])
            return launcher

        assert _simulate(sim, program).commands[0].argv[1] == str(candidate.resolve())

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
        sim: Sim,
        tmp_path: Path,
        workspace: Path,
        overrides: Mapping[str, object],
        message: str,
    ) -> None:
        def program(net: _Network) -> tuple[dict[str, object], _RecordingLauncher]:
            launcher = _RecordingLauncher(net.threads)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                call = {**_gpu_call(broker, str(workspace)), **overrides}
                return _raw_request(net, broker, call), launcher

        reply, launcher = _simulate(sim, program)

        assert message in str(reply["error"])
        assert launcher.commands == []

    def test_the_job_gets_the_host_baseline_and_none_of_the_containers_identity(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        host = {"PATH": "/host/bin", "HOME": "/home/host", "ANTHROPIC_API_KEY": "secret"}
        container = {
            "PATH": "/container/bin",
            "HOME": "/home/agent",
            "FOO": "bar",
            "SLURM_JOB_ID": "7",
            "VIBESYS_COMMAND_BROKER_TOKEN": "t",
            "XDG_CACHE_HOME": "/home/agent/.cache",
        }

        def program(net: _Network) -> _RecordingLauncher:
            launcher = _RecordingLauncher(net.threads)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher, host)) as broker:
                _raw_request(net, broker, _gpu_call(broker, str(workspace), env=container))
            return launcher

        launcher = _simulate(sim, program)

        assert launcher.commands[0].env == {"PATH": "/host/bin", "HOME": "/home/host", "FOO": "bar"}

    def test_closing_the_connection_cancels_the_job(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        def program(net: _Network) -> tuple[dict[str, Any], bool]:
            launcher = _RecordingLauncher(net.threads, block=True)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                client = _send_request(net, broker, _gpu_call(broker, str(workspace)))
                first = _read_frame(client)
                client.close()
                return first, launcher.cancelled.wait(HANG_GUARD_S)

        first, cancelled = _simulate(sim, program)

        assert base64.b64decode(first["output"]) == b"started\n"
        assert cancelled

    def test_closing_the_broker_cancels_running_jobs_and_waits_for_them(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        """A run that exits leaves no job behind: close() returns after the cancel was acted on."""

        def program(net: _Network) -> bool:
            launcher = _RecordingLauncher(net.threads, block=True)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                client = _send_request(net, broker, _gpu_call(broker, str(workspace)))
                _read_frame(client)  # the job is running, and this connection stays open
                broker.close()
                cancelled = launcher.cancelled.is_set()
                client.close()
            return cancelled

        assert _simulate(sim, program)

    def test_an_operation_the_run_does_not_offer_is_refused_by_name(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        def program(net: _Network) -> dict[str, object]:
            gates = _RecordingGates(net.threads)
            with _serving(net, tmp_path, workspace, gates=_gates(gates)) as broker:
                return _raw_request(net, broker, _gpu_call(broker, str(workspace)))

        reply = _simulate(sim, program)

        assert "does not offer the gpu operation" in str(reply["error"])


class TestGateOperation:
    def test_runs_the_planned_gate_from_the_requested_directory(
        self,
        sim: Sim,
        tmp_path: Path,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        monkeypatch.chdir(workspace)

        def program(net: _Network) -> tuple[int, _RecordingGates]:
            gates = _RecordingGates(net.threads)
            with _serving(net, tmp_path, workspace, gates=_gates(gates)) as broker:
                _point_at(monkeypatch, broker)
                return _run_client(net, ["--gate", "accuracy"]), gates

        status, gates = _simulate(sim, program)

        assert status == 3
        assert capsysbinary.readouterr().out == b"gate accuracy\n"
        assert gates.runs == [(GateKind.ACCURACY, (), workspace.resolve())]

    def test_a_workspace_result_path_passes_through_unchanged(
        self, sim: Sim, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(workspace)

        def program(net: _Network) -> _RecordingGates:
            gates = _RecordingGates(net.threads)
            with _serving(net, tmp_path, workspace, gates=_gates(gates)) as broker:
                _point_at(monkeypatch, broker)
                _run_client(
                    net, ["--gate", "benchmark", _OUTPUT_ARGUMENT, ".vibesys-benchmark-x1.json"]
                )
            return gates

        assert _simulate(sim, program).runs[0][1] == (
            _OUTPUT_ARGUMENT,
            ".vibesys-benchmark-x1.json",
        )

    def test_a_framework_result_is_written_on_the_host_and_relayed_to_the_caller(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        """The gate writes a host file of the same allowed shape; the caller gets its contents."""

        def program(net: _Network) -> tuple[list[dict[str, Any]], _RecordingGates]:
            gates = _RecordingGates(net.threads, result=b'{"ok": true}')
            with _serving(net, tmp_path, workspace, gates=_gates(gates)) as broker:
                frames = _frames(
                    net,
                    broker,
                    _gate_call(
                        broker,
                        str(workspace),
                        kind="benchmark",
                        arguments=[_OUTPUT_ARGUMENT, _FRAMEWORK_RESULT],
                    ),
                )
            return frames, gates

        frames, gates = _simulate(sim, program)

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
        self, sim: Sim, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The framework names this fixed /tmp shape; a fresh nonce keeps runs apart.
        target = Path(f"/tmp/vibesys-framework-benchmark-{uuid.uuid4().hex}.json")  # noqa: S108  # lint-waiver: LW-954374 [S108]; the framework's result path shape under test.
        monkeypatch.chdir(workspace)

        def program(net: _Network) -> int:
            gates = _RecordingGates(net.threads, result=b"relayed")
            with _serving(net, tmp_path, workspace, gates=_gates(gates)) as broker:
                _point_at(monkeypatch, broker)
                return _run_client(net, ["--gate", "benchmark", _OUTPUT_ARGUMENT, str(target)])

        try:
            status = _simulate(sim, program)

            assert status == 3
            assert target.read_bytes() == b"relayed"
            assert list(target.parent.glob(f".{target.name}*")) == []
        finally:
            target.unlink(missing_ok=True)

    @settings(max_examples=60, deadline=None)
    @given(
        seed=SEEDS,
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
    def test_gate_arguments_are_accepted_only_in_the_planned_shapes(
        self,
        seed: int | None,
        kind: GateKind,
        arguments: list[str],
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        workspace = tmp_path_factory.mktemp("workspace")
        valid_results = {".vibesys-benchmark-ok.json", _FRAMEWORK_RESULT}

        def program(net: _Network) -> tuple[list[dict[str, Any]], _RecordingGates]:
            gates = _RecordingGates(net.threads)
            with _serving(net, workspace, workspace, gates=_gates(gates)) as broker:
                call = _gate_call(broker, str(workspace), kind=kind.value, arguments=arguments)
                return _frames(net, broker, call), gates

        frames, gates = _simulate_with(seed, program)

        expected = (kind is GateKind.ACCURACY and arguments == []) or (
            kind is GateKind.BENCHMARK
            and len(arguments) == 2
            and arguments[0] == _OUTPUT_ARGUMENT
            and arguments[1] in valid_results
        )
        assert bool(gates.runs) == expected
        if expected and kind is GateKind.BENCHMARK:
            # What the broker hands the gate wrapper is itself an allowed result path:
            # the wrapper validates it again with the same function.
            assert classify_benchmark_output(gates.runs[-1][1][1]) is not None
        if not expected:
            assert "invalid arguments" in str(frames[0]["error"])

    def test_a_benchmark_without_a_declared_result_argument_takes_no_arguments(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        def program(net: _Network) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            gates = _RecordingGates(net.threads)
            with _serving(net, tmp_path, workspace, gates=_gates(gates, None)) as broker:
                refused = _frames(
                    net,
                    broker,
                    _gate_call(broker, str(workspace), kind="benchmark", arguments=["--x", "y"]),
                )
                accepted = _frames(
                    net, broker, _gate_call(broker, str(workspace), kind="benchmark")
                )
            return refused, accepted

        refused, accepted = _simulate(sim, program)

        assert "invalid arguments" in str(refused[0]["error"])
        assert accepted[-1] == {"exit": 3}

    def test_closing_the_connection_cancels_the_gate(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        def program(net: _Network) -> tuple[dict[str, Any], bool]:
            gates = _RecordingGates(net.threads, block=True)
            with _serving(net, tmp_path, workspace, gates=_gates(gates)) as broker:
                client = _send_request(net, broker, _gate_call(broker, str(workspace)))
                first = _read_frame(client)
                client.close()
                return first, gates.cancelled.wait(HANG_GUARD_S)

        first, cancelled = _simulate(sim, program)

        assert base64.b64decode(first["output"]) == b"gate accuracy\n"
        assert cancelled


def _simulate_with[T](seed: int | None, program: Callable[[_Network], T]) -> T:
    """Like :func:`_simulate`, for a property test that draws the schedule seed itself."""
    threads = SimThreads(schedule_seed=seed)
    net = _network(threads)
    return threads.run(lambda: program(net))


def _frames(
    net: _Network, broker: HostCommandBroker, request: Mapping[str, object]
) -> list[dict[str, Any]]:
    """Every frame the broker sends for *request*, until it closes the connection."""
    connection = _send_request(net, broker, request)
    frames: list[dict[str, Any]] = []
    buffer = b""
    while chunk := connection.recv(65536, HANG_GUARD_S):
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        frames.extend(json.loads(line) for line in lines)
    connection.close()
    return frames


class TestRoots:
    @settings(max_examples=80, deadline=None)
    @given(
        seed=SEEDS,
        base=st.sampled_from(["workspace", "worktrees", "other", "root"]),
        parts=st.lists(st.sampled_from(["a", "b", "..", "."]), max_size=4),
    )
    def test_a_directory_is_accepted_exactly_when_it_is_inside_the_runs_roots(
        self,
        seed: int | None,
        base: str,
        parts: list[str],
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Whatever path the container names, the broker agrees with a path model of the roots."""
        tmp_path = tmp_path_factory.mktemp("roots")
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
        named = posixpath.join(starts[base], *parts)

        def program(net: _Network) -> tuple[list[dict[str, Any]], _RecordingLauncher]:
            launcher = _RecordingLauncher(net.threads)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                return _frames(net, broker, _gpu_call(broker, named)), launcher

        reply, launcher = _simulate_with(seed, program)

        cwd = posixpath.normpath(named)
        ws, wt = str(workspace.resolve()), str(worktrees.resolve())
        inside_workspace = cwd == ws or cwd.startswith(ws + "/")
        inside_candidate = cwd.startswith(wt + "/")
        accepted = bool(launcher.commands)
        assert accepted == (inside_workspace or inside_candidate)
        if accepted:
            # The job is confined to the run's workspace or to one candidate under the root.
            confined = launcher.commands[0].argv[1]
            assert confined == ws or confined.startswith(wt + "/")
            assert reply[-1] == {"exit": 5}

    def test_a_symlink_inside_the_workspace_cannot_reach_outside_it(
        self, sim: Sim, tmp_path: Path, workspace: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "link").symlink_to(outside)

        def program(net: _Network) -> tuple[list[dict[str, Any]], _RecordingLauncher]:
            launcher = _RecordingLauncher(net.threads)
            with _serving(net, tmp_path, workspace, gpu=_gpu(launcher)) as broker:
                return _frames(net, broker, _gpu_call(broker, str(workspace / "link"))), launcher

        reply, launcher = _simulate(sim, program)

        assert "inside the run's workspace" in str(reply[0]["error"])
        assert launcher.commands == []


def _status_when_the_broker_drops(
    threads: SimThreads, net: _Network, *, argument_bytes: int, monkeypatch: pytest.MonkeyPatch
) -> int:
    """Run the client against a broker that accepts a connection and closes it unread."""
    listener = net.network.listen("dead.sock")

    def accept_and_drop() -> None:
        listener.accept(HANG_GUARD_S).close()

    server = threads.spawn(accept_and_drop, name="dead-broker")
    monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, "dead.sock")
    monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, "t")
    status = _run_client(net, ["--", "x" * argument_bytes])
    server.join(HANG_GUARD_S)
    listener.close()
    return status


class TestClient:
    def test_without_a_broker_it_refuses(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(COMMAND_BROKER_SOCKET_ENV, raising=False)
        monkeypatch.delenv(COMMAND_BROKER_TOKEN_ENV, raising=False)
        assert execute(["--", "nvidia-smi"], stop=Stop()) == 2
        assert "no command broker" in capsys.readouterr().err

    def test_an_unknown_gate_is_a_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, "/nonexistent")
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, "t")
        assert execute(["--gate", "profile"], stop=Stop()) == 2
        assert "--gate takes one of" in capsys.readouterr().err

    def test_a_dropped_connection_is_a_failure_not_a_success(
        self, sim: Sim, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threads = sim.threads()
        net = _network(threads)

        status = threads.run(
            lambda: _status_when_the_broker_drops(
                threads, net, argument_bytes=1, monkeypatch=monkeypatch
            )
        )

        assert status == 1
        assert "closed the connection" in capsys.readouterr().err

    @settings(max_examples=25, deadline=None)
    @given(seed=SEEDS, argument_bytes=st.integers(min_value=0, max_value=4 * 1024 * 1024))
    def test_a_dropped_connection_never_depends_on_how_much_was_sent_or_who_runs_first(
        self, seed: int | None, argument_bytes: int
    ) -> None:
        threads = SimThreads(schedule_seed=seed)
        net = _network(threads)
        with pytest.MonkeyPatch.context() as env:
            status = threads.run(
                lambda: _status_when_the_broker_drops(
                    threads, net, argument_bytes=argument_bytes, monkeypatch=env
                )
            )
        assert status == 1

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
