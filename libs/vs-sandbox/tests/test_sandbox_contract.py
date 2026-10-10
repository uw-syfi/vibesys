"""The ``CommandRunner.execute`` contract, held against every implementation.

The contract (also documented in :mod:`vs_sandbox.command_execution`):

* An empty command is rejected without running: exit code 1, the reason on
  ``stderr``. A non-positive timeout raises ``ValueError``.
* ``output == stdout + stderr``, and ``len(output)`` never exceeds the cap.
  Past the cap ``truncated`` is true and the output carries the truncation
  marker.
* A normal exit reports the command's status; a signalled command reports
  ``128 + N``, never a negative number.
* A timeout stops the command's whole process tree, keeps the output written
  before the stop, ends ``stderr`` with the timeout notice, and reports exit
  code 124 (GNU ``timeout``'s status; the benchmark failure classification
  reads it as a stage timeout, the candidate exceeding its budget).
* A cancel stops the whole process tree the same way, keeps the partial
  output, and returns ``cancelled=True``.

Implementations: ``LocalShellRunner`` (real subprocesses), ``DockerSandbox``
over ``FakeDockerEngine`` (the real sandbox class, a fake daemon), ``DockerSandbox``
over a real daemon (opt in: ``VIBESYS_E2E_DOCKER=1`` with ``docker`` on PATH),
and ``FakeCommandRunner`` (in-memory). Each implementation supplies a ``_Harness``
that turns a behavior ("print this, then exit 7", "hang with a child") into a
command for its sandbox; the cases assert only the contract. To hold a new
implementation to it, add a ``_Harness`` factory to ``_IMPLEMENTATIONS``.

No case depends on a wall-clock race. A cancel is sent only after the command
reports it is running (a named pipe the command writes to), and the process
tree is checked through a pipe whose writers are the command's processes: its
end-of-file means every one of them is gone. The only elapsed time is the
one-second timeout of the timeout case, whose command can never finish before
it.
"""

from __future__ import annotations

import contextlib
import errno
import os
import select
import shlex
import shutil
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import pytest
from tests.support.bounded_waits import join_or_fail

from vs_agent.api.images import agent_image
from vs_sandbox.api import CommandResult, CommandRunner, DockerSandbox, LocalShellRunner
from vs_sandbox.api.testing import FakeCommandRunner, FakeDockerEngine

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_CAP = 200
_TIMEOUT_SECONDS = 2
#: Upper bound on a stopped process exiting; reached only when a stop failed.
_EXIT_BOUND_SECONDS = 30.0
_PARTIAL_STDOUT = "partial-out"
_PARTIAL_STDERR = "partial-err"
_TRUNCATION_MARKER = "...[truncated]..."
_E2E_DOCKER = "VIBESYS_E2E_DOCKER"
_DOCKER_BASE_IMAGE = "python:3.12-bookworm"
_DOCKER_BUILD_TIMEOUT_S = 600.0
_SIGKILL_STATUS = 137
_SIGTERM_STATUS = 143
_NONZERO = 7


class _Harness(Protocol):
    """How one implementation runs the behaviors the contract probes."""

    @property
    def sandbox(self) -> CommandRunner:
        """Return the sandbox under test."""
        ...

    def emit(self, stdout: str, stderr: str, exit_code: int) -> str:
        """Return a command that prints *stdout*, *stderr*, then exits."""
        ...

    def killed_by(self, signal_number: int) -> str:
        """Return a command whose own process dies of *signal_number*."""
        ...

    def hang(self, *, announce: bool) -> str:
        """Return a command that prints partial output, starts a child, and blocks.

        With *announce*, the command reports it is running before it blocks.
        """
        ...

    def wait_until_running(self) -> None:
        """Block until an announcing hang command is running."""
        ...

    def release_waiter(self) -> None:
        """Unblock :meth:`wait_until_running` if the command can no longer announce."""
        ...

    def processes_gone(self) -> bool:
        """Report whether every process the hang command started has exited."""
        ...


@dataclass
class _ShellHarness:
    """A harness for a sandbox that runs real shell commands in ``workspace``."""

    sandbox: CommandRunner
    workspace: Path
    _alive_reader: int = -1

    def __post_init__(self) -> None:
        os.mkfifo(self.workspace / "ready")
        os.mkfifo(self.workspace / "alive")
        # The command's processes hold write ends of `alive`; this read end
        # sees end-of-file only once all of them have exited.
        self._alive_reader = os.open(self.workspace / "alive", os.O_RDONLY | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self._alive_reader)

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
            f"printf %s {_PARTIAL_STDOUT}",
            f"printf %s {_PARTIAL_STDERR} >&2",
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


