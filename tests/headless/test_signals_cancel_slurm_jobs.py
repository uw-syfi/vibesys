"""A headless run ended by a process signal cancels its in-flight Slurm job.

r14: a run's job stayed RUNNING after its process ended. The headless engine
died on SIGTERM or SIGHUP without unwinding, and a job is cancelled only by
the teardown of the run that submitted it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from vs_slurm.fake_connector import JOB_ID, SUBMITTED_FILE, recorded_commands

_REPOSITORY = Path(__file__).resolve().parents[2]


def _write_input(base: Path) -> tuple[Path, Path, Path]:
    cluster = base / "cluster"
    cluster.mkdir()
    os.mkfifo(cluster / SUBMITTED_FILE)
    connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(cluster)])
    config = base / "slurm.toml"
    # A one-hour poll interval: the job leaves the queue only through scancel.
    config.write_text(
        "[slurm]\n"
        'name = "fake"\n'
        'remote_workspace_root = "/remote/runs"\n'
        "poll_interval_seconds = 3600.0\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )
    project = base / "project"
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Improve the queue.\n", encoding="utf-8")
    (project / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n',
        encoding="utf-8",
    )
    return cluster, config, project


@pytest.mark.parametrize("closed_stdout", [False, True], ids=["stdout", "closed-stdout"])
@pytest.mark.parametrize(
    "signals",
    [
        (signal.SIGTERM,),
        (signal.SIGHUP,),
        (signal.SIGINT, signal.SIGTERM),
    ],
    ids=["sigterm", "sighup", "sigint-then-sigterm"],
)
def test_a_signal_that_ends_the_run_cancels_its_slurm_job(
    tmp_path: Path, signals: tuple[signal.Signals, ...], *, closed_stdout: bool
) -> None:
    cluster, config, project = _write_input(tmp_path)
    reader, writer = os.pipe()
    # lint-waiver: LW-731104 [S603]; the run must be its own process to be signalled.
    # > Signalling an in-process run would signal pytest; the argv is fixed.
    run = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "tests.headless._signalled_run", str(project), str(config)],
        cwd=_REPOSITORY,
        stdout=writer,
        stderr=subprocess.DEVNULL,
        # Its own process group, as a terminal's foreground job has.
        start_new_session=True,
    )
    os.close(writer)
    try:
        # Blocks until the run's job is queued on the Fake cluster.
        assert (cluster / SUBMITTED_FILE).read_text(encoding="utf-8") == JOB_ID
        if closed_stdout:
            # The terminal's pipe reader (`tee`) died with the same signal.
            os.close(reader)
        for number in signals:
            os.killpg(run.pid, number)
        returncode = run.wait()
    finally:
        if not closed_stdout:
            os.close(reader)

    assert f"scancel {JOB_ID}" in recorded_commands(cluster)
    assert returncode != 0
