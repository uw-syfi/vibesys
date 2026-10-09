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

from vs_sandbox.api import DockerSandbox
from vs_sandbox.api.testing import FakeDockerEngine

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
    engine = FakeDockerEngine(tmp_path / "engine-state", agent_ids=(os.getuid(), os.getgid()))
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image="fake-image",
        agent_uid=os.getuid(),
        agent_gid=os.getgid(),
        docker=engine,
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
        worker.join()

        readable, _, _ = select.select([alive], [], [], _EXIT_BOUND_SECONDS)
        assert outcome == [True]
        assert readable
        assert os.read(alive, 1) == b""
    finally:
        os.close(alive)
        sandbox.stop()