@dataclass
class _FakeHarness:
    """A harness for ``FakeCommandRunner``, which scripts what a process would have done."""

    sandbox: FakeCommandRunner
    _count: int = field(default=0, repr=False)

    def _key(self, label: str) -> str:
        self._count += 1
        return f"{label}-{self._count}"

    def emit(self, stdout: str, stderr: str, exit_code: int) -> str:
        key = self._key("emit")
        self.sandbox.script_process(key, stdout=stdout, stderr=stderr, returncode=exit_code)
        return key

    def killed_by(self, signal_number: int) -> str:
        key = self._key("killed")
        self.sandbox.script_process(key, returncode=128 + signal_number)
        return key

    def hang(self, *, announce: bool) -> str:
        del announce
        key = self._key("hang")
        self.sandbox.script_hang(key, stdout=_PARTIAL_STDOUT, stderr=_PARTIAL_STDERR)
        return key

    def wait_until_running(self) -> None:
        """Block until the scripted hang has started, so a cancel cannot precede it."""
        self.sandbox.wait_until_hanging()

    def release_waiter(self) -> None:
        """Unblock :meth:`wait_until_running` for a command that ended without hanging."""
        self.sandbox.release_hanging_waiters()

    def processes_gone(self) -> bool:
        """The fake starts no processes."""
        return True


def _local(tmp_path: Path) -> Iterator[_Harness]:
    harness = _ShellHarness(LocalShellRunner(tmp_path, max_output_chars=_CAP), tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _docker_on_fake_daemon(tmp_path: Path) -> Iterator[_Harness]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image="fake-image",
        max_output_bytes=_CAP,
        agent_uid=os.getuid(),
        agent_gid=os.getgid(),
        docker=FakeDockerEngine(tmp_path / "engine-state", agent_ids=(os.getuid(), os.getgid())),
    )
    (tmp_path / "engine-state").mkdir()
    sandbox.start()
    harness = _ShellHarness(sandbox, workspace)
    try:
        yield harness
    finally:
        harness.close()
        sandbox.stop()


def _docker_on_real_daemon(tmp_path: Path) -> Iterator[_Harness]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=agent_image(_DOCKER_BASE_IMAGE, timeout=_DOCKER_BUILD_TIMEOUT_S),
        max_output_bytes=_CAP,
    )
    sandbox.start()
    harness = _ShellHarness(sandbox, workspace)
    try:
        yield harness
    finally:
        harness.close()
        sandbox.stop()


def _fake(tmp_path: Path) -> Iterator[_Harness]:
    del tmp_path
    yield _FakeHarness(FakeCommandRunner(max_output_chars=_CAP))


_real_docker_unavailable = os.environ.get(_E2E_DOCKER) != "1" or shutil.which("docker") is None

_IMPLEMENTATIONS = [
    pytest.param(_local, id="local"),
    pytest.param(_docker_on_fake_daemon, id="docker-fake-daemon"),
    pytest.param(
        _docker_on_real_daemon,
        id="docker-real-daemon",
        marks=[
            pytest.mark.e2e,
            pytest.mark.real_contract,
            pytest.mark.skipif(
                _real_docker_unavailable,
                reason=f"set {_E2E_DOCKER}=1 with docker on PATH to run against a real daemon",
            ),
        ],
    ),
    pytest.param(_fake, id="fake"),
]


@pytest.fixture(params=_IMPLEMENTATIONS)
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[_Harness]:
    """One ready implementation, torn down after the case."""
    make: Callable[[Path], Iterator[_Harness]] = request.param
    yield from make(tmp_path)


class _Execution:
    """One ``execute`` call running on its own thread."""

    def __init__(
        self, harness: _Harness, command: str, *, timeout: int, cancel: threading.Event
    ) -> None:
        self._result: CommandResult | None = None
        self._error: BaseException | None = None
        self._harness = harness

        def run() -> None:
            try:
                self._result = harness.sandbox.execute(command, timeout=timeout, cancel=cancel)
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-731014 [BLE001]; the case re-raises whatever the worker raised.
                self._error = error
            finally:
                # A command that ended without announcing must not leave the
                # case blocked waiting for the announcement.
                harness.release_waiter()

        self._thread = threading.Thread(target=run)
        self._thread.start()

    def result(self) -> CommandResult:
        join_or_fail(self._thread)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _assert_streams_compose_output(result: CommandResult) -> None:
    assert result.output == result.stdout + result.stderr


def test_id_is_a_nonempty_stable_string(harness: _Harness) -> None:
    assert isinstance(harness.sandbox.id, str)
    assert harness.sandbox.id
    assert harness.sandbox.id == harness.sandbox.id


