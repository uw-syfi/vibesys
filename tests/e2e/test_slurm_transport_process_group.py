"""Real POSIX contract for direct Slurm transport timeout ownership."""

from __future__ import annotations

import os
import select
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import MonotonicClock, OsProcesses, OsThreads
from vs_slurm.api import (
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmError,
    SlurmJobRequest,
    SlurmJobRunner,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"),
    reason="the descendant identity check requires Linux pidfds",
)
@pytest.mark.timeout(20)
def test_transport_timeout_kills_descendant_that_inherits_captured_streams(
    tmp_path: Path,
) -> None:
    """The public runner must stop a wrapper's whole locally owned process group."""
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    child_pid = tmp_path / "child.pid"
    survived = tmp_path / "descendant-survived"
    wrapper = textwrap.dedent(
        f"""
        import os
        import pathlib
        import signal

        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(write_fd)
            os.read(read_fd, 1)
            pathlib.Path({str(survived)!r}).write_text("survived", encoding="utf-8")
            os._exit(0)
        os.close(read_fd)
        pathlib.Path({str(child_pid)!r}).write_text(str(child), encoding="utf-8")
        print("ready", flush=True)
        signal.pause()
        """
    )
    config = SlurmConfig(
        name="timeout-contract",
        remote_workspace_root=str(tmp_path / "remote"),
        transport=SlurmConnectorTransport(
            kind="connector",
            command=(sys.executable, "-c", wrapper),
        ),
        transport_timeout_seconds=2,
    )

    with pytest.raises(SlurmError, match="transport timed out during exec"):
        SlurmJobRunner(config, processes=OsProcesses(), clock=MonotonicClock()).run(
            SlurmJobRequest(workspace=workspace, command=("true",)), pause=OsThreads().sleep
        )

    pid = int(child_pid.read_text(encoding="utf-8"))
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        descriptor = -1
    if descriptor >= 0:
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            assert poller.poll(5_000), "timed-out transport descendant did not exit"
        finally:
            os.close(descriptor)
    assert not survived.exists()
