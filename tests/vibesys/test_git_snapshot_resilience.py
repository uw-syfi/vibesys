"""Canonical-project snapshot resilience tests."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vs_project.api import (
    CliGitRepository,
    GitFaultSink,
    GitTracker,
    NullGitTrackerEvents,
    Project,
    StagingError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _tracker(project: Path, *, excluded_dirs: set[str] | None = None) -> GitTracker:
    return GitTracker(
        project,
        run_id="test-run",
        events=NullGitTrackerEvents(),
        excluded_dirs=excluded_dirs or (),
    )


def test_project_excludes_runtime_and_compiled_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "code.py").write_text("VALUE = 1\n")
    tracker = _tracker(project, excluded_dirs={"_mounts", "target"})

    tracker.init(existing=False)

    excludes = (project / ".git" / "info" / "exclude").read_text().splitlines()
    assert Project.open(project).state.git_integration("test-run").local_exclude_pattern in excludes
    assert "_mounts/" in excludes
    assert "target/" in excludes
    assert "*.neff" in excludes
    assert "*.ntff" in excludes
    assert "neuron-compile-cache/" in excludes


def test_unreadable_scan_skips_excluded_runtime_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    ignored = project / ".venv" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "large-package.so").write_text("cached")
    source = project / "src"
    source.mkdir()
    (source / "engine.py").write_text("pass\n")
    tracker = _tracker(project, excluded_dirs={".venv"})
    tracker.init(existing=False)

    checked: list[str] = []
    real_access = os.access

    def recording_access(path: Path, mode: int) -> bool:
        checked.append(os.fspath(path))
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", recording_access)
    tracker.snapshot("ignored runtime tree")

    assert any(path.endswith("src/engine.py") for path in checked)
    assert all(".venv" not in path for path in checked)


class _RepositoryRefusingFirstStage(CliGitRepository):
    """Report an unreadable file the host scan missed, on the first staging attempt only."""

    def __init__(self, root: Path, *, faults: GitFaultSink) -> None:
        super().__init__(root, faults=faults)
        self.refused_first_stage = False

    def stage_all(self, pathspecs: Sequence[str], *, force: bool = False) -> None:
        if not self.refused_first_stage and not force:
            self.refused_first_stage = True
            raise StagingError(
                ["git", "add", "-A"], 128, "permission denied", ["system_profile.json"]
            )
        super().stage_all(pathspecs, force=force)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_snapshot_excludes_unreadable_project_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("VALUE = 1\n")
    events = NullGitTrackerEvents()
    repository = _RepositoryRefusingFirstStage(project, faults=events)
    tracker = GitTracker(project, run_id="test-run", events=events, repository=repository)
    tracker.init(existing=False)

    code.write_text("VALUE = 2\n")
    unreadable = project / "system_profile.json"
    unreadable.write_text("{}")
    unreadable.chmod(0o000)
    real_access = os.access

    def report_readable(path: Path, mode: int) -> bool:
        return True if path == unreadable else real_access(path, mode)

    monkeypatch.setattr(os, "access", report_readable)
    try:
        tracker.snapshot("skip unreadable file")
        committed = run_test_command(
            ["git", "show", "--format=", "--name-only", "HEAD"],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert committed == ["code.py"]
        assert repository.refused_first_stage
        assert (
            "/system_profile.json"
            in (project / ".git" / "info" / "exclude").read_text().splitlines()
        )
    finally:
        unreadable.chmod(0o644)


def test_reinitializing_git_at_project_root_does_not_change_tracking_repository(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("VALUE = 1\n")
    tracker = _tracker(project)
    tracker.init(existing=False)

    run_test_command(["git", "init", "-q"], cwd=project, check=True)
    code.write_text("VALUE = 2\n")
    tracker.snapshot("round-1")

    subject = run_test_command(
        ["git", "log", "-1", "--format=%s"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "round-1"
