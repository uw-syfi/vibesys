"""The Docker CLI adapter starts its commands in their own session, on a real process.

Ctrl-C signals the terminal's foreground process group; a ``docker stop`` started in
that group dies mid-flight. Only a real child process can report its own session, so
this belongs to the real-system tier.
"""

from __future__ import annotations

import sys

from vs_sandbox.api import SubprocessDockerCli


def test_docker_lifecycle_commands_run_outside_the_terminals_process_group() -> None:
    probe = "import os; print(os.getsid(0) == os.getpid())"

    result = SubprocessDockerCli().run([sys.executable, "-c", probe], timeout_seconds=30)

    assert result.stdout.strip() == "True"
