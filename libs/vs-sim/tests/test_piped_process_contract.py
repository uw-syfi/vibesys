"""The real piped launcher and the scripted piped process pass the same ``PipedProcess`` contract.

The real harness's commands end themselves after ``HANG_GUARD_S``, writing a line no
case expects, so a group signal or an output cancel that does not arrive fails its
case instead of hanging it.
"""

from __future__ import annotations

import sys
from functools import partial
from typing import TYPE_CHECKING

from vs_sim.api import SubprocessPipedProcessLauncher
from vs_sim.api.testing import (
    HANG_GUARD_S,
    PipedProcessContract,
    PipedUnderTest,
    ScriptedPipedProcess,
    SimThreads,
)

if TYPE_CHECKING:
    from vs_sim.api import PipedProcess

_HANG_GUARD = (
    "import os, signal, sys\n"
    "def _outlived(*_):\n"
    "    sys.stdout.write('outlived the hang guard\\n'); sys.stdout.flush(); os._exit(2)\n"
    "signal.signal(signal.SIGALRM, _outlived)\n"
    f"signal.alarm({int(HANG_GUARD_S)})\n"
)
_WRITES_THEN_EXITS = (
    "import sys\n"
    "sys.stdout.write(sys.argv[1]); sys.stdout.flush()\n"
    "sys.stderr.write(sys.argv[2]); sys.stderr.flush()\n"
    "sys.exit(int(sys.argv[3]))\n"
)
_BLOCKS = _HANG_GUARD + "signal.pause()\n"
# The descendant writes ``ready`` only once the leader has exited, so a group signal
# sent after reading it cannot reach the leader. The leader's end of ``exited`` closes
# when the leader exits, after its exit status is fixed, so the descendant's read of
# the other end returns end of file at that point and no earlier.
_DESCENDANT_HOLDS_OUTPUT = (
    "import os\n"
    "exited, leader_alive = os.pipe()\n"
    "if os.fork() == 0:\n"
    + "".join(f"    {line}\n" for line in _HANG_GUARD.splitlines())
    + "    os.close(leader_alive)\n"
    "    os.read(exited, 1)\n"
    "    sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
    "    signal.pause()\n"
    "    os._exit(1)\n"
    "os._exit(0)\n"
)


class TestSubprocessPipedProcess(PipedProcessContract):
    def piped_under_test(self) -> PipedUnderTest:
        launcher = SubprocessPipedProcessLauncher()

        def writes(stdout: str, stderr: str, status: int, *, merged: bool = False) -> PipedProcess:
            argv = (sys.executable, "-c", _WRITES_THEN_EXITS, stdout, stderr, str(status))
            return launcher.start(argv, merge_stderr=merged)

        return PipedUnderTest(
            writes_then_exits=writes,
            merged_writes_then_exits=partial(writes, merged=True),
            blocks_until_signalled=lambda: launcher.start((sys.executable, "-c", _BLOCKS)),
            descendant_holds_output=lambda: launcher.start(
                (sys.executable, "-c", _DESCENDANT_HOLDS_OUTPUT)
            ),
            drive=lambda case: case(),
        )


class TestScriptedPipedProcess(PipedProcessContract):
    def piped_under_test(self) -> PipedUnderTest:
        threads = SimThreads()

        def writes(
            stdout: str, stderr: str, status: int, *, merged: bool = False
        ) -> ScriptedPipedProcess:
            process = ScriptedPipedProcess(threads, merge_stderr=merged)
            process.write(stdout, stderr)
            process.exit(status)
            return process

        def descendant_holds_output() -> ScriptedPipedProcess:
            process = ScriptedPipedProcess(threads)
            process.write("ready\n")
            process.exit(0, close_output=False)
            return process

        return PipedUnderTest(
            writes_then_exits=writes,
            merged_writes_then_exits=partial(writes, merged=True),
            blocks_until_signalled=lambda: ScriptedPipedProcess(threads),
            descendant_holds_output=descendant_holds_output,
            drive=threads.run,
        )
