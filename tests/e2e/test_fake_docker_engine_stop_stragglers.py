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
from vs_sim.api import DISCARD

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
    sandbox.start(output=DISCARD)
    try:
        engine.signal_requests_find_nothing(1)
        cancel = threading.Event()
        command = "exec 3> alive\nsleep 1000 &\necho go > ready\nwait"
        outcome: list[bool] = []
        worker = threading.Thread(
            target=lambda: outcome.append(
                sandbox.execute(command, timeout=3600, cancel=cancel, output=DISCARD).cancelled
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
    tmp_path: Path, *, failing_signal_requests: int = 0, unreadable_signal_requests: int = 0
) -> tuple[DockerSandbox, FakeDockerEngine, list[object]]:
    """Cancel a program in a fake container whose next signal requests fail or meet doubt."""
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
    sandbox.start(output=DISCARD)
    engine.signal_requests_fail(failing_signal_requests)
    engine.signal_requests_meet_unreadable(unreadable_signal_requests)
    cancel = threading.Event()
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(
                sandbox.execute(
                    "echo go > ready\nsleep 1000", timeout=3600, cancel=cancel, output=DISCARD
                )
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


def test_a_stop_whose_first_answer_is_unknown_is_confirmed_by_a_later_count(
    tmp_path: Path,
) -> None:
    sandbox, _, outcome = _cancel_a_running_program(tmp_path, unreadable_signal_requests=1)
    try:
        assert [type(result).__name__ for result in outcome] == ["CommandResult"]
    finally:
        sandbox.stop()
