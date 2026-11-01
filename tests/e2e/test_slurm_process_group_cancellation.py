"""The Slurm GPU launcher owns and signals a real POSIX process group."""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import AgentGpuConfig, AgentGpuLauncher, GpuCommand, GpuJobRequest
from vs_sim.api import OsThreads, ProbeResult, SubprocessPipedProcessLauncher
from vs_sim.api.testing import ScriptedProbe

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(os.name != "posix", reason="process groups require a POSIX host")
@pytest.mark.timeout(45)
def test_agent_gpu_real_process_group_terminates_pipe_holding_descendants(
    tmp_path: Path,
) -> None:
    """The legacy Popen seam still creates a group whose descendants receive SIGTERM."""
    group_file = tmp_path / "process-group"
    wrapper = f"""
import os
import pathlib
import signal

pathlib.Path({str(group_file)!r}).write_text(str(os.getpgrp()), encoding="utf-8")
child = os.fork()
if child == 0:
    print("descendant ready", flush=True)
    signal.pause()
    raise SystemExit(1)
print(f"leader={{os.getpid()}} group={{os.getpgrp()}}", flush=True)
signal.pause()
"""
    # The scheduler reports the partition's limit before submission; every
    # other probe (the cancel's scancel and queue check) succeeds with no output.
    probe = ScriptedProbe(
        lambda argv: ProbeResult(0, "gpu* 2:00:00\n" if argv[-1].split()[0] == "sinfo" else "")
    )
    launcher = AgentGpuLauncher(
        AgentGpuConfig(
            partitions=("gpu",),
            max_gpus=1,
            max_time_minutes=5,
            default_time_minutes=5,
            # The queue answers at once; the default 5 s between the six checks is 25 s of waiting.
            queue_check_interval_seconds=0.05,
        ),
        (sys.executable, "-c", wrapper),
        probe=probe,
        processes=SubprocessPipedProcessLauncher(),
        threads=OsThreads(),
    )
    cancel = threading.Event()
    output = bytearray()
    completed = False

    def capture(chunk: bytes) -> None:
        output.extend(chunk)
        if b"descendant ready\n" in output and b"leader=" in output:
            cancel.set()

    try:
        outcome = launcher.run(
            GpuJobRequest(gpus=1, time_minutes=5),
            GpuCommand(argv=("work",), cwd=tmp_path, env=dict(os.environ)),
            write=capture,
            cancel=cancel,
        )
        completed = True
    finally:
        if not completed and group_file.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(group_file.read_text(encoding="utf-8")), signal.SIGKILL)

    leader_and_group = re.search(rb"leader=(\d+) group=(\d+)", output)
    assert leader_and_group is not None
    assert leader_and_group[1] == leader_and_group[2]
    assert outcome.exit_code == -signal.SIGTERM
    assert outcome.termination_confirmed
    programs = [argv[-1].split()[0] for argv, _timeout in probe.calls]
    # The partition-limit query before submission, then cancellation and the
    # launcher's bounded continuous-absence proof.
    assert programs == ["sinfo", "scancel", *("squeue",) * 6]
