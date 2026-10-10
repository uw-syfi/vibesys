"""``vibesys tasks``: list the tasks a VibeSys project defines.

A project's tasks are the directories under ``.vibesys/tasks/``. ``vibesys tasks
[PROJECT] [--json]`` opens ``PROJECT`` (the current directory by default)
through ``vs_project``'s ``Project`` and prints its validated tasks, ordered by
name. ``--json`` prints one ``TaskList`` document on stdout, which the desktop
app reads to offer a task picker; the human form prints one name per line. A
project that does not exist, has no task configuration, or holds an invalid
task exits 1 with the reason on stderr and nothing on stdout.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from vs_project.api import Project, ProjectError


class TaskSummary(BaseModel):
    """One task of a project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The task's directory name, the value ``--task`` accepts."""


class TaskList(BaseModel):
    """``vibesys tasks --json``: the tasks of one project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    project_root: str
    """The resolved absolute project directory the tasks were read from."""
    tasks: tuple[TaskSummary, ...]


class TasksError(Exception):
    """The project's tasks could not be listed; the message names the path."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibesys tasks",
        description="List the tasks a VibeSys project defines under .vibesys/tasks/.",
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        default=None,
        help="the project directory (default: the current directory)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def list_tasks(project_root: Path) -> TaskList:
    """Read ``project_root``'s tasks; ``TasksError`` names what is missing or invalid."""
    root = project_root.expanduser()
    try:
        project = Project.open(root)
        if not project.is_initialized():
            message = f"{project.root} has no VibeSys tasks: {project.configuration_path()}/tasks/ does not exist"
            raise TasksError(message)
        tasks = project.discover_tasks()
    except ProjectError as exc:
        raise TasksError(str(exc)) from exc
    return TaskList(
        project_root=str(project.root),
        tasks=tuple(TaskSummary(name=task.name.value) for task in tasks),
    )


def format_tasks(listing: TaskList) -> str:
    """Render a listing for a terminal, one task name per line."""
    if not listing.tasks:
        return f"{listing.project_root} defines no tasks."
    return "\n".join(task.name for task in listing.tasks)


def run(argv: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run one ``vibesys tasks`` command; return its exit code, stdout and stderr text."""
    args = _parser().parse_args(argv)
    project = args.project if args.project is not None else cwd
    if not project.expanduser().is_absolute():
        project = cwd / project
    try:
        listing = list_tasks(project)
    except TasksError as exc:
        return 1, "", f"vibesys tasks: {exc}\n"
    return 0, (listing.model_dump_json() if args.json else format_tasks(listing)) + "\n", ""


def main(argv: list[str] | None = None) -> int:
    """Run ``vibesys tasks`` from the current directory."""
    code, output, error = run(sys.argv[1:] if argv is None else argv, cwd=Path.cwd())
    sys.stdout.write(output)
    sys.stderr.write(error)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
