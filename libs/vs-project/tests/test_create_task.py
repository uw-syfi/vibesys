from __future__ import annotations

from pathlib import Path

import pytest

from vs_project.api import InvalidTaskNameError, Project, TaskExistsError, UnsafeProjectPathError


def test_create_task_makes_the_tasks_root_and_returns_a_discoverable_task(tmp_path: Path) -> None:
    project = Project.open(tmp_path)

    task = project.create_task("serve", objective="Go faster.\n", manifest="version = 1\n")

    assert project.is_initialized()
    assert project.discover_tasks() == (task,)
    assert task.objective_path.read_text() == "Go faster.\n"
    assert task.manifest_path.read_text() == "version = 1\n"


def test_create_task_refuses_an_existing_name(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.create_task("serve", objective="a", manifest="b")

    with pytest.raises(TaskExistsError, match="serve"):
        project.create_task("serve", objective="c", manifest="d")
    assert project.select_task("serve").objective_path.read_text() == "a"


@pytest.mark.parametrize("name", ["Bad Name", "../escape", "", ".hidden"])
def test_create_task_rejects_names_that_are_not_one_task_directory(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(InvalidTaskNameError):
        Project.open(tmp_path).create_task(name, objective="a", manifest="b")
    assert not (tmp_path / ".vibesys").exists()


def test_create_task_removes_the_task_directory_when_a_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project.open(tmp_path)
    original_write_text = Path.write_text
    calls = 0
    disk_full = OSError("disk full")

    def flaky_write_text(self: Path, data: str, encoding: str | None = None) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise disk_full
        return original_write_text(self, data, encoding=encoding)

    # test-isolation: no in-memory Fake models a mid-write filesystem failure
    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    with pytest.raises(OSError, match="disk full"):
        project.create_task("serve", objective="a", manifest="b")

    assert not (tmp_path / ".vibesys" / "tasks" / "serve").exists()


def test_create_task_refuses_a_symlinked_configuration_root(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".vibesys").symlink_to(elsewhere)

    with pytest.raises(UnsafeProjectPathError):
        Project.open(project_root).create_task("serve", objective="a", manifest="b")
    assert list(elsewhere.iterdir()) == []
