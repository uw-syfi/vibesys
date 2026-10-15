"""A stop reaches a container program whose first signal request found nothing to signal.

A cancel or timeout can land while a ``docker exec`` is still starting its program in
the daemon. The first signal request then reaches no process, and killing the local
client afterwards leaves the program running unreachable. The sandbox must signal again
once the client is gone.
"""

from __future__ import annotations

import os
import select
import threading
from typing import TYPE_CHECKING

from vs_sandbox.api import CommandStopUnconfirmedError, DockerSandbox
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import OsThreads, SubprocessPipedProcessLauncher
from vs_sim.api.testing import join_or_fail

if TYPE_CHECKING:
    from pathlib import Path

_EXIT_BOUND_SECONDS = 30.0  # Reached only when a stop failed; the pass path waits on the event.


def test_a_stop_after_a_lost_signal_request_still_ends_the_program(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "engine-state").mkdir()
    os.mkfifo(workspace / "ready")
    os.mkfifo(workspace / "alive")
    # The program holds a write end of `alive`; end-of-file means it exited.
    alive = os.open(workspace / "alive", os.O_RDONLY | os.O_NONBLOCK)
    engine = FakeDockerEngine(
        tmp_path / "engine-state",
        agent_ids=(os.getuid(), os.getgid()),
        launcher=SubprocessPipedProcessLauncher(),
    )
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image="fake-image",
        agent_uid=os.getuid(),
        agent_gid=os.getgid(),
        docker=engine,
        threads=OsThreads(),
    )
    sandbox.start()
    try:
        engine.signal_requests_find_nothing(1)
        cancel = threading.Event()
        command = "exec 3> alive\nsleep 1000 &\necho go > ready\nwait"
        outcome: list[bool] = []
        worker = threading.Thread(
            target=lambda: outcome.append(
                sandbox.execute(command, timeout=3600, cancel=cancel).cancelled
            )
        )
        worker.start()
        (workspace / "ready").read_text(encoding="utf-8")
        cancel.set()
        join_or_fail(worker)

        readable, _, _ = select.select([alive], [], [], _EXIT_BOUND_SECONDS)
        assert outcome == [True]
        assert readable
        assert os.read(alive, 1) == b""
    finally:
        os.close(alive)
        sandbox.stop()


def _cancel_a_running_program(
    tmp_path: Path, *, failing_signal_requests: int
) -> tuple[DockerSandbox, FakeDockerEngine, list[object]]:
    """Cancel a program in a fake container whose next signal requests fail."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "engine-state").mkdir()
    os.mkfifo(workspace / "ready")
    engine = FakeDockerEngine(
        tmp_path / "engine-state",
        agent_ids=(os.getuid(), os.getgid()),
        launcher=SubprocessPipedProcessLauncher(),
    )
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image="fake-image",
        agent_uid=os.getuid(),
        agent_gid=os.getgid(),
        docker=engine,
        threads=OsThreads(),
    )
    sandbox.start()
    engine.signal_requests_fail(failing_signal_requests)
    cancel = threading.Event()
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(
                sandbox.execute("echo go > ready\nsleep 1000", timeout=3600, cancel=cancel)
            )
        except CommandStopUnconfirmedError as error:
            outcome.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    (workspace / "ready").read_text(encoding="utf-8")
    cancel.set()
    join_or_fail(worker)
    return sandbox, engine, outcome


def test_a_failed_signal_request_is_retried_until_one_answers(tmp_path: Path) -> None:
    sandbox, _, outcome = _cancel_a_running_program(tmp_path, failing_signal_requests=2)
    try:
        assert [type(result).__name__ for result in outcome] == ["CommandResult"]
    finally:
        sandbox.stop()


def test_a_stop_whose_signal_requests_all_fail_is_unconfirmed(tmp_path: Path) -> None:
    """Killing the client alone proves nothing about the program in the container."""
    sandbox, _, outcome = _cancel_a_running_program(tmp_path, failing_signal_requests=1000)
    try:
        assert len(outcome) == 1
        assert isinstance(outcome[0], CommandStopUnconfirmedError)
        assert outcome[0].survivors is None
        assert outcome[0].result.cancelled
    finally:
        sandbox.stop()
