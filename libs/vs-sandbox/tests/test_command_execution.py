"""Properties of the result mapping every sandbox's ``execute`` shares.

Driven through ``FakeSandbox``, which builds its results with the same mapping
the real sandboxes use, so any output size and cap is checked without a
subprocess.
"""

from __future__ import annotations

import threading

from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.testing import FakeSandbox

_NOTICE_ROOM = 200
_TIMEOUT_STATUS = 124
_streams = st.text(max_size=300)


@given(
    stdout=_streams,
    stderr=_streams,
    returncode=st.integers(min_value=0, max_value=255),
    cap=st.integers(min_value=0, max_value=400),
)
def test_a_finished_command_maps_to_a_bounded_result_whose_streams_compose_the_output(
    stdout: str, stderr: str, returncode: int, cap: int
) -> None:
    sandbox = FakeSandbox(max_output_chars=cap)
    sandbox.script_process("cmd", stdout=stdout, stderr=stderr, returncode=returncode)

    result = sandbox.execute("cmd")

    assert result.output == result.stdout + result.stderr
    assert len(result.output) <= cap
    assert result.exit_code == returncode
    assert not result.cancelled
    assert result.truncated == (len(stdout) + len(stderr) > cap)
    if not result.truncated:
        assert (result.stdout, result.stderr) == (stdout, stderr)


@given(
    stdout=_streams,
    stderr=_streams,
    timeout=st.integers(min_value=1, max_value=3600),
    cap=st.integers(min_value=0, max_value=400),
)
def test_a_timed_out_command_reports_124_within_the_cap(
    stdout: str, stderr: str, timeout: int, cap: int
) -> None:
    sandbox = FakeSandbox(max_output_chars=cap)
    sandbox.script_hang("cmd", stdout=stdout, stderr=stderr)

    result = sandbox.execute("cmd", timeout=timeout)

    assert result.exit_code == _TIMEOUT_STATUS
    assert not result.cancelled
    assert result.output == result.stdout + result.stderr
    assert len(result.output) <= cap


@given(
    stdout=_streams,
    stderr=_streams,
    timeout=st.integers(min_value=1, max_value=3600),
)
def test_a_stopped_command_within_the_cap_keeps_partial_output_and_ends_with_its_notice(
    stdout: str, stderr: str, timeout: int
) -> None:
    sandbox = FakeSandbox(max_output_chars=len(stdout) + len(stderr) + _NOTICE_ROOM)
    sandbox.script_hang("cmd", stdout=stdout, stderr=stderr)
    cancel = threading.Event()
    cancel.set()

    timed_out = sandbox.execute("cmd", timeout=timeout)
    cancelled = sandbox.execute("cmd", timeout=timeout, cancel=cancel)

    assert not timed_out.truncated
    assert timed_out.stdout == stdout
    assert timed_out.stderr.startswith(stderr)
    assert timed_out.stderr.endswith(f"timed out after {timeout} seconds.\n")
    assert cancelled.cancelled
    assert cancelled.stderr.endswith("was cancelled.\n")
