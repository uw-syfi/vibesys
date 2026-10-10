"""``SubprocessCommandRunner`` against a real child process.

The unit tests in ``tests/vibesys/skypilot/test_runner.py`` drive ``SkyPilotJobRunner`` through
a scripted command runner. What only a real process shows is here: both output pipes are
drained to completion while a sink callback fails, and the failure then propagates.
"""

from __future__ import annotations

import sys

import pytest

from vs_sandbox.api.skypilot import SubprocessCommandRunner


def test_subprocess_runner_drains_pipes_and_propagates_sink_failure() -> None:
    calls = 0

    def broken_sink(_: str) -> None:
        nonlocal calls
        calls += 1
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        SubprocessCommandRunner().run(
            (
                sys.executable,
                "-c",
                "import sys; [print(i) for i in range(1000)]; print('err', file=sys.stderr)",
            ),
            stdout_sink=broken_sink,
        )

    assert calls == 1000


def test_subprocess_runner_captures_both_streams_and_the_exit_status() -> None:
    result = SubprocessCommandRunner().run(
        (
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        )
    )

    assert (result.returncode, result.stdout, result.stderr) == (3, "out\n", "err\n")
