"""Git and the GitHub CLI for tests: real Git for repositories on disk, and no GitHub at all."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from vs_github.api import GitHubCLI
from vs_project.api import Git, GitTracker, open_git_repository
from vs_sim.api import OsProcesses
from vs_sim.api.testing import FakeProcesses

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from vs_project.api import GitTrackerEvents
    from vs_sim.api import ProcessSpec

REAL_PROCESSES = OsProcesses()
"""Starts real processes, for a test that drives real Git on disk."""

REAL_GIT = Git(REAL_PROCESSES)
"""Starts real Git processes, for a test whose repository is on disk."""


def _refuse(spec: ProcessSpec) -> Never:
    message = f"this test runs no such command, but ran {' '.join(spec.argv)}"
    raise AssertionError(message)


def _no_time(seconds: float) -> Never:
    message = f"a refused command took {seconds:g} s"
    raise AssertionError(message)


NO_GITHUB = GitHubCLI(FakeProcesses(_refuse, pass_time=_no_time))
"""A GitHub CLI for a run that never reaches GitHub; any command fails the test."""

NO_GIT = Git(FakeProcesses(_refuse, pass_time=_no_time))
"""Git for a test that never runs a Git command; any command fails the test."""


def real_git_tracker(  # noqa: PLR0913  # lint-waiver: LW-261403 [PLR0913]; mirrors GitTracker's keywords one to one.
    # > A settings object would rename GitTracker's own keywords for a test helper whose only
    # > job is to add the real repository to them.
    root: Path,
    *,
    run_id: str,
    events: GitTrackerEvents,
    excluded_dirs: Iterable[str] = (),
    excluded_files: Iterable[str] = (),
    trusted_input_paths: Iterable[str | Path] = (),
) -> GitTracker:
    """A ``GitTracker`` over the configured repository implementation, on real Git."""
    return GitTracker(
        root,
        run_id=run_id,
        events=events,
        excluded_dirs=excluded_dirs,
        excluded_files=excluded_files,
        trusted_input_paths=trusted_input_paths,
        repository=open_git_repository(root, git=REAL_GIT, faults=events),
    )