def test_streams_and_status_of_a_normal_exit_are_kept_apart(harness: _Harness) -> None:
    result = harness.sandbox.execute(harness.emit("hello\n", "warn\n", 0))

    assert isinstance(result, CommandResult)
    assert (result.exit_code, result.stdout, result.stderr) == (0, "hello\n", "warn\n")
    assert not result.truncated
    assert not result.cancelled
    _assert_streams_compose_output(result)


def test_a_nonzero_status_is_reported_unchanged(harness: _Harness) -> None:
    result = harness.sandbox.execute(harness.emit("out", "err", _NONZERO))

    assert result.exit_code == _NONZERO
    assert (result.stdout, result.stderr) == ("out", "err")
    _assert_streams_compose_output(result)


@pytest.mark.parametrize(("signal_number", "status"), [(9, _SIGKILL_STATUS), (15, _SIGTERM_STATUS)])
def test_a_signalled_command_reports_128_plus_the_signal(
    harness: _Harness, signal_number: int, status: int
) -> None:
    result = harness.sandbox.execute(harness.killed_by(signal_number))

    assert result.exit_code == status
    assert not result.cancelled
    _assert_streams_compose_output(result)


def test_an_empty_command_is_rejected_with_exit_code_one(harness: _Harness) -> None:
    result = harness.sandbox.execute("")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr
    assert not result.cancelled
    _assert_streams_compose_output(result)


@pytest.mark.parametrize("timeout", [0, -1])
def test_a_non_positive_timeout_is_rejected(harness: _Harness, timeout: int) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        harness.sandbox.execute(harness.emit("x", "", 0), timeout=timeout)


def test_output_within_the_cap_is_not_truncated(harness: _Harness) -> None:
    result = harness.sandbox.execute(harness.emit("a" * (_CAP // 2), "b" * (_CAP // 2), 0))

    assert not result.truncated
    assert (len(result.stdout), len(result.stderr)) == (_CAP // 2, _CAP // 2)
    _assert_streams_compose_output(result)


def test_output_past_the_cap_is_cut_marked_and_flagged(harness: _Harness) -> None:
    result = harness.sandbox.execute(harness.emit("a" * (_CAP * 3), "", 0))

    assert result.truncated
    assert len(result.output) <= _CAP
    assert _TRUNCATION_MARKER in result.output
    assert result.stdout.startswith("a" * 10)
    _assert_streams_compose_output(result)


def test_a_failed_command_keeps_the_end_of_its_diagnostics_past_the_cap(
    harness: _Harness,
) -> None:
    diagnostics = "".join(f"line {number}\n" for number in range(_CAP))
    result = harness.sandbox.execute(harness.emit("", diagnostics, 1))

    assert result.truncated
    assert len(result.output) <= _CAP
    assert result.stderr.endswith(diagnostics[-20:])
    _assert_streams_compose_output(result)


def test_an_unset_cancel_event_leaves_the_command_alone(harness: _Harness) -> None:
    result = harness.sandbox.execute(harness.emit("done", "", 0), cancel=threading.Event())

    assert result.exit_code == 0
    assert not result.cancelled
    assert result.stdout == "done"


def test_a_timeout_stops_the_process_tree_and_keeps_partial_output(harness: _Harness) -> None:
    result = harness.sandbox.execute(harness.hang(announce=False), timeout=_TIMEOUT_SECONDS)

    assert result.exit_code == 124
    assert not result.cancelled
    assert result.stdout == _PARTIAL_STDOUT
    assert result.stderr.startswith(_PARTIAL_STDERR)
    assert result.stderr.endswith(f"timed out after {_TIMEOUT_SECONDS} seconds.\n")
    _assert_streams_compose_output(result)
    assert harness.processes_gone()


def test_a_cancel_stops_the_process_tree_and_keeps_partial_output(harness: _Harness) -> None:
    cancel = threading.Event()
    execution = _Execution(harness, harness.hang(announce=True), timeout=3600, cancel=cancel)
    harness.wait_until_running()
    cancel.set()
    result = execution.result()

    assert result.cancelled
    assert isinstance(result.exit_code, int)
    assert result.stdout == _PARTIAL_STDOUT
    assert result.stderr.startswith(_PARTIAL_STDERR)
    _assert_streams_compose_output(result)
    assert harness.processes_gone()


def test_a_cancel_set_before_the_call_cancels_it(harness: _Harness) -> None:
    cancel = threading.Event()
    cancel.set()

    result = harness.sandbox.execute(harness.hang(announce=False), timeout=3600, cancel=cancel)

    assert result.cancelled
    _assert_streams_compose_output(result)
    assert harness.processes_gone()
