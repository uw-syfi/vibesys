"""A Slurm job script ends its service's whole process group, within a bound, under real bash.

Regression (2026-10-15): the job script sent SIGTERM to the service's parent pid only and
then waited on it without a bound. A vLLM pipeline-parallel server waited on GPU workers
that did not exit, the script never ended, Slurm's KillWait and UnkillableStepTimeout
expired, and Slurm drained the node. The fake service here does the same: its parent waits
on a child that ignores SIGTERM. The script must still exit, and leave nothing in the
service's process group.

The job runs on the executing Fake Slurm cluster, which runs the exact production-generated
job script under ``/bin/bash`` inside ``sbatch``. The transport timeout is the hang guard:
at the merge base the script never exits, so ``sbatch`` times out and the run fails.
The script's content and the ``sbatch`` flags are pinned in the deterministic tier by
``libs/vs-slurm/tests/test_api.py``.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import textwrap
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import MonotonicClock, OsProcesses
from vs_sim.api.testing import HANG_GUARD_S
from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmService,
)
from vs_slurm.fake_connector import executing_cluster

if TYPE_CHECKING:
    from pathlib import Path

_GRACE_SECONDS = 1

# The parent ignores nothing: on SIGTERM it waits for its child, as a server waits for its
# workers. The child ignores SIGTERM, so only a SIGKILL of the group ends either of them.
_SERVICE = textwrap.dedent(
    """
    import os, pathlib, signal, sys

    root, port = pathlib.Path(sys.argv[1]), sys.argv[2]
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(writer, b"x")
        while True:
            signal.pause()
    os.read(reader, 1)
    signal.signal(signal.SIGTERM, lambda *_: os.waitpid(child, 0))
    (root / "service.pgid").write_text(str(os.getpgrp()))
    (root / "child.pid").write_text(str(child))
    (root / f"ready-{port}").write_text("ok")
    while True:
        signal.pause()
    """
)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _unpaused(seconds: float) -> None:
    message = f"the executing cluster's job was not terminal at its first poll ({seconds} s)"
    raise AssertionError(message)


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("setsid") is None or shutil.which("curl") is None,
    reason="needs POSIX process groups, setsid and curl, as a Slurm compute node has",
)
def test_job_script_ends_a_service_whose_workers_ignore_sigterm(tmp_path: Path) -> None:
    service_root = tmp_path / "service"
    service_root.mkdir()
    service_program = tmp_path / "service.py"
    service_program.write_text(_SERVICE, encoding="utf-8")
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    cluster = executing_cluster(tmp_path / "cluster")
    config = SlurmConfig(
        name="test-cluster",
        remote_workspace_root=remote.as_posix(),
        transport=SlurmConnectorTransport(
            kind="connector",
            command=(sys.executable, "-m", "vs_slurm.fake_connector", str(cluster)),
        ),
        service_stop_grace_seconds=_GRACE_SECONDS,
        transport_timeout_seconds=int(HANG_GUARD_S),
    )
    service = SlurmService(
        command=(sys.executable, str(service_program), str(service_root), "VIBESYS_DYNAMIC_PORT"),
        readiness_url=f"file://{service_root}/ready-VIBESYS_DYNAMIC_PORT",
        startup_timeout_seconds=int(HANG_GUARD_S),
    )
    pgid_file = service_root / "service.pgid"
    try:
        result = SlurmJobRunner(config, processes=OsProcesses(), clock=MonotonicClock()).run(
            SlurmJobRequest(workspace=workspace, command=("true",), service=service),
            pause=_unpaused,
        )

        assert result.exit_code == 0, result.output
        pgid = int(pgid_file.read_text(encoding="utf-8"))
        assert pgid != os.getpgrp()
        # The script exits only once the group is empty (or its reap bound ran out).
        assert not _group_alive(pgid)
    finally:
        if pgid_file.is_file():
            with suppress(ProcessLookupError):
                os.killpg(int(pgid_file.read_text(encoding="utf-8")), signal.SIGKILL)
