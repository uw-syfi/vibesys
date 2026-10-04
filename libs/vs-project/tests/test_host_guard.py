"""The project boundary fences concurrent run hosts without another state file."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_project.api import Project, ProjectStateError

if TYPE_CHECKING:
    from pathlib import Path


def test_host_guard_rejects_competing_owner_and_reopens_after_exit(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    first = project.state
    second = Project.open(tmp_path).state
    with first.exclusive_run_host("test-run"):
        with (
            pytest.raises(ProjectStateError, match="host already owns"),
            second.exclusive_run_host("test-run"),
        ):
            pytest.fail("a second host must be refused")
        # A different run remains independent.
        with project.state.exclusive_run_host("other-run"):
            pass
    with second.exclusive_run_host("test-run"):
        pass


def test_host_guard_releases_after_owner_failure(tmp_path: Path) -> None:
    namespace = Project.open(tmp_path).state
    with (
        pytest.raises(RuntimeError, match="host crashed"),
        namespace.exclusive_run_host("test-run"),
    ):
        _crash()
    with namespace.exclusive_run_host("test-run"):
        pass


def _crash() -> None:
    message = "host crashed"
    raise RuntimeError(message)
