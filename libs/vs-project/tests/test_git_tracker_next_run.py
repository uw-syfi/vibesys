"""A run that ends or fails leaves the project a clean baseline for the next run.

Every run writes portable state below the project's VibeSys metadata directory that it
never commits (its store, artifacts, publications). The next run is a new process, so it
knows nothing of what the earlier one wrote. These tests reproduce that by running the
earlier run in one project directory and starting the next run in a copy of it: the copy
holds the same files, and no write of this process was ever recorded under its paths.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support import run_test_command
from tests.support.real_git import real_git_tracker

from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_project.api.testing import run_execution_record
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import GitTracker

# Each example runs real Git processes. A pull request draws a sample; the nightly depth
# (`--depth=nightly`) searches deeper.

_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_EARLIER_RUN = "earlier-run"
_NEXT_RUN = "next-run"

# Portable namespaces a run writes and does not commit, and file names inside them.
_NAMESPACES = st.sampled_from(["artifacts", "publications", "core-store", "control", "board"])
_FILE_NAMES = st.from_regex(r"[a-z][a-z0-9_-]{0,11}(\.json|\.lock)?", fullmatch=True)
_LEFTOVER = st.lists(st.tuples(_NAMESPACES, _FILE_NAMES), min_size=1, max_size=4)


def _git(root: Path, *args: str) -> str:
    return run_test_command(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    ).stdout.strip()


def _tracker(root: Path, run_id: str) -> GitTracker:
    return real_git_tracker(root, run_id=run_id, events=NullGitTrackerEvents())


def _user_repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "main.py")
    _git(root, "commit", "-q", "-m", "baseline")


def _earlier_run(root: Path, leftover: list[tuple[str, str]]) -> None:
    """Start a run as the product does, write its uncommitted state, and stop there."""
    tracker = _tracker(root, _EARLIER_RUN)
    tracker.init(existing=False)
    project = Project.open(root)
    project.state.create_project("Next run test", now=_NOW)
    assert tracker.trusted_input_baseline is not None
    project.state.create_run(
        project.state.new_run_manifest(
            "Next run test",
            run_id=_EARLIER_RUN,
            branch=tracker.project_branch,
            vibesys_version="test",
            trusted_input_baseline=tracker.trusted_input_baseline,
            run_environment=RunEnvironmentRecord(name="docker"),
            execution=run_execution_record(),
            orchestration=OrchestrationDescriptor(id="plain", config_version=1, options={}),
            now=_NOW,
        )
    )
    tracker.snapshot_with_framework_metadata(
        f"vibesys: initialize run {_EARLIER_RUN}",
        project.state.initialization_snapshot(_EARLIER_RUN),
    )
    for namespace, name in leftover:
        project.state.portable_namespace(_EARLIER_RUN, namespace).write_bytes(name, b"{}\n")
    project.state.state_store_namespace(_EARLIER_RUN).write_bytes("store.json", b"{}\n")


def _as_next_process(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination, symlinks=True)
    return destination


@settings(
    max_examples=examples(pr=4, nightly=30),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(leftover=_LEFTOVER)
def test_next_run_starts_after_an_earlier_run_left_its_uncommitted_state(
    tmp_path_factory: pytest.TempPathFactory, leftover: list[tuple[str, str]]
) -> None:
    base = tmp_path_factory.mktemp("next-run")
    _user_repository(base / "earlier")
    _earlier_run(base / "earlier", leftover)
    project = _as_next_process(base / "earlier", base / "next")

    tracker = _tracker(project, _NEXT_RUN)
    tracker.init(existing=False)

    assert tracker.baseline_changes() == []
    assert _git(project, "branch", "--show-current") == f"vibesys-runs/{_NEXT_RUN}"


@settings(
    max_examples=examples(pr=4, nightly=30),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(leftover=_LEFTOVER)
def test_next_run_still_refuses_user_changes_beside_earlier_run_state(
    tmp_path_factory: pytest.TempPathFactory, leftover: list[tuple[str, str]]
) -> None:
    base = tmp_path_factory.mktemp("next-run")
    _user_repository(base / "earlier")
    _earlier_run(base / "earlier", leftover)
    project = _as_next_process(base / "earlier", base / "next")
    (project / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    (project / "notes.txt").write_text("draft\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"must be clean.*: main\.py, notes\.txt$"):
        _tracker(project, _NEXT_RUN).init(existing=False)


def test_next_run_refuses_edited_committed_vibesys_metadata(tmp_path: Path) -> None:
    _user_repository(tmp_path / "earlier")
    _earlier_run(tmp_path / "earlier", [("artifacts", "index.json")])
    project = _as_next_process(tmp_path / "earlier", tmp_path / "next")
    metadata = Project.open(project).state.git_integration(_EARLIER_RUN).metadata_pathspec
    committed = _git(project, "ls-files", "--", metadata).splitlines()[0]
    edited = project / committed
    edited.write_text(edited.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(ValueError, match="must be clean") as raised:
        _tracker(project, _NEXT_RUN).init(existing=False)
    assert committed in str(raised.value)
