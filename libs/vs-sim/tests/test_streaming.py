"""The real streaming command: stderr lines arrive in order and the result matches ``subprocess.run``."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import SubprocessStreamingCommand

if TYPE_CHECKING:
    from pathlib import Path


def _python(code: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code, *args)


def test_stderr_lines_stream_in_order_and_the_result_keeps_both_streams(tmp_path: Path) -> None:
    lines: list[str] = []
    script = (
        "import sys\n"
        "for i in range(3):\n"
        "    print(f'step {i}', file=sys.stderr, flush=True)\n"
        "sys.stdout.write('done')\n"
        "sys.exit(int(sys.argv[1]))\n"
    )

    result = SubprocessStreamingCommand().run(
        _python(script, "3"), cwd=tmp_path, timeout_seconds=30, on_stderr_line=lines.append
    )

    assert lines == ["step 0", "step 1", "step 2"]
    assert (result.returncode, result.stdout, result.stderr) == (
        3,
        "done",
        "step 0\nstep 1\nstep 2\n",
    )


def test_a_missing_program_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        SubprocessStreamingCommand().run(
            ("/nonexistent/vs-sim-no-such-program",),
            cwd=tmp_path,
            timeout_seconds=30,
            on_stderr_line=lambda _line: None,
        )


def test_a_command_that_outlives_the_timeout_is_killed_and_raises(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        SubprocessStreamingCommand().run(
            _python("import signal; signal.pause()"),
            cwd=tmp_path,
            timeout_seconds=0.2,
            on_stderr_line=lambda _line: None,
        )
