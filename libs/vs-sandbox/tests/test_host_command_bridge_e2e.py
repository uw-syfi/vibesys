"""The socket bridge across a real container boundary.

A real Docker container runs the single-file client against a host broker whose
``srun`` is a fake program. This is the one thing the in-process tests cannot
show: the Unix socket is bind-mounted into a container, the workspace is
mounted at its host path, and the client runs under the image's plain
``python3`` with no VibeSys packages.

Skipped unless ``VIBESYS_E2E_DOCKER=1`` and ``docker`` is on PATH:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest libs/vs-sandbox/tests/test_host_command_bridge_e2e.py -q -s
```
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    HOST_COMMAND_CLIENT,
    GateKind,
    Gates,
    GpuCommands,
    GpuJobRequest,
    HostCommandBroker,
    RunRoots,
    SlurmGpuConfig,
    SlurmGpuLauncher,
    SrunGateRunner,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

_ENABLE_ENV = "VIBESYS_E2E_DOCKER"
_IMAGE = "python:3.12-slim"
_CLIENT_IN_CONTAINER = "/opt/vibesys/vibesys-gpu"
_RESULT = "/tmp/vibesys-framework-benchmark-{}.json"  # noqa: S108  # lint-waiver: LW-954375 [S108]; the framework's result path shape, inside the container.

# Stands in for srun and scancel: records its argv and cwd, then runs what follows
# `--`. Without a `--` it is scancel, and it reports the cancellation on a FIFO the
# test blocks on, so the test waits for the event itself rather than polling for it.
_FAKE_SLURM = """\
import os, sys
with open({log!r}, "a") as log:
    log.write(repr(sys.argv[1:]) + " cwd=" + os.getcwd() + "\\n")
if "--" in sys.argv:
    command = sys.argv[sys.argv.index("--") + 1 :]
    os.execvp(command[0], command)
if {cancelled!r}:
    with open({cancelled!r}, "w") as fifo:
        fifo.write(" ".join(sys.argv[1:]))
"""
# The planned benchmark: writes the result file named by its last argument.
_PLANNED_BENCHMARK = """\
import sys
open(sys.argv[-1], "w").write('{"throughput": 42}')
print("benchmark ran")
"""


class _NoConfinement:
    """The fake job confinement: the test's fake srun runs the command as given."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        del workspace
        return list(argv)


@contextmanager
def _broker(
    tmp_path: Path, log: Path, cancelled: Path | None = None
) -> Iterator[HostCommandBroker]:
    fake = tmp_path / "fake_slurm.py"
    fake.write_text(_FAKE_SLURM.format(log=str(log), cancelled=str(cancelled or "")))
    bench = tmp_path / "bench.py"
    bench.write_text(_PLANNED_BENCHMARK)
    config = SlurmGpuConfig.model_validate(
        {
            "partitions": ("main",),
            "max_gpus": 8,
            "max_time_minutes": 60,
            "srun_command": (sys.executable, str(fake)),
            "scancel_command": (sys.executable, str(fake)),
        }
    )
    launcher = SlurmGpuLauncher(config, windows=lambda _config: None)
    env = dict(os.environ)
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    broker = HostCommandBroker(
        tmp_path / "broker.sock",
        roots=RunRoots((workspace,)),
        gpu=GpuCommands(config, _NoConfinement(), env, launcher=launcher),
        gates=Gates(
            SrunGateRunner(
                launcher,
                GpuJobRequest(gpus=1, time_minutes=5),
                {GateKind.BENCHMARK: (sys.executable, str(bench))},
                env=env,
            ),
            benchmark_output_argument="--vs-output",
        ),
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


def _docker_run(
    broker: HostCommandBroker,
    tmp_path: Path,
    command: Sequence[str],
    *,
    name: str | None = None,
    cwd: Path | str | None = None,
) -> list[str]:
    """Build a ``docker run`` that mounts what the Slurm editor mounts."""
    workspace = tmp_path / "workspace"
    return [
        "docker",
        "run",
        "--rm",
        *(["--name", name] if name else []),
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{workspace}:{workspace}",
        "-v",
        f"{broker.socket_path}:{broker.socket_path}",
        "-v",
        f"{HOST_COMMAND_CLIENT}:{_CLIENT_IN_CONTAINER}:ro",
        "-e",
        f"{COMMAND_BROKER_SOCKET_ENV}={broker.socket_path}",
        "-e",
        f"{COMMAND_BROKER_TOKEN_ENV}={broker.token}",
        "-w",
        str(cwd or workspace / "sub"),
        _IMAGE,
        *command,
    ]


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get(_ENABLE_ENV) != "1" or shutil.which("docker") is None,
        reason=f"set {_ENABLE_ENV}=1 with docker on PATH to run a container against the broker",
    ),
]


