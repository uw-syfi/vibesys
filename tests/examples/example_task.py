"""Locate and load one task of a task-layout example project."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api.request import load_project_task
from vs_project.api import Project

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api.request import InputBundle


def task_root(project_root: Path, task: str = "default") -> Path:
    """The directory holding the trusted files of ``task`` in ``project_root``."""
    return project_root / ".vibesys" / "tasks" / task


def load_example_task(project_root: Path, task: str = "default") -> InputBundle:
    """The validated input bundle of one task, loaded against its project root."""
    project = Project.open(project_root)
    return load_project_task(project, project.select_task(task))
