"""Real-process proofs for the live-instance registry and detached sessions."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from entrypoints.server import _DetachedGatewayEffects
from server.instances import (
    ControlSocketStopRequester,
    FileInstanceStore,
    LiveRegistry,
    StopEffects,
    StopOutcome,
    StopRoute,
    instance_root,
)
from vs_sim.api import OsThreads, PidfdProcessSignaller, UnixNetwork
from vs_sim.api.testing import HANG_GUARD_S, stop_process

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

INSTANCE = "00000000e2e0"


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveRegistry:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return LiveRegistry(FileInstanceStore(instance_root(os.environ, os.getuid())))


@pytest.fixture
def holder(registry: LiveRegistry) -> Iterator[subprocess.Popen[str]]:
    del registry  # the child inherits the XDG_RUNTIME_DIR that fixture set
    child = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-178208 [S603]; the test runs its own fixed child module.
        # > Proving liveness across kill -9 needs a real process the kernel reaps.
        [sys.executable, "-m", "tests.e2e.live_instance_child", INSTANCE],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == "ready\n"
        yield child
    finally:
        stop_process(child)


def test_a_killed_server_is_listed_until_it_dies_and_then_removed(
    registry: LiveRegistry, holder: subprocess.Popen[str]
) -> None:
    listing = registry.list()
    assert [(record.id, record.pid) for record in listing.instances] == [(INSTANCE, holder.pid)]

    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=HANG_GUARD_S)

    assert registry.list().instances == ()
    assert registry.find(INSTANCE) is None
    assert registry.list().unverified == ()


def test_a_server_that_exits_cleanly_leaves_nothing_behind(
    registry: LiveRegistry, holder: subprocess.Popen[str], tmp_path: Path
) -> None:
    assert holder.stdin is not None
    holder.stdin.close()
    assert holder.wait(timeout=HANG_GUARD_S) == 0

    assert list((tmp_path / "vibesys" / "instances").iterdir()) == []
    assert registry.list().instances == ()


@pytest.mark.skipif(sys.platform != "linux", reason="stable pid signalling needs Linux pidfds")
def test_stop_terminates_a_live_server_and_removes_its_record(
    registry: LiveRegistry, holder: subprocess.Popen[str]
) -> None:
    threads = OsThreads()

    # The child binds no control socket, so the stop falls back to the signal.
    result = registry.stop(
        INSTANCE,
        StopEffects(
            ControlSocketStopRequester(UnixNetwork()),
            PidfdProcessSignaller(),
            threads,
            threads.sleep,
        ),
    )

    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert holder.wait(timeout=HANG_GUARD_S) == -signal.SIGTERM
    assert registry.list().instances == ()


def test_a_detached_child_runs_in_its_own_session_without_a_terminal(tmp_path: Path) -> None:
    """No controlling terminal and a new session: a closed SSH session cannot hang it up."""
    probe = (
        "import os, sys\n"
        "print(os.getsid(0) == os.getpid())\n"
        "try:\n"
        "    os.open('/dev/tty', os.O_RDONLY)\n"
        "    print('tty')\n"
        "except OSError:\n"
        "    print('no tty')\n"
    )
    log = tmp_path / "child.log"
    with log.open("w+b") as output:
        child = _DetachedGatewayEffects().spawn([sys.executable, "-c", probe], {}, output)
        try:
            assert child.wait(timeout=HANG_GUARD_S) == 0
        finally:
            if child.poll() is None:
                child.kill()

    assert log.read_text().splitlines() == ["True", "no tty"]
