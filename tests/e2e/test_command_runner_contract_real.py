"""The ``CommandRunner.execute`` contract held against the implementations that run real processes.

``LocalShellRunner``, ``DockerSandbox`` over ``FakeDockerEngine`` (the real sandbox
class, a fake daemon whose containers are real shell processes) and ``DockerSandbox``
over a real daemon (opt in: ``VIBESYS_E2E_DOCKER=1`` with ``docker`` on PATH) each run
the cases in ``tests/support/command_runner_contract.py``. These spawn processes,
signal them and read named pipes, so they belong to the real-system tier; the pure
``FakeCommandRunner`` passes the same cases on simulated threads from
``libs/vs-sandbox/tests/test_sandbox_contract.py``.

A cancel is sent only after the command reports it is running (a named pipe it
writes to), and the process tree is checked through a pipe whose writers are the
command's processes: its end-of-file means every one of them is gone.
"""

from __future__ import annotations

import contextlib
import errno
import os
import select
import shlex
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from tests.support.command_runner_contract import (
    CAP,
    PARTIAL_STDERR,
    PARTIAL_STDOUT,
    CommandRunnerContract,
    Harness,
)

from vs_agent.api.images import agent_image
from vs_sandbox.api import CommandRunner, DockerSandbox, LocalShellRunner
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import OsThreads, Threads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

#: Upper bound on a stopped process exiting; reached only when a stop failed.
_EXIT_BOUND_SECONDS = 30.0
_E2E_DOCKER = "VIBESYS_E2E_DOCKER"
_DOCKER_BASE_IMAGE = "python:3.12-bookworm"
_DOCKER_BUILD_TIMEOUT_S = 600.0


@dataclass
class _ShellHarness:
    """A harness for a sandbox that runs real shell commands in ``workspace``."""

    sandbox: CommandRunner
    workspace: Path
    threads: Threads = field(default_factory=OsThreads)
    _alive_reader: int = -1

    def __post_init__(self) -> None:
        os.mkfifo(self.workspace / "ready")
        os.mkfifo(self.workspace / "alive")
        # The command's processes hold write ends of `alive`; this read end
        # sees end-of-file only once all of them have exited.
        self._alive_reader = os.open(self.workspace / "alive", os.O_RDONLY | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self._alive_reader)

    def run[T](self, program: Callable[[], T]) -> T:
        return program()

    def emit(self, stdout: str, stderr: str, exit_code: int) -> str:
        return (
            f"printf %s {shlex.quote(stdout)}; printf %s {shlex.quote(stderr)} >&2;"
            f" exit {exit_code}"
        )

    def killed_by(self, signal_number: int) -> str:
        return f"kill -{signal_number} $$"

    def hang(self, *, announce: bool) -> str:
        # Relative paths: the command runs in the workspace on every kind.
        steps = [
            "exec 3> alive",
            "sleep 1000 &",
            f"printf %s {PARTIAL_STDOUT}",
            f"printf %s {PARTIAL_STDERR} >&2",
        ]
        if announce:
            steps.append("echo go > ready")
        steps.append("wait")
        return "\n".join(steps)

    def wait_until_running(self) -> None:
        (self.workspace / "ready").read_text(encoding="utf-8")

    def release_waiter(self) -> None:
        # A reader blocked in open() has a pending reader; with none, open
        # fails with ENXIO and there is nothing to release.
        with contextlib.suppress(OSError):
            os.close(os.open(self.workspace / "ready", os.O_WRONLY | os.O_NONBLOCK))

    def processes_gone(self) -> bool:
        """Whether every process of the command exits once its stop was delivered.

        ``execute`` returns when the stopped command's leader has exited and been
        reaped. A descendant it left behind exits asynchronously after its signal,
        so an instantaneous check can see it mid-exit. A live writer makes the read
        end unreadable; waiting for end-of-file on ``alive`` observes the exit itself.
        The bound only turns a process that survives its stop into a failure instead
        of a hang. A command that never started has no writer and reads end-of-file
        at once, which a wait could never see.
        """
        while True:
            try:
                # b"" is end-of-file: no writer is left.
                return os.read(self._alive_reader, 1) == b""
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise
            readable, _, _ = select.select([self._alive_reader], [], [], _EXIT_BOUND_SECONDS)
            if not readable:
                return False


class TestLocalShellRunner(CommandRunnerContract):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> Iterator[Harness]:
        harness = _ShellHarness(LocalShellRunner(tmp_path, max_output_chars=CAP), tmp_path)
        try:
            yield harness
        finally:
            harness.close()


class TestDockerSandboxOnFakeDaemon(CommandRunnerContract):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> Iterator[Harness]:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (tmp_path / "engine-state").mkdir()
        sandbox = DockerSandbox(
            host_workspace=str(workspace),
            image="fake-image",
            max_output_bytes=CAP,
            agent_uid=os.getuid(),
            agent_gid=os.getgid(),
            docker=FakeDockerEngine(
                tmp_path / "engine-state", agent_ids=(os.getuid(), os.getgid())
            ),
        )
        sandbox.start()
        harness = _ShellHarness(sandbox, workspace)
        try:
            yield harness
        finally:
            harness.close()
            sandbox.stop()


@pytest.mark.e2e
@pytest.mark.real_contract
@pytest.mark.skipif(
    os.environ.get(_E2E_DOCKER) != "1" or shutil.which("docker") is None,
    reason=f"set {_E2E_DOCKER}=1 with docker on PATH to run against a real daemon",
)
class TestDockerSandboxOnRealDaemon(CommandRunnerContract):
    @pytest.fixture
    def harness(self, tmp_path: Path) -> Iterator[Harness]:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = DockerSandbox(
            host_workspace=str(workspace),
            image=agent_image(_DOCKER_BASE_IMAGE, timeout=_DOCKER_BUILD_TIMEOUT_S),
            max_output_bytes=CAP,
        )
        sandbox.start()
        harness = _ShellHarness(sandbox, workspace)
        try:
            yield harness
        finally:
            harness.close()
            sandbox.stop()
