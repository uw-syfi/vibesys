"""Admission is separate from the writer lock, and releases on every exit."""

from pathlib import Path

import pytest

from vs_runlog.api import (
    FileRunLogWriter,
    RunLaunchLockedError,
    assert_run_unlocked,
    exclusive_run_launch,
)
from vs_sim.api.testing import ManualClock, SimThreads


def interrupt_launch(path: Path) -> None:
    with exclusive_run_launch(path):
        raise ArithmeticError


def test_launch_lock_is_exclusive_independent_and_released_on_exception(tmp_path: Path) -> None:
    path = tmp_path / "run.log"
    with exclusive_run_launch(path):
        with pytest.raises(RunLaunchLockedError), exclusive_run_launch(path):
            pytest.fail("concurrent launcher acquired admission")
        assert_run_unlocked(path)
        with FileRunLogWriter(path, clock=ManualClock(), threads=SimThreads()):
            pass
    with pytest.raises(ArithmeticError):
        interrupt_launch(path)
    with exclusive_run_launch(path):
        assert_run_unlocked(path)
