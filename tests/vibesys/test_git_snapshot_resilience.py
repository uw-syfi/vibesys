"""Canonical-project snapshot resilience tests."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vibesys.run import GitTracker
from vibesys.run.git_events import NullGitTrackerEvents
from vs_project.api import Project

if TYPE_CHECKING:
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_snapshot_excludes_unreadable_project_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    code = project / "code.py"
    code.write_text("VALUE = 1\n")
    tracker = _tracker(project)
    tracker.init(existing=False)

    code.write_text("VALUE = 2\n")
    unreadable = project / "system_profile.json"
    unreadable.write_text("{}")
    unreadable.chmod(0o000)
    real_access = os.access
    real_run = tracker.run

    def report_permission_failure_once(
        command: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if command[:3] == ["git", "add", "-A"] and not failed_add[0]:
            failed_add[0] = True
            return subprocess.CompletedProcess(
                command,
                128,
                stdout=b"",
                stderr=(
                    b'error: open("system_profile.json"): Permission denied\n'
                    b"fatal: adding files failed\n"
                ),
            )
        return real_run(command, check=check, env=env, timeout=timeout)

    failed_add = [False]

    def report_readable(path: Path, mode: int) -> bool:
        return True if path == unreadable else real_access(path, mode)

    monkeypatch.setattr(os, "access", report_readable)
    monkeypatch.setattr(tracker, "run", report_permission_failure_once)
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
        assert failed_add[0]
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
