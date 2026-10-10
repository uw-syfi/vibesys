"""A stop of the ``docker exec`` client keeps the output its program already wrote.

The fake daemon's exec client relays a program's output to the sandbox. A real
``docker exec`` client delivers what the program wrote before a stop; the fake must
too, or a cancel contract that waits for the program to announce itself would see
partial output vanish whenever the client was slow to read.
"""

from __future__ import annotations

import os
import signal
from typing import TYPE_CHECKING

from vs_sandbox.api.testing import FakeDockerEngine

if TYPE_CHECKING:
    from pathlib import Path


def test_a_stop_delivers_output_written_before_the_client_read_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "state").mkdir()
    os.mkfifo(workspace / "started")
    os.mkfifo(workspace / "go")
    os.mkfifo(workspace / "written")
    engine = FakeDockerEngine(tmp_path / "state")
    created = engine.run(
        ("docker", "run", "-d", "-v", f"{workspace}:/w", "--workdir", "/w", "image"),
        timeout_seconds=30,
    )
    container = created.stdout.strip()
    script = "echo up > started; read _ < go; printf out; printf err >&2; echo done > written; sleep 1000"
    client = engine.spawn(("docker", "exec", "-w", "/w", container, "sh", "-c", script))

    # Freeze the client before the program writes, so the output sits unread in the
    # pipes when the stop arrives: the interleaving a loaded host produces by chance.
    (workspace / "started").read_text(encoding="utf-8")
    os.kill(client.pid, signal.SIGSTOP)
    os.waitpid(client.pid, os.WUNTRACED)
    (workspace / "go").write_text("go\n", encoding="utf-8")
    (workspace / "written").read_text(encoding="utf-8")
    os.kill(client.pid, signal.SIGTERM)
    os.kill(client.pid, signal.SIGCONT)
    stdout, stderr = client.communicate()
    engine.run(("docker", "rm", "-f", container), timeout_seconds=30)

    assert (stdout, stderr) == ("out", "err")
    assert client.returncode == -signal.SIGTERM
