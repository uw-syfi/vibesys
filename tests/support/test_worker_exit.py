"""A dead xdist worker is reported with how it ended, and the session ends with it."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from tests.support.child_pytest import BARE_PYTEST, child_pythonpath
from tests.support.worker_exit import describe_exit, worker_returncode

if TYPE_CHECKING:
    import pytest

pytest_plugins = ["pytester"]

_REPO = Path(__file__).resolve().parents[2]
_KILLED = 9


@given(st.integers(min_value=1, max_value=255))
def test_a_negative_status_names_the_signal(signal_number: int) -> None:
    assert describe_exit(-signal_number) == f"killed by signal {signal_number}"


@given(st.integers(min_value=0, max_value=255))
def test_a_non_negative_status_names_the_exit_code(code: int) -> None:
    assert describe_exit(code) == f"exited with code {code}"


def test_a_worker_that_has_not_ended_says_so() -> None:
    assert "still running" in describe_exit(None)


class _KilledProcess:
    """A process killed without being waited for: its status settles only when polled."""

    def __init__(self, status: int) -> None:
        self.returncode: int | None = None
        self._status = status

    def poll(self) -> int | None:
        self.returncode = self._status
        return self.returncode


@given(st.integers(min_value=1, max_value=255))
def test_a_worker_killed_without_a_wait_is_reported_by_its_signal(signal_number: int) -> None:
    assert describe_exit(worker_returncode(_KilledProcess(-signal_number))) == (
        f"killed by signal {signal_number}"
    )


def test_a_worker_with_no_process_has_no_status() -> None:
    assert worker_returncode(None) is None


def test_a_killed_worker_is_reported_and_the_session_ends_without_waiting_for_its_group(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTHONPATH", child_pythonpath(_REPO))
    pytester.makepyfile(
        test_crash=f"""
import os
import pytest

@pytest.mark.xdist_group("owed")
def test_dies():
    os.closerange(3, 1024)  # the connection to the controller: it ends before the process
    sum(range(3_000_000))  # the process is still exiting when the controller sees the end
    os.kill(os.getpid(), {_KILLED})

@pytest.mark.xdist_group("owed")
def test_owed_after_the_death():
    pass

@pytest.mark.parametrize("index", range(6))
def test_ungrouped(index):
    pass
"""
    )

    result = pytester.runpytest(
        *BARE_PYTEST,
        "-p",
        "tests.support.worker_exit",
        "-n",
        "2",
        "--dist",
        "loadgroup",
        "--maxschedchunk=1",
        "--max-worker-restart=0",
        "test_crash.py",
    )

    result.stdout.fnmatch_lines(
        ["*crashed and worker restarting disabled*", f"*worker gw* killed by signal {_KILLED} *"]
    )
    assert result.ret != 0
