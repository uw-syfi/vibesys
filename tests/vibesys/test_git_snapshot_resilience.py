"""Canonical-project snapshot resilience tests."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from vibesys.run import GitTracker
from vibesys.run.git_events import NullGitTrackerEvents
from vs_project import Project

if TYPE_CHECKING:
    from pathlib import Path


def _tracker(project: Path, *, excluded_dirs: set[str] | None = None) -> GitTracker:
    return GitTracker(
        project,
        run_id="test-run",
        events=NullGitTrackerEvents(),
        excluded_dirs=excluded_dirs or (),
    )


def _staged(project: Path) -> list[str]:
    return subprocess.run(
        ["git", "diff", "--cached", "--name-only"],  # noqa: S607
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def test_project_excludes_runtime_and_compiled_artifacts(tmp_path):  # noqa: ANN001, ANN201
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


def test_unreadable_git_error_parser_accepts_both_message_forms():  # noqa: ANN201
    stderr = (
        'error: open("system_profile.json"): Permission denied\n'
        "error: unable to index file 'sub/ntrace.pb'\n"
        "fatal: adding files failed\n"
    )

    assert GitTracker._unreadable_from_stderr(stderr) == [  # noqa: SLF001
        "system_profile.json",
        "sub/ntrace.pb",
    ]


def test_unreadable_scan_skips_excluded_runtime_trees(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    project = tmp_path / "project"
    ignored = project / ".venv" / "lib"
    ignored.mkdir(parents=True)
    (ignored / "large-package.so").write_text("cached")
    source = project / "src"
    source.mkdir()
    (source / "engine.py").write_text("pass\n")

    checked: list[str] = []
    real_access = os.access

    def recording_access(path, mode):  # noqa: ANN001, ANN202
        checked.append(os.fspath(path))
        return real_access(path, mode)

    monkeypatch.setattr(os, "access", recording_access)
    tracker = _tracker(project, excluded_dirs={".venv"})

    assert tracker._collect_unreadable() == []  # noqa: SLF001
    assert any(path.endswith("src/engine.py") for path in checked)
    assert all(".venv" not in path for path in checked)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_snapshot_excludes_unreadable_project_file(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("VALUE = 1\n")
    tracker = _tracker(project)
    tracker.init(existing=False)

    code.write_text("VALUE = 2\n")
    unreadable = project / "system_profile.json"
    unreadable.write_text("{}")
    os.chmod(unreadable, 0o000)  # noqa: PTH101
    try:
        tracker._add_all()  # noqa: SLF001

        assert _staged(project) == ["code.py"]
        assert (
            "/system_profile.json"
            in (project / ".git" / "info" / "exclude").read_text().splitlines()
        )
    finally:
        os.chmod(unreadable, 0o644)  # noqa: PTH101


def test_reinitializing_git_at_project_root_does_not_change_tracking_repository(tmp_path):  # noqa: ANN001, ANN201
    project = tmp_path / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("VALUE = 1\n")
    tracker = _tracker(project)
    tracker.init(existing=False)

    subprocess.run(["git", "init", "-q"], cwd=project, check=True)  # noqa: S607
    code.write_text("VALUE = 2\n")
    tracker.snapshot("round-1")

    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],  # noqa: S607
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "round-1"
