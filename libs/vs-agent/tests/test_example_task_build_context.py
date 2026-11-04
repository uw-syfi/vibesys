"""The example tasks' ``.dockerignore`` keeps run leftovers out of the task image's tag.

Running a task's checker and benchmark writes ``__pycache__`` directories into the task
folder, which is the build context. The tag keys to every build input, so a leftover
cache would rebuild an image nothing changed in.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vs_agent.api.images import task_image_reference

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
_TASKS = sorted(path.parent for path in _EXAMPLES.glob("**/.vibesys/tasks/*/.dockerignore"))
_BASE = "python:3.12-bookworm"
# Tasks whose Dockerfile predates this one and is not a plain CPU image (Verus, Trainium).
_OTHER_TASKS = {"verus-mpmc-open", "Llama-3-8B-trn2"}


def _reference(task: Path) -> str:
    return task_image_reference(task / "Dockerfile", base_image=_BASE)


def test_cpu_example_tasks_ship_a_dockerignore() -> None:
    dockerfiles = {path.parent for path in _EXAMPLES.glob("**/.vibesys/tasks/*/Dockerfile")}
    cpu_tasks = {task for task in dockerfiles if not _OTHER_TASKS & set(task.parts)}

    assert cpu_tasks
    assert cpu_tasks <= set(_TASKS)


@pytest.mark.parametrize("task", _TASKS, ids=lambda task: "/".join(task.parts[-4:]))
def test_bytecode_caches_do_not_change_the_task_image_reference(task: Path, tmp_path: Path) -> None:
    copy = tmp_path / task.name
    shutil.copytree(task, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    before = _reference(copy)

    (copy / "__pycache__").mkdir()
    (copy / "__pycache__" / "checker.cpython-312.pyc").write_bytes(b"cache")
    nested = copy / "profiler" / "__pycache__"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "tool.cpython-312.pyc").write_bytes(b"cache")
    (copy / "stray.pyc").write_bytes(b"cache")

    assert _reference(copy) == before


@pytest.mark.parametrize("task", _TASKS, ids=lambda task: "/".join(task.parts[-4:]))
def test_a_source_change_still_changes_the_task_image_reference(task: Path, tmp_path: Path) -> None:
    copy = tmp_path / task.name
    shutil.copytree(task, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    before = _reference(copy)

    (copy / "added_checker.py").write_text("print('changed')\n", encoding="utf-8")

    assert _reference(copy) != before
