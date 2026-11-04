"""The run's queue status: the reader each run environment gets, and the service agents read.

A Slurm run reads its cluster's queue through the same transport its evaluations use;
a run on a service that reports no queue gets an opaque reading; a run on this machine
a local one. The host serves one cached reading to every agent over a socket in the
run's private socket directory, and the launch catalog's tool server reads it there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from launch.composition import STATELESS_TOOL_SERVERS
from vibesys.api.wiring import AgentToolContext
from vibesys.run.evaluation_socket import evaluation_socket_directory, prepare_queue_status_socket
from vibesys.run.queue_status import queue_status_reader, queue_status_service
from vs_evaluation.api.queue_status import (
    FRESH_FOR_S,
    QueueKind,
    QueueStatus,
    SocketQueueStatusReader,
)
from vs_evaluation.api.testing import FakeQueueStatusReader
from vs_mcp.api import StdioServerDescriptor
from vs_runtime.api.infrastructure import AgentPaths, ClusterEvaluationFacts, RunEnvironmentView
from vs_sandbox.api.slurm import SlurmEvaluationPlan, load_slurm_operator_settings
from vs_sim.api.testing import NO_PROCESSES, ManualClock, SimNetwork, SimThreads
from vs_slurm.api import split_framed_scheduler_query

# test-isolation: public executable transport Fake answers as the scheduler does.
from vs_slurm.fake_connector import FakeConnector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_evaluation.api.queue_status import QueueStatusUnavailable

_RECORDED = """\
2026-10-15T09:30:00
__VS_QUEUE_MINE__
__VS_QUEUE_EVERYONE__
PENDING|gpu|1200
RUNNING|gpu|1500
PENDING|cpu|900
__VS_QUEUE_NODES__
gpu-node-01|gpu|mix|gpu:h100:8|gpu:h100:6|
cpu-node-01|cpu|idle|(null)|gpu:0|
"""


@pytest.mark.parametrize(
    ("env_kind", "queue"),
    [
        ("host", QueueKind.LOCAL),
        ("docker", QueueKind.LOCAL),
        ("modal", QueueKind.OPAQUE),
        ("skypilot", QueueKind.OPAQUE),
    ],
)
def test_a_run_without_a_cluster_gets_its_environments_reader(
    tmp_path: Path, env_kind: str, queue: QueueKind
) -> None:
    reader = queue_status_reader(
        RunEnvironmentView(paths=AgentPaths(), workspace_git_writable=True, env_kind=env_kind),
        run_id="run-1",
        handle_root=tmp_path,
        processes=NO_PROCESSES,
        clock=ManualClock(),
    )

    reading = reader.read()

    assert isinstance(reading, QueueStatus)
    assert reading.queue is queue
    assert reading.items is None


def test_a_slurm_run_reads_its_clusters_queue_in_its_partition(tmp_path: Path) -> None:
    path = tmp_path / "slurm.toml"
    path.write_text(
        '[slurm]\nname = "cluster"\nremote_workspace_root = "/shared/vibesys"\n'
        'sbatch_arguments = ["--partition=gpu"]\n'
        '[slurm.transport]\nkind = "connector"\ncommand = ["connector"]\n[vibesys]\n',
        encoding="utf-8",
    )
    settings = load_slurm_operator_settings(path)
    connector = FakeConnector(tmp_path / "connector")

    def answer(
        request: dict[str, object], forward: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        query, end = split_framed_scheduler_query(str(request.get("command", "")))
        if "__VS_QUEUE_MINE__" not in query:
            return forward(request)
        return {"version": 1, "returncode": 0, "stdout": _RECORDED + end, "stderr": ""}

    connector.intercept(answer)
    view = RunEnvironmentView(
        paths=AgentPaths(),
        workspace_git_writable=True,
        env_kind="slurm",
        cluster_evaluation=ClusterEvaluationFacts(settings, SlurmEvaluationPlan(config_path=path)),
    )

    reading = queue_status_reader(
        view,
        run_id="run-1",
        handle_root=tmp_path / "handles",
        processes=connector,
        clock=ManualClock(),
    ).read()

    assert isinstance(reading, QueueStatus)
    assert reading.queue is QueueKind.SHARED
    assert reading.items == ()
    contention = reading.contention
    assert (contention.jobs_waiting, contention.jobs_running) == (1, 1)
    assert (contention.gpus_total, contention.gpus_busy, contention.gpus_free) == (8, 6, 2)


def test_the_service_answers_every_agent_from_one_reading_while_fresh(tmp_path: Path) -> None:
    socket = tmp_path / "queue-status.sock"
    threads = SimThreads(schedule_seed=0)
    network = SimNetwork(threads)
    clock = ManualClock()
    backend = FakeQueueStatusReader([QueueStatus(queue=QueueKind.LOCAL, items=None)])
    service = queue_status_service(backend, socket, network=network, threads=threads, clock=clock)
    reader = SocketQueueStatusReader(socket, network=network, clock=threads)

    def main() -> list[QueueStatus | QueueStatusUnavailable]:
        service.start()
        try:
            readings = [reader.read(), reader.read()]
            clock.advance(FRESH_FOR_S)
            readings.append(reader.read())
            return readings
        finally:
            service.close()

    readings = threads.run(main)

    assert all(isinstance(reading, QueueStatus) for reading in readings)
    assert backend.reads == 2  # once for the first two calls, again once stale


def test_the_tool_server_reads_the_socket_the_host_names() -> None:
    socket = "/run/vse-x/queue-status.sock"

    descriptor = STATELESS_TOOL_SERVERS["queue_status"](
        AgentToolContext("none", queue_status_socket=socket)
    )

    assert isinstance(descriptor, StdioServerDescriptor)
    assert dict(descriptor.runtime_env) == {"VS_QUEUE_STATUS_SOCKET": socket}


def test_the_tool_server_declines_when_the_host_serves_no_queue_status() -> None:
    assert STATELESS_TOOL_SERVERS["queue_status"](AgentToolContext("none")) is None


def test_the_queue_socket_shares_the_evaluation_sockets_private_directory(tmp_path: Path) -> None:
    socket = prepare_queue_status_socket(tmp_path, "run-1")

    assert socket.parent == evaluation_socket_directory(tmp_path, "run-1")
    assert socket.name == "queue-status.sock"
