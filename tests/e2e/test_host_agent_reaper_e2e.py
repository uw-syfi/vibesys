"""A Host agent is found by its mark and ended on this host, with the host's real process listing.

The argv is the one production gives an agent on the host: ``confine_to_sandbox`` led by a shell
that carries the mark as an argument (``leads_group``), which is all macOS shows of a process. It
is started the way agentshim's host executor starts a command, in a session of its own with the
mark in its environment. Nothing records the process: the reaper of the Host environment finds
it through ``OsProcessTable``, which reads ``/proc`` on Linux and ``ps`` on macOS.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import agentshim
import pytest
from agentshim.testing import FakeExecutor, FakeRun

from vs_agent.session_launch import confine_to_sandbox
from vs_core.api import RequestId
from vs_runtime.api.infrastructure import HostEnvironment
from vs_runtime.api.stage_marks import agent_mark
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api import step_tag_env
from vs_sim.api import OsProcessTable, parse_ps
from vs_sim.api.testing import InlineBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from vs_project.api import StateNamespace

pytestmark = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="no process table for this platform",
)

RUN = "run-a"
_AGENT = "import time; time.sleep(600)"
_LEFT_RUNNING: list[int] = []


class _Unconfined:
    """A sandbox that confines nothing, so the argv under test is only the launch's own."""

    @property
    def env(self) -> Mapping[str, str]:
        return {}

    def wrap(self, argv: list[str], cwd: Path | str | None = None, /) -> list[str]:
        del cwd
        return list(argv)

    def agent_path(self, path: Path | str, /) -> str:
        return str(path)


@pytest.fixture(autouse=True)
def _end_what_a_test_left() -> Iterator[None]:
    yield
    while _LEFT_RUNNING:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(_LEFT_RUNNING.pop(), signal.SIGKILL)


def _no_namespace(name: str) -> StateNamespace:
    message = f"the {name} state namespace was read"
    raise AssertionError(message)


def _running_in_group(pgid: int) -> int:
    listing = subprocess.run(
        ["ps", "-axww", "-o", "pid=,pgid=,stat=,lstart=,command="],  # noqa: S607  # lint-waiver: LW-914331 [S607]; ps on PATH, as the table under test runs it.
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sum(1 for row in parse_ps(listing) if row.pgid == pgid and not row.stat.startswith("Z"))


def _start_agent(tag: RequestId) -> subprocess.Popen[bytes]:
    """Launch an agent for the step *tag* the way a Host turn is launched."""
    mark = agent_mark(RUN, tag)
    recorder = FakeExecutor([FakeRun()])
    executor = confine_to_sandbox(recorder, _Unconfined(), leads_group=True)
    environment = {**os.environ, **step_tag_env(mark)}
    request = agentshim.CommandRequest(
        argv=[sys.executable, "-I", "-c", _AGENT],
        stdin=None,
        cwd=None,
        env=environment,
        timeout=None,
    )
    executor.run(request, agentshim.NullSink())
    argv = list(recorder.requests[-1].argv)
    process = subprocess.Popen(argv, env=environment, start_new_session=True)  # noqa: S603  # lint-waiver: LW-914332 [S603]; a real-tier test starts its own fixed argv.
    _LEFT_RUNNING.append(process.pid)
    return process


def test_a_host_agent_is_ended_by_its_mark_and_another_step_s_agent_is_left() -> None:
    target, bystander = RequestId(root="step-a"), RequestId(root="step-b")
    ended, kept = _start_agent(target), _start_agent(bystander)
    reaper = HostEnvironment().agent_work_reaper(
        RUN,
        system=replace(idle_environment_system(), process_table=OsProcessTable()),
        blocking=InlineBlockingRunner(),
        local_namespace=_no_namespace,
        log=lambda _line: None,
    )
    assert _running_in_group(ended.pid) > 0

    run_virtual(VirtualClock(0.0), reaper.reap(target))

    assert _running_in_group(ended.pid) == 0
    assert _running_in_group(kept.pid) > 0
