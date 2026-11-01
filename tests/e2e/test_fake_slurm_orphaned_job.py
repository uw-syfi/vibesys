"""A job of the executing Fake Slurm cluster outlives the process that started it.

Regression (2026-10-31): the executing Fake runs a job's script inside the call that submitted
it, so a host killed while its job ran left the job ``RUNNING`` for good: nothing ran it to its
end, and the run that resumed on that host's log polled it forever (the pause-kill-resume case
that kills a run in its implementer's turn failed whenever the kill landed during the baseline's
job, which only a slow machine does). A scheduler does not lose a job its submitter's death does
not touch, so the next poll of the job's id runs it to its end. A job whose runner is alive is
left alone.

The job script here waits for a file the test creates, so the kill lands mid-job without a clock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from vs_slurm.fake_connector import active_jobs, executing_cluster

if TYPE_CHECKING:
    from pathlib import Path

_CONNECTOR = (sys.executable, "-m", "vs_slurm.fake_connector")
_JOB = """\
#!/bin/bash
if [ ! -e go ]; then
  echo up > started
  while [ ! -e go ]; do sleep 0.05; done
fi
"""


class _Cluster:
    """An executing cluster, a job script that waits for ``go``, and the connector calls."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.state = executing_cluster(base / "cluster")
        self.fifo = base / "started"
        os.mkfifo(self.fifo)
        self.script = base / "job.sh"
        self.script.write_text(_JOB.replace("echo up > started", f"echo up > {self.fifo}"))

    def submit(self) -> subprocess.Popen[bytes]:
        """``sbatch`` the job from a connector process of its own; it returns only when the job ends."""
        command = (
            f"cd {self.base} && sbatch --job-name=orphan --output={self.base}/out.log {self.script}"
        )
        process = subprocess.Popen(  # noqa: S603
            (*_CONNECTOR, str(self.state)), stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        assert process.stdin is not None
        process.stdin.write(self._request(command))
        process.stdin.close()
        return process

    def wait_until_running(self) -> None:
        """Return once the job's script is running, which it says through the fifo."""
        descriptor = os.open(self.fifo, os.O_RDONLY)
        try:
            assert os.read(descriptor, 8) == b"up\n"
        finally:
            os.close(descriptor)

    def let_go(self) -> None:
        """Let every run of the script end."""
        (self.base / "go").touch()

    def state_of(self, job_id: str) -> str:
        """What ``sacct`` says of the job, asked through a connector process of its own."""
        reply = subprocess.run(  # noqa: S603
            (*_CONNECTOR, str(self.state)),
            input=self._request(f"sacct -n -X -j {job_id} --format=State,ExitCode"),
            capture_output=True,
            check=True,
        )
        return str(json.loads(reply.stdout)["stdout"]).split()[0]

    @staticmethod
    def _request(command: str) -> bytes:
        return json.dumps({"version": 1, "operation": "exec", "command": command}).encode()


def test_a_job_whose_runner_was_killed_is_run_to_its_end_by_the_next_poll(tmp_path: Path) -> None:
    cluster = _Cluster(tmp_path)
    submitter = cluster.submit()
    try:
        cluster.wait_until_running()
        (job_id,) = active_jobs(cluster.state)
        submitter.kill()
        submitter.wait()
        cluster.let_go()

        assert cluster.state_of(job_id) == "COMPLETED"
    finally:
        cluster.let_go()
        submitter.kill()
        submitter.wait()


def test_a_job_whose_runner_is_alive_is_left_to_it(tmp_path: Path) -> None:
    cluster = _Cluster(tmp_path)
    submitter = cluster.submit()
    try:
        cluster.wait_until_running()
        (job_id,) = active_jobs(cluster.state)

        assert cluster.state_of(job_id) == "RUNNING"

        cluster.let_go()
        submitter.wait()
        assert cluster.state_of(job_id) == "COMPLETED"
    finally:
        cluster.let_go()
        submitter.kill()
        submitter.wait()
