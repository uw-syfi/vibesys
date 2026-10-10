"""The SkyPilot bridge serves its evaluator protocol over the simulated network.

Every handler is a simulated thread and the SkyPilot runner is a recording Fake, so
the lifecycle and the framing run in one process without sockets or sleeps. The
real Unix socket path is exercised by ``tests/vibesys/skypilot/test_bridge.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_project.api import StateNamespace
from vs_sandbox.api.skypilot import (
    AckRequest,
    ClusterInfo,
    ClusterStatus,
    ErrorFrame,
    EvaluationRequest,
    JobResult,
    JobStatus,
    ResolvedSkyPilotResources,
    SkyPilotBridge,
    SkyPilotJobRunner,
    decode_response,
    encode_message,
)
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vs_sandbox.api.skypilot import RemoteJobInfo
    from vs_sim.api import Connection, Listener

_ADDRESS = "bridge.sock"


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


class _Runner(SkyPilotJobRunner):
    def __init__(self) -> None:
        self.ensure_calls = 0
        self.released: list[str] = []
        self.commands: list[tuple[str, ...]] = []

    def ensure_cluster(
        self, name: str, resources: ResolvedSkyPilotResources, *, timeout: float | None = 300
    ) -> ClusterInfo:
        del resources, timeout
        self.ensure_calls += 1
        return ClusterInfo(name, ClusterStatus.UP)

    def inspect_cluster(self, name: str, *, timeout: float = 60) -> ClusterInfo | None:
        del timeout
        return ClusterInfo(name, ClusterStatus.UP)

    def run(  # noqa: PLR0913  # lint-waiver: LW-731021 [PLR0913]; this Fake keeps the public SkyPilotJobRunner.run signature.
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
        del resources, workdir, timeout, job_name, existing_job_id, log_tail
        assert stdout_sink is not None
        assert stderr_sink is not None
        assert job_started is not None
        self.commands.append(tuple(command))
        job_started(9)
        stdout_sink("out\n")
        stderr_sink("err\n")
        return JobResult(JobStatus.COMPLETED, 0, 9, "out\n", "err\n", cluster_name)

    def query_job(
        self, cluster_name: str, *, job_name: str, job_id: int | None = None, timeout: float = 60
    ) -> RemoteJobInfo | None:
        del cluster_name, job_name, job_id, timeout
        return None

    def cancel(self, cluster_name: str, job_id: int, *, timeout: float = 60) -> None:
        del cluster_name, job_id, timeout

    def release(self, cluster_name: str, *, timeout: float = 60) -> None:
        del timeout
        self.released.append(cluster_name)


class _Setup:
    def __init__(self, tmp_path: Path, network: SimNetwork, threads: SimThreads) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "candidate.py").write_text("candidate")
        state = tmp_path / ".vibesys" / "state" / "skypilot"
        state.mkdir(parents=True)
        self.network = network
        self.runner = _Runner()
        self.bridge = SkyPilotBridge(
            runner=self.runner,
            cluster_name="lease",
            resources=_resources(),
            workspace=workspace,
            evaluator_package_root=None,
            hidden_paths=(),
            commands={"accuracy": ("true",)},
            benchmark_output_argument=None,
            state_namespace=StateNamespace(project_root=tmp_path, root=state, portable=False),
            socket_path=tmp_path / _ADDRESS,
            log=lambda _: None,
            network=network,
            threads=threads,
        )

    def dial(self) -> Connection:
        return self.network.connect(str(self.bridge.socket_path), HANG_GUARD_S)


def _read_line(connection: Connection) -> bytes:
    line = b""
    while not line.endswith(b"\n"):
        chunk = connection.recv(1, HANG_GUARD_S)
        if not chunk:
            break
        line += chunk
    return line


def _run(tmp_path: Path, scenario: Callable[[_Setup], None]) -> None:
    threads = SimThreads()
    setup = _Setup(tmp_path, SimNetwork(threads), threads)
    threads.run(lambda: scenario(setup))


def test_an_evaluation_streams_output_then_the_result_and_is_acknowledged(tmp_path: Path) -> None:
    types: list[str] = []

    def scenario(setup: _Setup) -> None:
        setup.bridge.start()
        try:
            client = setup.dial()
            request = EvaluationRequest(kind="accuracy", invocation_id="a" * 32)
            client.send(encode_message(request))
            while True:
                frame = decode_response(_read_line(client))
                types.append(frame.type)
                if frame.type in {"result", "error"}:
                    break
            client.send(encode_message(AckRequest(invocation_id=request.invocation_id)))
            types.append(decode_response(_read_line(client)).type)
            client.close()
        finally:
            setup.bridge.close()
        assert setup.runner.released == ["lease"]

    _run(tmp_path, scenario)

    assert types[-2:] == ["result", "acked"]
    assert {"stdout", "stderr"} <= set(types)


@pytest.mark.parametrize(
    "payload",
    [b"not json\n", b'{"kind":"accuracy"}\n', b"no newline", b""],
    ids=["garbage", "missing-fields", "unterminated", "empty"],
)
def test_a_malformed_request_gets_a_typed_error_frame(tmp_path: Path, payload: bytes) -> None:
    replies: list[ErrorFrame] = []

    def scenario(setup: _Setup) -> None:
        setup.bridge.start()
        try:
            client = setup.dial()
            client.send(payload)
            if not payload.endswith(b"\n"):
                client.close()
                return
            reply = decode_response(_read_line(client))
            assert isinstance(reply, ErrorFrame)
            replies.append(reply)
        finally:
            setup.bridge.close()

    _run(tmp_path, scenario)

    if payload.endswith(b"\n"):
        assert [reply.type for reply in replies] == ["error"]


def test_an_unconfigured_evaluator_is_refused_without_running_anything(tmp_path: Path) -> None:
    def scenario(setup: _Setup) -> None:
        setup.bridge.start()
        try:
            client = setup.dial()
            client.send(encode_message(EvaluationRequest(kind="benchmark", invocation_id="b" * 32)))
            reply = decode_response(_read_line(client))
            assert reply == ErrorFrame(error="ValueError")
        finally:
            setup.bridge.close()
        assert setup.runner.commands == []

    _run(tmp_path, scenario)


def test_closing_stops_listening_and_releases_the_allocation_once(tmp_path: Path) -> None:
    def scenario(setup: _Setup) -> None:
        setup.bridge.start()
        setup.bridge.close()
        setup.bridge.close()
        with pytest.raises(ConnectionRefusedError):
            setup.dial()
        assert setup.runner.released == ["lease"]

    _run(tmp_path, scenario)


def test_a_failed_bind_releases_the_allocation_and_reports_the_error(tmp_path: Path) -> None:
    held: list[Listener] = []

    def scenario(setup: _Setup) -> None:
        held.append(setup.network.listen(str(setup.bridge.socket_path)))
        with pytest.raises(OSError, match="in use"):
            setup.bridge.start()
        assert setup.runner.released == ["lease"]

    _run(tmp_path, scenario)
