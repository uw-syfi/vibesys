"""``PopenProcess`` against real processes: the same contract the simulated Fake passes.

The unit tests in ``libs/vs-sandbox/tests/test_stoppable_process.py`` run the stop policy
on a scripted process and a virtual clock. What only a real process and real signals show
is here: a shell's exit status and streams, ``SIGTERM`` and ``SIGKILL`` delivered to a
process group, and a process that ignores ``SIGTERM``.
"""

from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.testing import ProcessHarness, StoppableProcessContract

# test-isolation: PopenProcess and start_process_group are the real adapter under contract test.
from vs_sandbox.process_execution import PopenProcess, start_process_group

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any


def _start(script: str) -> PopenProcess:
    return PopenProcess(start_process_group(("/bin/sh", "-c", script), env=None, cwd=None))


class TestPopenProcess(StoppableProcessContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._ready = tmp_path / "ready"
        os.mkfifo(self._ready)

    def harness(self) -> ProcessHarness:
        def run(body: Callable[[], Any]) -> Any:  # noqa: ANN401  # lint-waiver: LW-731017 [ANN401]; the contract's case bodies return nothing in particular.
            return body()

        def ignores_term() -> PopenProcess:
            # The trap is installed before the shell announces itself, so a SIGTERM sent
            # after this returns is ignored rather than racing the shell's startup.
            process = _start(
                f"trap '' TERM; echo up > {shlex.quote(str(self._ready))}; "
                "while :; do sleep 1; done"
            )
            self._ready.read_text(encoding="utf-8")
            return process

        return ProcessHarness(
            run=run,
            exits_with=lambda out, err, code: _start(
                f"printf %s {shlex.quote(out)}; printf %s {shlex.quote(err)} >&2; exit {code}"
            ),
            runs_until_signalled=lambda: _start("exec sleep 1000"),
            ignores_term=ignores_term,
        )