def test_a_container_runs_a_gpu_job_from_its_working_directory(tmp_path: Path) -> None:
    log = tmp_path / "slurm.log"
    with _broker(tmp_path, log) as broker:
        result = run_test_command(
            _docker_run(
                broker,
                tmp_path,
                [
                    "python3",
                    _CLIENT_IN_CONTAINER,
                    "--gpus",
                    "2",
                    "--time",
                    "5",
                    "--",
                    "sh",
                    "-c",
                    "echo job-ran; exit 7",
                ],
            ),
            text=True,
            capture_output=True,
            timeout=300,
        )

    assert result.returncode == 7
    assert "job-ran" in result.stdout
    recorded = log.read_text()
    assert "--gres=gpu:2" in recorded
    # The job's directory is the host path of the working directory the container named.
    assert f"cwd={tmp_path / 'workspace'}" in recorded


def test_a_container_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    log = tmp_path / "slurm.log"
    with _broker(tmp_path, log) as broker:
        result = run_test_command(
            _docker_run(broker, tmp_path, ["python3", _CLIENT_IN_CONTAINER, "--", "true"], cwd="/"),
            text=True,
            capture_output=True,
            timeout=300,
        )

    assert result.returncode == 2
    assert "inside the run's workspace" in result.stderr
    assert not log.exists()


def test_a_benchmark_result_written_on_the_host_arrives_in_the_containers_tmp(
    tmp_path: Path,
) -> None:
    log = tmp_path / "slurm.log"
    result_path = _RESULT.format(uuid.uuid4().hex)
    with _broker(tmp_path, log) as broker:
        result = run_test_command(
            _docker_run(
                broker,
                tmp_path,
                [
                    "sh",
                    "-c",
                    f"python3 {_CLIENT_IN_CONTAINER} --gate benchmark --vs-output {result_path}"
                    f" && cat {result_path}",
                ],
            ),
            text=True,
            capture_output=True,
            timeout=300,
        )

    assert result.returncode == 0, result.stderr
    assert "benchmark ran" in result.stdout
    assert '{"throughput": 42}' in result.stdout


def test_stopping_the_container_client_cancels_the_job(tmp_path: Path) -> None:
    log = tmp_path / "slurm.log"
    cancelled = tmp_path / "cancelled.fifo"
    os.mkfifo(cancelled)
    name = f"vs-bridge-{uuid.uuid4().hex[:8]}"
    with _broker(tmp_path, log, cancelled) as broker:
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-954376 [S603]; test-owned docker argv.
            _docker_run(
                broker,
                tmp_path,
                ["python3", _CLIENT_IN_CONTAINER, "--", "sleep", "60"],
                name=name,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            # The launcher announces the job, by name, before it starts srun.
            header = _bounded(lambda: process.stdout.readline() if process.stdout else b"")
            job_name = header.decode().split("job-name=")[1].split()[0]
            run_test_command(["docker", "kill", "--signal=TERM", name], check=True)
            process.wait(timeout=60)
            scancel = _bounded(cancelled.read_text)
        finally:
            run_test_command(["docker", "rm", "-f", name], capture_output=True)
            process.kill()
            process.wait()

    assert f"--name={job_name}" in scancel
    assert "--me" in scancel


def _bounded[Value](blocking: Callable[[], Value], *, seconds: float = 120.0) -> Value:
    """Run a blocking read on a thread, failing the test rather than hanging it.

    The read returns the moment the event it waits for happens; the bound only
    limits how long a broken bridge can stall the test.
    """
    outcome: list[Value] = []
    thread = threading.Thread(target=lambda: outcome.append(blocking()), daemon=True)
    thread.start()
    thread.join(seconds)
    if not outcome:
        message = "the expected event never happened"
        raise AssertionError(message)
    return outcome[0]
