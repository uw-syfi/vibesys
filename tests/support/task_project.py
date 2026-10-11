"""Write a one-task VibeSys project for a test, in the only supported layout.

A task's ``OBJECTIVE.md``, ``vibesys.input.toml``, and the files its commands
read live in ``<project>/.vibesys/tasks/<task>/``; candidate source lives at the
project root. Tests that need a project build it with :func:`write_task`
and load it with :func:`vibesys.inputs.load_task`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_TASK = "default"


def task_path(project: Path, task: str = DEFAULT_TASK) -> Path:
    """The directory of ``task`` inside ``project``."""
    return project / ".vibesys" / "tasks" / task


def write_task(
    project: Path,
    manifest: str,
    *,
    objective: str = "Optimize the candidate.\n",
    task: str = DEFAULT_TASK,
) -> Path:
    """Write ``task``'s objective and manifest under ``project``; return the task directory."""
    directory = task_path(project, task)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "OBJECTIVE.md").write_text(objective, encoding="utf-8")
    (directory / "vibesys.input.toml").write_text(manifest, encoding="utf-8")
    return directory
