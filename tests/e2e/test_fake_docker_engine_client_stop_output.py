"""A stop of the ``docker exec`` client keeps the output its program already wrote.

The fake daemon's exec client relays a program's output to the sandbox. A real
``docker exec`` client delivers what the program wrote before a stop; the fake must
too, or a cancel contract that waits for the program to announce itself would see
partial output vanish whenever the client was slow to read.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import DISCARD, SubprocessPipedProcessLauncher
from vs_sim.api.testing import HANG_GUARD_S


def test_a_stop_delivers_output_written_before_the_client_read_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "state").mkdir()
    os.mkfifo(workspace / "started")
    os.mkfifo(workspace / "go")
    os.mkfifo(workspace / "written")
    engine = FakeDockerEngine(tmp_path / "state", launcher=SubprocessPipedProcessLauncher())
    created = engine.run(
        ("docker", "run", "-d", "-v", f"{workspace}:/w", "--workdir", "/w", "image"),
        timeout_seconds=30,
        output=DISCARD,
    )
    container = created.stdout.strip()
    script = "echo up > started; read _ < go; printf out; printf err >&2; echo done > written; sleep 1000"
    client = engine.spawn(("docker", "exec", "-w", "/w", container, "sh", "-c", script))

    # Freeze the client before the program writes, so the output sits unread in the
    # pipes when the stop arrives: the interleaving a loaded host produces by chance.
    (workspace / "started").read_text(encoding="utf-8")
    client_pid = _exec_client_pid(tmp_path / "state")
    os.kill(client_pid, signal.SIGSTOP)
    os.waitpid(client_pid, os.WUNTRACED)
    (workspace / "go").write_text("go\n", encoding="utf-8")
    (workspace / "written").read_text(encoding="utf-8")
    client.terminate_group()
    os.kill(client_pid, signal.SIGCONT)
    stdout = client.stdout.read()
    stderr = client.stderr.read()
    status = client.wait(HANG_GUARD_S)
    engine.run(("docker", "rm", "-f", container), timeout_seconds=30, output=DISCARD)

    assert (stdout, stderr) == ("out", "err")
    assert status == -signal.SIGTERM


def _exec_client_pid(state_dir: Path) -> int:
    """The pid of the engine's one exec client, whose spec names *state_dir*."""
    marker = str(state_dir).encode()
    pids = [
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit() and marker in _cmdline(entry)
    ]
    assert len(pids) == 1, pids
    return pids[0]


def _cmdline(process: Path) -> bytes:
    try:
        return (process / "cmdline").read_bytes()
    except OSError:
        return b""
