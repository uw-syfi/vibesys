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

Review of that fix (2026-10-16): the script counted a multithreaded process whose main
thread had exited (``ps`` state ``Zl``) as gone while a sibling thread was still in the
kernel, and a CUDA process releases the GPU only when its last thread exits. It also never
signalled a descendant that called ``setsid`` (as Ray and daemons do), since it found
processes by the sessions it started. Here a tracer holds a worker's sibling thread in its
exit after the main thread exited, and a command leaves behind a process in a session of
its own, with a child whose environment is empty.

Second review (2026-10-16): a nested job submitted by the command inherited the marker and
was killed, and a session found after the first scan got only SIGKILL. A third finding, an
environment read that blocks on a stuck process's mmap_lock, has no real-tier test: holding
another process's mmap_lock, or making a read of its environment block, needs privileges
(``userfaultfd`` for kernel faults, FUSE, or a stuck driver) that a test host does not
reliably grant, so ``libs/vs-slurm/tests/test_api.py`` pins that every such read is bounded.

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
    SERVICE_NOT_READY_EXIT_CODE,
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
# tracer is expected, the service is ready only once the tracer answered. With a ``fail``
# file, the parent exits once its child is ready instead, and the service never is.
_SERVICE = textwrap.dedent(
    """
    import ctypes, os, pathlib, signal, sys

    root, port = pathlib.Path(sys.argv[1]), sys.argv[2]
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ctypes.CDLL(None).prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0)
        child_pid = root / "child.pid"
        pending_child_pid = root / ".child.pid.pending"
        pending_child_pid.write_text(str(os.getpid()))
        pending_child_pid.replace(child_pid)
        while (root / "traced").is_file() and not (root / "tracer").is_file():
            signal.sigtimedwait([], 0.05)
        os.write(writer, b"x")
        while True:
            signal.pause()
    os.read(reader, 1)
    if (root / "fail").is_file():
        sys.exit(1)
    signal.signal(signal.SIGTERM, lambda *_: os.waitpid(child, 0))
    (root / "service.pgid").write_text(str(os.getpgrp()))
    (root / "script.pid").write_text(str(os.getppid()))
    (root / f"ready-{port}").write_text("ok")
    while True:
        signal.pause()
    """
)

# Seizes the service's child (or the thread named in the file ``argv[4]``) with PTRACE_O_TRACEEXIT and holds it in its exit stop, the
# state a process killed while the GPU driver frees its memory is in: SIGKILLed, not yet a
# zombie. With a hold time it then notes whether the job script is still running, lets
# the exit finish, and never reaps the zombie, so the zombie lasts as long as this tracer;
# without one it holds the exit stop until killed.
_TRACER = textwrap.dedent(
    """
    import ctypes, os, pathlib, signal, sys, time

    root, hold, target = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[4]
    deadline = time.monotonic() + float(sys.argv[3])
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
    PTRACE_SEIZE, PTRACE_CONT, PTRACE_O_TRACEEXIT, PTRACE_EVENT_EXIT = 0x4206, 7, 0x40, 6
    while not (root / target).is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int((root / target).read_text())
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
        leftover_pid = root / "leftover.pid"
        pending_leftover_pid = root / ".leftover.pid.pending"
        pending_leftover_pid.write_text(str(os.getpid()))
        pending_leftover_pid.replace(leftover_pid)
        while True:
            signal.pause()
    while not (root / "leftover.pid").is_file():
        signal.sigtimedwait([], 0.05)
    """
)

# A command that leaves a multithreaded worker behind: the worker's main thread exits (by
# the raw exit system call, which ends only the calling thread), while a sibling thread,
# held by the tracer, lives on, so the worker shows as a zombie with two threads. The
# command exits 0 once the main thread is gone.
_THREADED = textwrap.dedent(
    """
    import ctypes, os, pathlib, signal, sys, threading

    root = pathlib.Path(sys.argv[1])
    libc = ctypes.CDLL(None)
    sys_exit = {"x86_64": 60, "aarch64": 93}[os.uname().machine]
    worker = os.fork()
    if worker == 0:
        libc.prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0)
        def hold():
            thread_tid = root / "thread.tid"
            pending_thread_tid = root / ".thread.tid.pending"
            pending_thread_tid.write_text(str(threading.get_native_id()))
            pending_thread_tid.replace(thread_tid)
            threading.Event().wait()
        threading.Thread(target=hold).start()
        while not (root / "tracer").is_file():
            signal.sigtimedwait([], 0.05)
        libc.syscall(sys_exit, 0)
    stat = pathlib.Path(f"/proc/{worker}/stat")
    while stat.read_text().rsplit(")", 1)[1].split()[0] != "Z":
        signal.sigtimedwait([], 0.05)
    (root / "worker.pid").write_text(str(worker))
    """
)

