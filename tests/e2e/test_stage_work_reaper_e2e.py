"""The stage work reaper ends real processes by the step's mark, after their owner died.

An owner process starts a stage in a process group of its own with the step's mark in the
stage's environment, as a local evaluation's candidate session does. The owner is killed, the
stage lives on, and a reaper built over the host's process table ends it by the tag alone. Linux
only: the process table reads ``/proc``.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_core.api import RequestId
from vs_runtime._evaluation_jobs import handle_of_life, is_handle_of
from vs_runtime.api.evaluation_step_runners import MarkedStageWorkReaper
from vs_runtime.api.testing import StageWorkReaperContract, StageWorkUnderTest
from vs_sandbox.api import STEP_TAG_ENV
from vs_sim.api import OsProcessTable, ProcessTableUnavailableError
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_sim.api import MarkedProcess

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the process table reads /proc"
)

_START_BOUND_S = 30.0
"""Upper bound on a child reporting it started; reached only when it failed to start."""

_OWNER = """
import os, subprocess, sys
stage = subprocess.Popen(
    ["sh", "-c", "sleep 600 & wait"],
    env={**os.environ, sys.argv[1]: sys.argv[2]},
    start_new_session=True,
    stdout=subprocess.DEVNULL,
)
print(stage.pid, flush=True)
stage.wait()
"""


def _reported_pid(owner: subprocess.Popen[str]) -> int:
    assert owner.stdout is not None
    ready, _, _ = select.select([owner.stdout], [], [], _START_BOUND_S)
    assert ready, "the owner did not report its stage"
    return int(owner.stdout.readline())


def _state(pid: int) -> str | None:
    """The kernel's one-letter state of *pid*, or ``None`` once it is gone."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return None


def _gone(pid: int) -> bool:
    return _state(pid) in (None, "Z")


def _group_members(pgid: int) -> list[int]:
    return [
        int(entry)
        for entry in (item.name for item in Path("/proc").iterdir())
        if entry.isdigit() and _pgid(int(entry)) == pgid and not _gone(int(entry))
    ]


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _spawn(
    argv: list[str],
    env: dict[str, str],
    *,
    stdout: int,
    new_session: bool = False,
    text: bool = False,
) -> subprocess.Popen[str]:
    return subprocess.Popen(argv, env=env, stdout=stdout, start_new_session=new_session, text=text)  # noqa: S603  # lint-waiver: LW-914301 [S603]; a real-tier test starts its own fixed argv.
    # > A wrapper would only rename subprocess.Popen; the argv is built in this file from constants.


def _start_stage_and_kill_owner(handle: str) -> int:
    owner = _spawn(
        [sys.executable, "-I", "-c", _OWNER, STEP_TAG_ENV, handle],
        dict(os.environ),
        stdout=subprocess.PIPE,
        text=True,
    )
    stage_pid = _reported_pid(owner)
    owner.kill()
    owner.wait(timeout=_START_BOUND_S)
    return stage_pid


def test_a_stage_whose_owner_died_is_ended_by_its_tag_and_its_group_is_gone() -> None:
    tag = RequestId(root="e2e-stage")
    handle = handle_of_life(tag, 1)
    stage_pid = _start_stage_and_kill_owner(handle)
    pgid = os.getpgid(stage_pid)
    assert _group_members(pgid), "the stage should outlive its owner"
    table = OsProcessTable()
    reaper = MarkedStageWorkReaper(table)
    run_virtual(VirtualClock(0.0), reaper.reap(tag))
    assert _group_members(pgid) == []
    assert table.marked(STEP_TAG_ENV, lambda value: is_handle_of(tag, value)) == ()


class _RefusableProcessTable:
    """The host's process table, which can be made unreadable."""

    def __init__(self) -> None:
        self._real = OsProcessTable()
        self.unreadable = False

    def marked(self, name: str, matches: Callable[[str], bool]) -> tuple[MarkedProcess, ...]:
        if self.unreadable:
            raise ProcessTableUnavailableError
        return self._real.marked(name, matches)

    def kill(self, process: MarkedProcess, name: str) -> bool:
        return self._real.kill(process, name)


class TestMarkedStageWorkReaperOverRealProcesses(StageWorkReaperContract):
    def stage_work_under_test(self) -> StageWorkUnderTest:
        table = _RefusableProcessTable()

        def leave(tag: RequestId) -> None:
            _spawn(
                ["sh", "-c", "sleep 120 & wait"],
                {**os.environ, STEP_TAG_ENV: handle_of_life(tag, 0)},
                new_session=True,
                stdout=subprocess.DEVNULL,
            )

        @contextmanager
        def unreachable() -> Iterator[None]:
            table.unreadable = True
            try:
                yield
            finally:
                table.unreadable = False

        return StageWorkUnderTest(
            reaper=MarkedStageWorkReaper(table),
            run=lambda main: run_virtual(VirtualClock(0.0), main),
            leave=leave,
            running=lambda tag: len(table.marked(STEP_TAG_ENV, lambda v: is_handle_of(tag, v))),
            unreachable=unreachable,
        )
