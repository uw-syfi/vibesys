"""A Slurm job script ends every process it started, and exits only once they are gone.

Regression (2026-10-15): the job script sent SIGTERM to the service's parent pid only and
then waited on it without a bound. A vLLM pipeline-parallel server waited on GPU workers
that did not exit, the script never ended, Slurm's KillWait and UnkillableStepTimeout
expired, and Slurm drained the node. The fake service here does the same: its parent waits
on a child that ignores SIGTERM. The script must still exit, and leave nothing behind.

Regression (2026-10-16): the script then sent SIGKILL to the service's process group and
exited at most 5 s later. On a B200 node two vLLM workers, each holding about 150 GB of
GPU memory, were still exiting when the script ended with status 0; Slurm's step kill gave
up after 63 s ("Kill task failed") and drained the node. A CUDA process can take tens of
seconds to exit after SIGKILL. Here a tracer outside the job holds the service's child in
its exit (``PTRACE_O_TRACEEXIT`` stops a SIGKILLed process before it releases anything) for
longer than the old 5 s: the script must wait for it, not count the zombie it leaves, and,
when the configured exit wait runs out first, name it and fail the job. A process the job's
command leaves behind is ended the same way.

Each job runs on the executing Fake Slurm cluster, which runs the exact production-generated
job script under ``/bin/bash`` inside ``sbatch``. The transport timeout is the hang guard.
The script's shape and the configuration's bounds are pinned in the deterministic tier by
``libs/vs-slurm/tests/test_api.py``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
from contextlib import suppress
from pathlib import Path

import pytest

from vs_sim.api import MonotonicClock, OsProcesses
from vs_sim.api.testing import HANG_GUARD_S
from vs_slurm.api import (
    PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobRunner,
    SlurmService,
)
from vs_slurm.fake_connector import executing_cluster

_GRACE_SECONDS = 1
# Longer than the 5 s the script waited after SIGKILL before this fix.
_HOLD_SECONDS = 7

# The parent ignores nothing: on SIGTERM it waits for its child, as a server waits for its
# workers. The child ignores SIGTERM, so only a SIGKILL ends either of them. The child lets
# any process trace it (Yama's ptrace_scope 1 otherwise allows only ancestors), and, when a
# tracer is expected, the service is ready only once the tracer answered.
_SERVICE = textwrap.dedent(
    """
    import ctypes, os, pathlib, signal, sys

    root, port = pathlib.Path(sys.argv[1]), sys.argv[2]
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ctypes.CDLL(None).prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0)
        (root / "child.pid").write_text(str(os.getpid()))
        while (root / "traced").is_file() and not (root / "tracer").is_file():
            signal.sigtimedwait([], 0.05)
        os.write(writer, b"x")
        while True:
            signal.pause()
    os.read(reader, 1)
    signal.signal(signal.SIGTERM, lambda *_: os.waitpid(child, 0))
    (root / "service.pgid").write_text(str(os.getpgrp()))
    (root / "script.pid").write_text(str(os.getppid()))
    (root / f"ready-{port}").write_text("ok")
    while True:
        signal.pause()
    """
)

# Seizes the service's child with PTRACE_O_TRACEEXIT and holds it in its exit stop, the
# state a process killed while the GPU driver frees its memory is in: SIGKILLed, not yet a
# zombie. With a hold time it then notes whether the job script is still running, lets
# the exit finish, and never reaps the zombie, so the zombie lasts as long as this tracer;
# without one it holds the exit stop until killed.
_TRACER = textwrap.dedent(
    """
    import ctypes, os, pathlib, signal, sys, time

    root, hold = pathlib.Path(sys.argv[1]), sys.argv[2]
    deadline = time.monotonic() + float(sys.argv[3])
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    PTRACE_SEIZE, PTRACE_CONT, PTRACE_O_TRACEEXIT, PTRACE_EVENT_EXIT = 0x4206, 7, 0x40, 6
    while not (root / "child.pid").is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int((root / "child.pid").read_text())
    if libc.ptrace(PTRACE_SEIZE, child, None, ctypes.c_void_p(PTRACE_O_TRACEEXIT)) != 0:
        (root / "tracer").write_text(f"unavailable: errno {ctypes.get_errno()}")
        sys.exit(0)
    (root / "tracer").write_text("attached")
    while True:
        _pid, status = os.waitpid(child, 0x40000000)  # __WALL
        if not os.WIFSTOPPED(status):
            sys.exit(0)
        if status >> 16 == PTRACE_EVENT_EXIT:
            break
        libc.ptrace(PTRACE_CONT, child, None, ctypes.c_void_p(os.WSTOPSIG(status)))
    (root / "exit-stopped").write_text("yes")
    if hold == "forever":
        signal.pause()
    time.sleep(float(hold))
    script = pathlib.Path(f"/proc/{(root / 'script.pid').read_text()}/stat")
    live = script.is_file() and script.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    (root / "released").write_text("while the job script ran" if live else "after it ended")
    libc.ptrace(PTRACE_CONT, child, None, None)
    signal.pause()
    """
)

# A command that leaves a process behind: it forks a child that ignores SIGTERM, as a
# benchmark that started a server and never stopped it, and exits 0 at once.
_LEFTOVER = textwrap.dedent(
    """
    import os, pathlib, signal, sys

    root = pathlib.Path(sys.argv[1])
    if os.fork() == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (root / "leftover.pid").write_text(str(os.getpid()))
        while True:
            signal.pause()
    while not (root / "leftover.pid").is_file():
        signal.sigtimedwait([], 0.05)
    """
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or shutil.which("setsid") is None
    or shutil.which("curl") is None,
    reason="needs Linux /proc, ptrace, setsid and curl, as a Slurm compute node has",
)


def _alive(pid: int) -> bool:
    """Whether ``pid`` exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def _group_alive(pgid: int) -> bool:
    """Whether a process of group ``pgid`` exists and is not a zombie."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with suppress(FileNotFoundError, ProcessLookupError):
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
    return False


def _unpaused(seconds: float) -> None:
    message = f"the executing cluster's job was not terminal at its first poll ({seconds} s)"
    raise AssertionError(message)


def _run_job(
    tmp_path: Path,
    *,
    command: tuple[str, ...] = ("true",),
    service: bool = True,
    exit_wait_seconds: int = int(HANG_GUARD_S),
) -> SlurmJobResult:
    service_root = tmp_path / "service"
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
        process_exit_wait_seconds=exit_wait_seconds,
        transport_timeout_seconds=int(HANG_GUARD_S) * 2,
    )
    request = SlurmJobRequest(
        workspace=workspace,
        command=command,
        service=SlurmService(
            command=(
                sys.executable,
                str(service_program),
                str(service_root),
                "VIBESYS_DYNAMIC_PORT",
            ),
            readiness_url=f"file://{service_root}/ready-VIBESYS_DYNAMIC_PORT",
            startup_timeout_seconds=int(HANG_GUARD_S),
        )
        if service
        else None,
    )
    return SlurmJobRunner(config, processes=OsProcesses(), clock=MonotonicClock()).run(
        request, pause=_unpaused
    )


def _kill_group_of(pgid_file: Path) -> None:
    if pgid_file.is_file():
        with suppress(ProcessLookupError):
            os.killpg(int(pgid_file.read_text(encoding="utf-8")), signal.SIGKILL)


def _start_tracer(tmp_path: Path, hold: str) -> subprocess.Popen[bytes]:
    root = tmp_path / "service"
    root.mkdir(exist_ok=True)
    (root / "traced").write_text("yes", encoding="utf-8")
    program = tmp_path / "tracer.py"
    program.write_text(_TRACER, encoding="utf-8")
    # Its own session keeps the tracer outside the job's processes, as the GPU driver is.
    # lint-waiver: LW-160101 [S603]; the real tier starts its own tracer program with the
    # > current interpreter and no shell; the tracer must outlive this call and run beside
    # > the job, which a run-to-completion process runner cannot do.
    return subprocess.Popen(  # noqa: S603
        (sys.executable, str(program), str(root), hold, str(HANG_GUARD_S)),
        start_new_session=True,
    )


def _require_tracer(tmp_path: Path) -> None:
    answer = (tmp_path / "service" / "tracer").read_text(encoding="utf-8")
    if answer != "attached":
        pytest.skip(f"this host does not allow ptrace ({answer})")


def test_job_script_ends_a_service_whose_workers_ignore_sigterm(tmp_path: Path) -> None:
    pgid_file = tmp_path / "service" / "service.pgid"
    (tmp_path / "service").mkdir()
    try:
        result = _run_job(tmp_path)

        assert result.exit_code == 0, result.output
        pgid = int(pgid_file.read_text(encoding="utf-8"))
        assert pgid != os.getpgrp()
        assert not _group_alive(pgid)
    finally:
        _kill_group_of(pgid_file)


def test_job_script_waits_for_a_killed_worker_to_exit_and_not_for_its_zombie(
    tmp_path: Path,
) -> None:
    tracer = _start_tracer(tmp_path, str(_HOLD_SECONDS))
    root = tmp_path / "service"
    try:
        result = _run_job(tmp_path)
        _require_tracer(tmp_path)

        assert result.exit_code == 0, result.output
        assert (root / "exit-stopped").is_file()
        # The script was still waiting when the worker's exit went on; at the merge base it
        # had ended 5 s after SIGKILL, while the tracer still held the worker.
        released = (root / "released").read_text(encoding="utf-8")
        assert released == "while the job script ran", result.output
        child = int((root / "child.pid").read_text(encoding="utf-8"))
        assert not _alive(child)
        # The tracer never reaps its zombie: the script did not wait the exit wait out on it.
        assert Path(f"/proc/{child}").exists()
        assert not _group_alive(int((root / "service.pgid").read_text(encoding="utf-8")))
    finally:
        tracer.kill()
        tracer.wait(HANG_GUARD_S)
        _kill_group_of(root / "service.pgid")


def test_job_script_names_a_process_that_outlives_the_exit_wait_and_fails_the_job(
    tmp_path: Path,
) -> None:
    tracer = _start_tracer(tmp_path, "forever")
    root = tmp_path / "service"
    try:
        result = _run_job(tmp_path, exit_wait_seconds=1)
        _require_tracer(tmp_path)

        child = int((root / "child.pid").read_text(encoding="utf-8"))
        assert result.exit_code == PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE, result.output
        assert "still alive 1 s after SIGKILL" in result.output
        rows = [line.split() for line in result.output.splitlines()]
        assert any(row[:1] == [str(child)] and row[4].startswith("t") for row in rows), (
            result.output
        )
    finally:
        tracer.kill()
        tracer.wait(HANG_GUARD_S)
        _kill_group_of(root / "service.pgid")


@pytest.mark.parametrize("service", [True, False], ids=["with-service", "without-service"])
def test_job_script_ends_a_process_its_command_left_behind(
    tmp_path: Path, *, service: bool
) -> None:
    program = tmp_path / "leftover.py"
    program.write_text(_LEFTOVER, encoding="utf-8")
    (tmp_path / "service").mkdir()
    pid_file = tmp_path / "leftover.pid"
    try:
        result = _run_job(
            tmp_path, command=(sys.executable, str(program), str(tmp_path)), service=service
        )

        assert result.exit_code == 0, result.output
        assert not _alive(int(pid_file.read_text(encoding="utf-8")))
    finally:
        if pid_file.is_file():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
        _kill_group_of(tmp_path / "service" / "service.pgid")