# A command that leaves a process in a session of its own (``setsid``, as Ray and daemons
# do) that ignores SIGTERM, and that process starts a child with an empty environment.
_ESCAPEE = textwrap.dedent(
    """
    import os, pathlib, signal, sys

    root = pathlib.Path(sys.argv[1])
    holder = (
        "import os, pathlib, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "identity = pathlib.Path(sys.argv[1]); "
        "pending = identity.with_name(f'.{identity.name}.pending'); "
        "pending.write_text(str(os.getpid())); pending.replace(identity); signal.pause()"
    )
    if os.fork() == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if os.fork() == 0:
            os.execve(sys.executable, [sys.executable, "-c", holder, str(root / "bare.pid")], {})
        while not (root / "bare.pid").is_file():
            signal.sigtimedwait([], 0.05)
        escapee_pid = root / "escapee.pid"
        pending_escapee_pid = root / ".escapee.pid.pending"
        pending_escapee_pid.write_text(str(os.getpid()))
        pending_escapee_pid.replace(escapee_pid)
        while True:
            signal.pause()
    while not (root / "escapee.pid").is_file():
        signal.sigtimedwait([], 0.05)
    """
)

# A command that submits a nested job: the nested job's process inherits the command's
# environment, the job's marker included (sbatch exports it), but runs under a job id of its
# own, in a session of its own. It ignores SIGTERM, so only teardown's SIGKILL would end it.
_NESTED = textwrap.dedent(
    """
    import os, pathlib, signal, subprocess, sys

    root = pathlib.Path(sys.argv[1])
    holder = (
        "import os, pathlib, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "identity = pathlib.Path(sys.argv[1]); "
        "pending = identity.with_name(f'.{identity.name}.pending'); "
        "pending.write_text(str(os.getpid())); pending.replace(identity); signal.pause()"
    )
    env = {**os.environ, "SLURM_JOB_ID": "nested-" + os.environ["SLURM_JOB_ID"]}
    subprocess.Popen(
        [sys.executable, "-c", holder, str(root / "nested.pid")], env=env, start_new_session=True
    )
    while not (root / "nested.pid").is_file():
        signal.sigtimedwait([], 0.05)
    """
)

