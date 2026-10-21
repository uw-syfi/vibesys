"""Host discovery uses the same configured state root as every other Project path."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from vs_project.api import Project, ProjectStateError


def test_publish_replaces_stale_link_and_keeps_transport_beside_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_home = tmp_path / "state-home"
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(state_home))
    first = tmp_path / "project-a"
    second = tmp_path / "project-b"
    first.mkdir()
    second.mkdir()
    link = Project.publish_run_for(first, "run-1")
    assert link == state_home / "runs/run-1"
    assert link.is_symlink()
    assert link.resolve() == Project.log_directory_for(first, "run-1")
    inode = link.lstat().st_ino
    assert Project.publish_run_for(first, "run-1") == link
    assert link.lstat().st_ino == inode
    Project.publish_run_for(second, "run-1")
    assert link.resolve() == Project.log_directory_for(second, "run-1")
    assert sorted(path.name for path in link.parent.iterdir()) == ["run-1"]
    assert (
        Project.run_log_path_for(first, "run-1").parent
        == Project.event_journal_path_for(first, "run-1").parent
    )


def test_publish_replaces_dangling_link_and_rejects_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    link = Project.publish_run_for(project, "run-1")
    link.unlink()
    link.symlink_to(tmp_path / "missing")
    Project.publish_run_for(project, "run-1")
    assert link.resolve() == Project.log_directory_for(project, "run-1")
    link.unlink()
    link.write_bytes(b"keep")
    with pytest.raises(ProjectStateError, match="not a symlink"):
        Project.publish_run_for(project, "run-1")
    assert link.read_bytes() == b"keep"


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "/absolute"])
def test_index_rejects_unsafe_run_ids(
    tmp_path: Path, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    with pytest.raises(ProjectStateError, match="run"):
        Project.publish_run_for(tmp_path, run_id)
    assert not (tmp_path / "home/runs").exists()


def test_index_discovery_reports_every_entry_without_creating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(home))
    assert Project.indexed_run_directories() == ()
    assert not home.exists()
    index = home / "runs"
    index.mkdir(parents=True)
    (index / "bad").write_bytes(b"keep")
    (index / "dangling").symlink_to(home / "absent")
    (index / "directory").mkdir()
    assert Project.indexed_run_directories() == tuple(
        index / name for name in ("bad", "dangling", "directory")
    )
