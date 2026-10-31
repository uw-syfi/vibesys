"""The stage work reapers end real processes and containers by the step's mark.

Processes are started the way a local evaluation starts a stage: through ``LocalShellRunner``
with the step's mark in its environment, in a child that plays the dead owner. The owner is
killed, the stage lives on, and a reaper built over the host's process table ends it by the tag
alone. Runs on Linux (the table reads ``/proc``) and on macOS (the table reads ``ps``, which
shows only a group leader's arguments); the contract's cases run on each. Containers are the
real ``DockerSandbox`` over ``FakeDockerEngine``, whose containers are real shell processes.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_core.api import RequestId
from vs_runtime._evaluation_jobs import handle_of_life
from vs_runtime._stage_marks import is_stage_mark_of, stage_mark
from vs_runtime.api.evaluation_step_runners import (
    ContainerStageWorkReaper,
    ProcessStageWorkReaper,
)
from vs_runtime.api.testing import StageWorkPlace, StageWorkReaperContract, StageWorkUnderTest
from vs_sandbox.api import (
    STEP_TAG_ENV,
    STEP_TAG_LABEL,
    DockerSandbox,
    DockerStepContainers,
    StepContainer,
    StepContainerError,
    StepContainers,
    step_shell_argv,
)
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import (
    DISCARD,
    MarkedProcess,
    OsProcessTable,
    OsThreads,
    ProcessSurvivedError,
    ProcessTableUnavailableError,
    SubprocessPipedProcessLauncher,
    parse_ps,
)
from vs_sim.api.testing import InlineBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="no process table for this platform",
)

RUN = "run-a"
OTHER_RUN = "run-b"
_START_BOUND_S = 120.0
"""Upper bound on a child reporting it started; reached only when it failed to start."""
_STAGE = "sleep 120 & echo ready; wait"

# The dead owner: runs a stage through the production launch path and prints the stage shell's pid.
_OWNER = """
import sys
from pathlib import Path
from vs_sandbox.api import LocalShellRunner
from vs_sim.api import OsThreads, OutputStream, SubprocessPipedProcessLauncher

class Printing:
    def write(self, stream, data):
        sys.stdout.write(data.decode()); sys.stdout.flush()

LocalShellRunner(
    Path(sys.argv[3]), env={sys.argv[1]: sys.argv[2]}, inherit_env=True,
    threads=OsThreads(), launcher=SubprocessPipedProcessLauncher(),
).execute(sys.argv[4], timeout=600, output=Printing())
"""


def _ps_rows() -> tuple:
    return parse_ps(
        subprocess.run(
            ["ps", "-axww", "-o", "pid=,pgid=,stat=,lstart=,command="],  # noqa: S607  # lint-waiver: LW-914311 [S607]; ps on PATH, as the table under test runs it.
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def _running_in_group(pgid: int) -> int:
    return sum(1 for row in _ps_rows() if row.pgid == pgid and not row.stat.startswith("Z"))


_LEFT_RUNNING: list[int] = []


@pytest.fixture(autouse=True)
def _end_what_a_test_left() -> Iterator[None]:
    yield
    while _LEFT_RUNNING:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(_LEFT_RUNNING.pop(), signal.SIGKILL)


def _spawn(
    argv: list[str], env: dict[str, str], *, stdout: int, session: bool
) -> subprocess.Popen[str]:
    process = subprocess.Popen(argv, env=env, stdout=stdout, start_new_session=session, text=True)  # noqa: S603  # lint-waiver: LW-914301 [S603]; a real-tier test starts its own fixed argv.
    # > A wrapper would only rename subprocess.Popen; the argv is built in this file from constants.
    if session:
        _LEFT_RUNNING.append(process.pid)
    return process


def _owner_with_stage(mark: str, workdir: Path) -> tuple[subprocess.Popen[str], int]:
    owner = _spawn(
        [
            sys.executable,
            "-I",
            "-c",
            _OWNER,
            STEP_TAG_ENV,
            mark,
            str(workdir),
            f"echo $$; {_STAGE}",
        ],
        {},
        stdout=subprocess.PIPE,
        session=False,
    )
    pid = int(_await_line(owner))
    assert _await_line(owner).strip() == "ready"
    _LEFT_RUNNING.append(_pgid_of(pid))
    return owner, pid


def _await_line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], _START_BOUND_S)
    assert ready, "the process did not report it started"
    return process.stdout.readline()


def _kill_owner(owner: subprocess.Popen[str]) -> None:
    owner.kill()
    owner.wait(timeout=_START_BOUND_S)


def _reap(tag: RequestId, table: OsProcessTable | None = None) -> None:
    reaper = ProcessStageWorkReaper(table or OsProcessTable(), RUN, InlineBlockingRunner())
    run_virtual(VirtualClock(0.0), reaper.reap(tag))


def test_a_stage_whose_owner_died_is_ended_by_its_tag_and_its_group_is_gone(tmp_path: Path) -> None:
    tag = RequestId(root="e2e-stage")
    owner, shell_pid = _owner_with_stage(stage_mark(RUN, handle_of_life(tag, 1)), tmp_path)
    pgid = _pgid_of(shell_pid)
    _kill_owner(owner)
    assert _running_in_group(pgid) > 0, "the stage should outlive its owner"
    _reap(tag)
    assert _running_in_group(pgid) == 0


def test_the_launcher_leaves_the_mark_in_the_leaders_arguments() -> None:
    mark = stage_mark(RUN, "vs-x")
    argv = step_shell_argv("/bin/sh", "echo hi", {STEP_TAG_ENV: mark})
    done = subprocess.run(  # noqa: S603  # lint-waiver: LW-914312 [S603]; the test's own fixed argv.
        argv, capture_output=True, text=True, check=True
    )
    assert done.stdout == "hi\n"
    assert f"{STEP_TAG_ENV}={mark}" in argv


def test_the_members_of_a_group_whose_leader_died_first_are_found_on_linux_only(
    tmp_path: Path,
) -> None:
    """The named residual: on macOS only the leader carries the mark that ``ps`` shows."""
    tag = RequestId(root="e2e-leader")
    owner, shell_pid = _owner_with_stage(stage_mark(RUN, handle_of_life(tag, 0)), tmp_path)
    pgid = _pgid_of(shell_pid)
    _kill_owner(owner)
    leader = next(row for row in _ps_rows() if row.pid == pgid)
    os.kill(leader.pid, signal.SIGKILL)
    try:
        _reap(tag)
        found_on_linux = sys.platform.startswith("linux")
        assert (_running_in_group(pgid) == 0) == found_on_linux
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)


def _pgid_of(pid: int) -> int:
    return next(row.pgid for row in _ps_rows() if row.pid == pid)


class _RefusableProcessTable:
    """The host's process table, which can be unreadable or unable to end what it kills."""

    def __init__(self) -> None:
        self._real = OsProcessTable()
        self.unreadable = False
        self.survivors = False

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        if self.unreadable:
            raise ProcessTableUnavailableError
        return self._real.marked(name, matches)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        if self.survivors:
            raise ProcessSurvivedError
        return self._real.kill(process, name)