# A command whose child leaves the job's sessions only when teardown's SIGTERM arrives, so
# teardown finds the new session after its first scan: on SIGTERM the child forks a process
# that calls setsid and then exits on its own SIGTERM, noting that it got one. SIGTERM stays
# blocked in that process until its handler is in place.
_LATE_ESCAPEE = textwrap.dedent(
    """
    import os, pathlib, signal, sys

    root = pathlib.Path(sys.argv[1])

    # SIGTERM stays blocked and is taken with sigwait: a handler followed by
    # signal.pause() loses a signal that lands after the handler check and before
    # pause() blocks.
    if os.fork() == 0:
        signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
        leaver_pid = root / "leaver.pid"
        pending_leaver_pid = root / ".leaver.pid.pending"
        pending_leaver_pid.write_text(str(os.getpid()))
        pending_leaver_pid.replace(leaver_pid)
        signal.sigwait([signal.SIGTERM])
        if os.fork() == 0:
            os.setsid()
            late_pid = root / "late.pid"
            pending_late_pid = root / ".late.pid.pending"
            pending_late_pid.write_text(str(os.getpid()))
            pending_late_pid.replace(late_pid)
            signal.sigwait([signal.SIGTERM])
            (root / "late-termed").write_text("yes")
            os._exit(0)
        os._exit(0)
    while not (root / "leaver.pid").is_file():
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
    grace_seconds: int = _GRACE_SECONDS,
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
        service_stop_grace_seconds=grace_seconds,
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


def _start_tracer(tmp_path: Path, hold: str, target: str = "child.pid") -> subprocess.Popen[bytes]:
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
        (sys.executable, str(program), str(root), hold, str(HANG_GUARD_S), target),
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
        assert any(row[:1] == [str(child)] and row[5].startswith("t") for row in rows), (
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


def test_a_service_that_never_became_ready_keeps_its_status_when_a_process_outlives_the_wait(
    tmp_path: Path,
) -> None:
    tracer = _start_tracer(tmp_path, "forever")
    root = tmp_path / "service"
    (root / "fail").write_text("yes", encoding="utf-8")
    try:
        result = _run_job(tmp_path, exit_wait_seconds=1)
        _require_tracer(tmp_path)

        # At the merge base the teardown overwrote 70 with 71, hiding why the job failed.
        assert result.exit_code == SERVICE_NOT_READY_EXIT_CODE, result.output
        child = (root / "child.pid").read_text(encoding="utf-8")
        assert "still alive 1 s after SIGKILL" in result.output
        rows = [line.split() for line in result.output.splitlines()]
        assert any(row[:1] == [child] for row in rows), result.output
    finally:
        tracer.kill()
        tracer.wait(HANG_GUARD_S)


def test_job_script_waits_for_a_worker_whose_main_thread_exited_before_its_others(
    tmp_path: Path,
) -> None:
    tracer = _start_tracer(tmp_path, "forever", target="thread.tid")
    root = tmp_path / "service"
    program = tmp_path / "threaded.py"
    program.write_text(_THREADED, encoding="utf-8")
    try:
        result = _run_job(
            tmp_path,
            command=(sys.executable, str(program), str(root)),
            service=False,
            exit_wait_seconds=1,
        )
        _require_tracer(tmp_path)

        worker = (root / "worker.pid").read_text(encoding="utf-8")
        # At the merge base the zombie main thread made the worker count as gone, and the
        # job ended with status 0 while its sibling thread was still held in its exit.
        assert result.exit_code == PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE, result.output
        rows = [line.split() for line in result.output.splitlines()]
        assert any(row[:1] == [worker] and row[5].startswith("Z") for row in rows), result.output
    finally:
        tracer.kill()
        tracer.wait(HANG_GUARD_S)


def test_job_script_ends_a_process_that_left_the_job_session_and_its_children(
    tmp_path: Path,
) -> None:
    root = tmp_path / "service"
    root.mkdir()
    program = tmp_path / "escapee.py"
    program.write_text(_ESCAPEE, encoding="utf-8")
    pid_files = (root / "escapee.pid", root / "bare.pid")
    try:
        result = _run_job(
            tmp_path, command=(sys.executable, str(program), str(root)), service=False
        )

        assert result.exit_code == 0, result.output
        # At the merge base neither was signalled: both outlived the job.
        for pid_file in pid_files:
            assert not _alive(int(pid_file.read_text(encoding="utf-8"))), pid_file.name
    finally:
        for pid_file in pid_files:
            if pid_file.is_file():
                with suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)


def test_job_script_leaves_a_nested_job_that_inherited_its_marker_alone(tmp_path: Path) -> None:
    """Review (2026-10-16): a nested sbatch job inherits the marker; it is not this job's."""
    root = tmp_path / "service"
    root.mkdir()
    program = tmp_path / "nested.py"
    program.write_text(_NESTED, encoding="utf-8")
    pid_file = root / "nested.pid"
    try:
        result = _run_job(
            tmp_path, command=(sys.executable, str(program), str(root)), service=False
        )

        assert result.exit_code == 0, result.output
        # At the merge base teardown matched the marker alone and killed the nested job.
        assert _alive(int(pid_file.read_text(encoding="utf-8")))
    finally:
        if pid_file.is_file():
            with suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)


def test_job_script_sends_sigterm_to_a_session_it_finds_after_its_first_scan(
    tmp_path: Path,
) -> None:
    """Review (2026-10-16): a session found after the first scan got only SIGKILL."""
    root = tmp_path / "service"
    root.mkdir()
    program = tmp_path / "late.py"
    program.write_text(_LATE_ESCAPEE, encoding="utf-8")
    pid_file = root / "late.pid"
    try:
        # The grace is as long as the configuration allows, and the late process exits
        # on SIGTERM, so a passing run ends well before it.
        result = _run_job(
            tmp_path,
            command=(sys.executable, str(program), str(root)),
            service=False,
            grace_seconds=20,
        )

        assert result.exit_code == 0, result.output
        # At the merge base it got only SIGKILL, at the end of the grace.
        assert (root / "late-termed").is_file(), result.output
        assert not _alive(int(pid_file.read_text(encoding="utf-8")))
    finally:
        for name in ("late.pid", "leaver.pid"):
            if (root / name).is_file():
                with suppress(ProcessLookupError):
                    os.kill(int((root / name).read_text(encoding="utf-8")), signal.SIGKILL)
