"""``vibesys tasks``: list a project's tasks for the desktop app's task picker."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from entrypoints.launcher import _headless_requested
from entrypoints.tasks import TaskList, TaskSummary, run

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_TASK_NAMES = st.from_regex(r"[a-z0-9][a-z0-9._-]{0,15}", fullmatch=True).filter(
    lambda name: name not in {".", ".."}
)


def _task(project: Path, name: str) -> None:
    task = project / ".vibesys" / "tasks" / name
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(names=st.sets(_TASK_NAMES, min_size=1, max_size=5))
def test_json_lists_every_task_by_name_in_order(
    tmp_path_factory: pytest.TempPathFactory, names: set[str]
) -> None:
    project = tmp_path_factory.mktemp("project")
    for name in names:
        _task(project, name)

    code, stdout, stderr = run([str(project), "--json"], cwd=project.parent)

    assert (code, stderr) == (0, "")
    listing = TaskList.model_validate_json(stdout)
    assert listing == TaskList(
        project_root=str(project.resolve()),
        tasks=tuple(TaskSummary(name=name) for name in sorted(names)),
    )


def test_project_defaults_to_the_working_directory(tmp_path: Path) -> None:
    _task(tmp_path, "spsc")

    code, stdout, _ = run(["--json"], cwd=tmp_path)

    assert code == 0
    assert json.loads(stdout) == {
        "version": 1,
        "project_root": str(tmp_path.resolve()),
        "tasks": [{"name": "spsc"}],
    }


def test_relative_project_resolves_against_the_working_directory(tmp_path: Path) -> None:
    _task(tmp_path / "repo", "mpmc")

    code, stdout, _ = run(["repo"], cwd=tmp_path)

    assert (code, stdout) == (0, "mpmc\n")


def test_missing_project_exits_1_naming_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"

    code, stdout, stderr = run([str(missing), "--json"], cwd=tmp_path)

    assert (code, stdout) == (1, "")
    assert str(missing) in stderr


def test_project_without_tasks_exits_1_naming_the_tasks_directory(tmp_path: Path) -> None:
    code, stdout, stderr = run([str(tmp_path), "--json"], cwd=tmp_path)

    assert (code, stdout) == (1, "")
    assert ".vibesys/tasks/" in stderr


def test_invalid_task_exits_1_naming_it(tmp_path: Path) -> None:
    (tmp_path / ".vibesys" / "tasks" / "broken").mkdir(parents=True)

    code, stdout, stderr = run([str(tmp_path), "--json"], cwd=tmp_path)

    assert (code, stdout) == (1, "")
    assert "broken" in stderr


def test_launcher_routes_tasks_to_the_engine_without_a_tui() -> None:
    assert _headless_requested(["tasks", "--json"])