@contextmanager
def _flag(owner: object, name: str) -> Iterator[None]:
    setattr(owner, name, True)
    try:
        yield
    finally:
        setattr(owner, name, False)


class TestProcessStageWorkReaperOverRealProcesses(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        table = _RefusableProcessTable()
        groups: dict[tuple[str, str], list[int]] = {}
        spawned: list[subprocess.Popen[str]] = []

        def leave(run_id: str, tag: RequestId) -> None:
            mark = stage_mark(run_id, handle_of_life(tag, 0))
            argv = step_shell_argv("/bin/sh", _STAGE, {STEP_TAG_ENV: mark})
            stage = _spawn(
                list(argv), {STEP_TAG_ENV: mark}, stdout=subprocess.DEVNULL, session=True
            )
            spawned.append(stage)
            groups.setdefault((run_id, tag.root), []).append(stage.pid)

        def running(run_id: str, tag: RequestId) -> int:
            return sum(
                1 for pgid in groups.get((run_id, tag.root), []) if _running_in_group(pgid) > 0
            )

        return StageWorkUnderTest(
            reaper=ProcessStageWorkReaper(table, RUN, InlineBlockingRunner()),
            run=lambda main: run_virtual(VirtualClock(0.0), main),
            places=(StageWorkPlace.LEADER, StageWorkPlace.DESCENDANT),
            leave=lambda tag, _place: leave(RUN, tag),
            leave_in_other_run=lambda tag, _place: leave(OTHER_RUN, tag),
            running=lambda tag: running(RUN, tag),
            running_in_other_run=lambda tag: running(OTHER_RUN, tag),
            unreachable=lambda: _flag(table, "unreadable"),
            stuck=lambda: _flag(table, "survivors"),
        )


class TestContainerStageWorkReaperOverTheDockerSandbox(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        scratch = Path(tempfile.mkdtemp())
        (scratch / "state").mkdir()
        engine = FakeDockerEngine(scratch / "state", launcher=SubprocessPipedProcessLauncher())
        sandboxes: list[DockerSandbox] = []

        daemon = _RefusableContainers(DockerStepContainers(engine, RUN))

        def leave(run_id: str, tag: RequestId) -> None:
            workspace = scratch / f"w{len(sandboxes)}"
            workspace.mkdir()
            sandbox = DockerSandbox(
                host_workspace=str(workspace),
                image="fake-image",
                docker=engine,
                threads=OsThreads(),
                env={STEP_TAG_ENV: stage_mark(run_id, handle_of_life(tag, 0))},
                run_id=run_id,
            )
            sandbox.start(output=DISCARD)
            sandboxes.append(sandbox)

        def running(run_id: str, tag: RequestId) -> int:
            return sum(
                1
                for container in engine.containers()
                if container.labels.get("vibesys.run-id") == run_id
                and is_stage_mark_of(run_id, tag, container.labels.get(STEP_TAG_LABEL, ""))
            )

        return StageWorkUnderTest(
            reaper=ContainerStageWorkReaper(daemon, RUN, InlineBlockingRunner()),
            run=lambda main: run_virtual(VirtualClock(0.0), main),
            places=(StageWorkPlace.CONTAINER,),
            leave=lambda tag, _place: leave(RUN, tag),
            leave_in_other_run=lambda tag, _place: leave(OTHER_RUN, tag),
            running=lambda tag: running(RUN, tag),
            running_in_other_run=lambda tag: running(OTHER_RUN, tag),
            unreachable=lambda: _flag(daemon, "unreachable"),
            stuck=lambda: _stuck_removals(engine),
        )


@contextmanager
def _stuck_removals(engine: FakeDockerEngine) -> Iterator[None]:
    engine.removals_lost(1_000)
    try:
        yield
    finally:
        engine.removals_lost(0)


class _RefusableContainers:
    """The step containers of a daemon that can become unreachable."""

    def __init__(self, daemon: StepContainers) -> None:
        self._daemon = daemon
        self.unreachable = False

    def listed(self) -> tuple[StepContainer, ...]:
        if self.unreachable:
            raise StepContainerError
        return self._daemon.listed()

    def remove(self, container: StepContainer) -> None:
        if self.unreachable:
            raise StepContainerError
        self._daemon.remove(container)
