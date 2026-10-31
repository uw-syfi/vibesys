"""What a task is for: the goal its ``OBJECTIVE.md`` states and the axes its ``objectives.toml`` declares.

The example catalog (``vibesys example list``) and the project catalog (``vibesys tasks``,
``get-task-goal``) show the same goal, so one module reads it. It is a module of its own so the
example catalog does not import the host helper's OS adapters to read one file.

Public surface: ``read_task_goal``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.host_examples import ExampleObjective, TaskGoal, bounded_goal
from vs_project.api import load_objectives

if TYPE_CHECKING:
    from vs_project.api import TaskDirectory


def read_task_goal(task: TaskDirectory) -> TaskGoal:
    """What ``task`` is for: its ``OBJECTIVE.md`` (cut to ``GOAL_MAX_CHARS``) and its axes.

    Raises ``OSError`` for a file that cannot be read and ``ValueError`` (naming the file) for
    ``objectives.toml`` that is invalid or a goal that is not UTF-8 text.
    """
    objectives = load_objectives(task.path).objective
    try:
        text = task.objective_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        message = f"{task.objective_path} is not UTF-8 text: {exc}"
        raise ValueError(message) from exc
    goal, truncated = bounded_goal(text)
    return TaskGoal(
        task=task.name.value,
        objectives=tuple(
            ExampleObjective(metric=axis.name, direction=axis.direction) for axis in objectives
        ),
        goal=goal,
        goal_truncated=truncated,
    )
