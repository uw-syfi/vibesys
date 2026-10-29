"""A headless run ended by a process signal cancels its in-flight Slurm job.

r14: a run's job stayed RUNNING after its process ended. The headless engine
died on SIGTERM or SIGHUP without unwinding, and a job is cancelled only by
the teardown of the run that submitted it.

The run is the product's own (`tests.e2e._signalled_run`) over a Fake cluster reached through
its connector process. Every signal sequence and transport runs it in a forked child of the test
(its own session, as a terminal's foreground job has), so the handlers and the teardown are the
real ones and only the interpreter start-up and the product import are not repeated. One case
starts the run as a real process, to prove the entry point's wiring.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.e2e._signalled_run import main as signalled_run
from tests.support.task_project import write_task

from vs_sim.api.testing import HANG_GUARD_S, Depth, depth
from vs_slurm.fake_connector import JOB_ID, POLLED_FILE, SUBMITTED_FILE, recorded_commands

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess

pytestmark = pytest.mark.usefixtures("isolated_github_auth")

_REPOSITORY = Path(__file__).resolve().parents[2]


def _write_input(base: Path, transport: str) -> tuple[Path, Path, Path]:
    cluster = base / "cluster"
    cluster.mkdir()
    os.mkfifo(cluster / SUBMITTED_FILE)
    os.mkfifo(cluster / POLLED_FILE)
    program = [sys.executable, "-m", "vs_slurm.fake_connector", str(cluster)]
    if transport == "connector":
        table = f'{{ kind = "connector", command = {json.dumps(program)} }}'
    else:
        # Production's transport: the gate reaches the cluster through the
        # run's host-side broker, which the signalled process owns.
        table = (
            f'{{ kind = "ssh", host = "fake", ssh_command = {json.dumps([*program, "ssh"])}, '
            f"rsync_command = {json.dumps([*program, 'rsync'])} }}"
        )
    config = base / "slurm.toml"
    # A one-hour poll interval: the job leaves the queue only through scancel.
    config.write_text(
        "[slurm]\n"
        'name = "fake"\n'
        'remote_workspace_root = "/remote/runs"\n'
        "poll_interval_seconds = 3600.0\n"
        f"transport = {table}\n",
        encoding="utf-8",
    )
    project = base / "project"
    project.mkdir()
    (project / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_task(
        project,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["run-benchmark"]\nresult_protocol = 2\n',
        objective="Improve the queue.\n",
    )
    return cluster, config, project


def _forked_run(project: Path, config: Path, stdout: int) -> BaseProcess:
    """Start the run in a forked child: a session of its own, its output on ``stdout``."""

    def child() -> None:
        os.setsid()
        os.dup2(stdout, 1)
        with Path(os.devnull).open("wb") as sink:
            os.dup2(sink.fileno(), 2)
        signalled_run(project, config)

    process = multiprocessing.get_context("fork").Process(target=child)
    with warnings.catch_warnings():
        # The child runs one function and exits, so it never needs a lock a thread held.
        warnings.simplefilter("ignore", DeprecationWarning)
        process.start()
    return process


class _RealRun:
    """The run as a real process of its own, with the same surface as a forked child."""

    def __init__(self, project: Path, config: Path, stdout: int) -> None:
        # lint-waiver: LW-731104 [S603]; the run must be its own process to be signalled.
        # > Signalling an in-process run would signal pytest; the argv is fixed.
        self._process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "tests.e2e._signalled_run", str(project), str(config)],
            cwd=_REPOSITORY,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            # Its own process group, as a terminal's foreground job has.
            start_new_session=True,
        )
        self.pid = self._process.pid

    def finish(self) -> int:
        """Wait for the process to end; its exit status."""
        # test-isolation: the bound only guards against a hung child; a run that ends in time never reads it.
        return self._process.wait(timeout=HANG_GUARD_S)


class _ForkedRun:
    def __init__(self, project: Path, config: Path, stdout: int) -> None:
        self._process = _forked_run(project, config, stdout)
        assert self._process.pid is not None
        self.pid = self._process.pid

    def finish(self) -> int:
        """Wait for the child to end; its exit status (negative: killed by that signal)."""
        self._process.join(HANG_GUARD_S)
        if self._process.is_alive():
            self._process.kill()
            pytest.fail("the signalled run did not end")
        assert self._process.exitcode is not None
        return self._process.exitcode


# The three axes (signal sequence, transport, whether the terminal's pipe reader died too) are
# covered pairwise rather than crossed: every pair of values from two axes appears in some case.
_CASES = [
    pytest.param((signal.SIGTERM,), "connector", False, id="sigterm-stdout-connector"),
    pytest.param((signal.SIGTERM,), "ssh", True, id="sigterm-closed-stdout-ssh"),
    pytest.param((signal.SIGHUP,), "connector", True, id="sighup-closed-stdout-connector"),
    pytest.param((signal.SIGHUP,), "ssh", False, id="sighup-stdout-ssh"),
    pytest.param(
        (signal.SIGINT, signal.SIGTERM),
        "connector",
        True,
        id="sigint-then-sigterm-closed-stdout-connector",
    ),
    pytest.param(
        (signal.SIGINT, signal.SIGTERM), "ssh", False, id="sigint-then-sigterm-stdout-ssh"
    ),
    # Ctrl-C alone ends the run the way SIGTERM does: its job is cancelled and it exits.
    pytest.param((signal.SIGINT,), "connector", False, id="sigint-stdout-connector"),
    pytest.param((signal.SIGINT,), "ssh", True, id="sigint-closed-stdout-ssh"),
]


def _signal_the_run(
    base: Path,
    start: type[_ForkedRun] | type[_RealRun],
    signals: tuple[signal.Signals, ...],
    transport: str,
    *,
    closed_stdout: bool,
) -> None:
    cluster, config, project = _write_input(base, transport)
    reader, writer = os.pipe()
    run = start(project, config, writer)
    os.close(writer)
    try:
        # Blocks until the run's job is queued on the Fake cluster, then until the run
        # has recorded the job and asks the scheduler about it. Signalling at the
        # submission announcement instead lands anywhere between the `sbatch` reply and
        # the recording of the job, so each run would exercise a different interleaving.
        assert (cluster / SUBMITTED_FILE).read_text(encoding="utf-8") == JOB_ID
        assert (cluster / POLLED_FILE).read_text(encoding="utf-8") == JOB_ID
        if closed_stdout:
            # The terminal's pipe reader (`tee`) died with the same signal.
            os.close(reader)
        for number in signals:
            os.killpg(run.pid, number)
        returncode = run.finish()
    finally:
        if not closed_stdout:
            os.close(reader)

    assert f"scancel {JOB_ID}" in recorded_commands(cluster)
    if set(signals) != {signal.SIGINT}:
        assert returncode != 0
    # Ctrl-C alone stops the run, which returns its stopped result: the stand-in exits
    # 0, as it reports no result (the CLI exits 1 for a run that stopped early).


# A forked run costs about 3 CPU-seconds (the Fake cluster's connector starts once per request).
# The real process below takes SIGTERM, so a pull request signals the forked run with each of the
# other three sequences, over the two transports and both states of the terminal's pipe; the
# nightly job runs all eight.
_PULL_REQUEST = {
    "sighup-closed-stdout-connector",
    "sigint-then-sigterm-stdout-ssh",
    "sigint-stdout-connector",
}


@pytest.mark.parametrize(
    ("signals", "transport", "closed_stdout"),
    [case for case in _CASES if depth() is Depth.NIGHTLY or case.id in _PULL_REQUEST],
)
def test_a_signal_that_ends_the_run_cancels_its_slurm_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signals: tuple[signal.Signals, ...],
    transport: str,
    *,
    closed_stdout: bool,
) -> None:
    # The run's agent container needs the credential its CLI would start with.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")
    _signal_the_run(tmp_path, _ForkedRun, signals, transport, closed_stdout=closed_stdout)


def test_the_real_headless_process_cancels_its_slurm_job_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one real process: the entry point's own wiring, signalled from outside."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic-key")
    _signal_the_run(tmp_path, _RealRun, (signal.SIGTERM,), "connector", closed_stdout=False)
