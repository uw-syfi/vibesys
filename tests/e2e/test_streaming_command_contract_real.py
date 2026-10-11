"""``SubprocessStreamingCommand`` against real processes: the contract its Fake passes.

``libs/vs-sim/tests/test_contracts.py`` runs the same cases on ``ScriptedStreamingCommand``.
What only a real process shows is here: a pipe read line by line while the command
runs, a real working directory, a missing program, and a kill at the timeout.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import SubprocessStreamingCommand
from vs_sim.api.testing import StreamingCommandContract, StreamingUnderTest

if TYPE_CHECKING:
    from pathlib import Path

_EXITS_WITH = (
    "import sys\n"
    "for line in sys.argv[3:]:\n"
    "    print(line, file=sys.stderr, flush=True)\n"
    "sys.stdout.write(sys.argv[2])\n"
    "sys.exit(int(sys.argv[1]))\n"
)


def _python(code: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code, *args)


class TestSubprocessStreamingCommand(StreamingCommandContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._cwd = tmp_path

    def streaming_under_test(self) -> StreamingUnderTest:
        return StreamingUnderTest(
            SubprocessStreamingCommand(),
            cwd=self._cwd,
            exits_with=lambda status, stdout, lines: _python(
                _EXITS_WITH, str(status), stdout, *lines
            ),
            prints_cwd=_python("import os, sys; sys.stdout.write(os.getcwd())"),
            blocks_forever=_python("import signal; signal.pause()"),
            missing_program=("/nonexistent/vs-sim-no-such-program",),
        )
