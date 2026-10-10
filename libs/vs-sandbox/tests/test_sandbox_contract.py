"""``FakeCommandRunner`` held to the ``CommandRunner.execute`` contract, on simulated threads.

The cases are shared with the implementations that spawn real processes, which run
them from ``tests/e2e/test_command_runner_contract_real.py``. The fake starts no
process, so its hang waits on events of a ``SimThreads``: a cancel races nothing and a
stuck case is reported as a deadlock instead of hanging the test process.
"""

from __future__ import annotations

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

from vs_sandbox.api.testing import FakeCommandRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api.testing import Sim, SimThreads


@dataclass
class _FakeHarness:
    """A harness for ``FakeCommandRunner``, which scripts what a process would have done."""

    sandbox: FakeCommandRunner
    threads: SimThreads
    _count: int = field(default=0, repr=False)

    def run[T](self, program: Callable[[], T]) -> T:
        return self.threads.run(program)

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
        self.sandbox.script_hang(key, stdout=PARTIAL_STDOUT, stderr=PARTIAL_STDERR)
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


class TestFakeCommandRunner(CommandRunnerContract):
    @pytest.fixture
    def harness(self, sim: Sim) -> Harness:
        threads = sim.threads()
        return _FakeHarness(FakeCommandRunner(max_output_chars=CAP, threads=threads), threads)
