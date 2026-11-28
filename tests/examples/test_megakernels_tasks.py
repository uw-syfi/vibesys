"""The megakernel tasks name real MegaBench cases and their own candidate files."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest
from tests.examples.example_task import load_example_task

_PROJECT = Path(__file__).parents[2] / "examples/kernels/megakernels"
_CASES = runpy.run_path(str(_PROJECT / ".vibesys/shared/megabench/cases.py"))
_TASKS = sorted(path.name for path in (_PROJECT / ".vibesys/tasks").iterdir())


def test_every_hardware_target_has_the_seven_workloads() -> None:
    by_hw: dict[str, set[str]] = {}
    for task in _TASKS:
        workload, _, hw = task.rpartition("-")
        by_hw.setdefault(hw, set()).add(workload)
    expected = {
        "dense-qwen3-0.6b",
        "moe-qwen3-30b-a3b",
        "w8-gemma3-4b",
        "w4-gemma3-4b",
        "tts-csm-1b",
        "vla-pi05",
        "world-waypoint1.5-1b",
    }
    assert by_hw
    assert all(workloads == expected for workloads in by_hw.values()), by_hw


@pytest.mark.parametrize("task", _TASKS)
def test_task_case_matches_the_vendored_harness(task: str) -> None:
    public = json.loads((_PROJECT / ".vibesys/tasks" / task / "case.json").read_text())
    case = _CASES["select_cases"]("all", [public["id"]])[0]
    assert case.ready
    assert case.max_gpu_launches == 1
    assert public == {key: value for key, value in case.to_dict().items() if key != "note"}


@pytest.mark.parametrize("task", _TASKS)
def test_task_grades_its_own_candidate(task: str) -> None:
    workload, _, hw = task.rpartition("-")
    assert (_PROJECT / hw / workload / "submission.py").is_file()
    manifest = load_example_task(_PROJECT, task).manifest
    for command in (manifest.accuracy.command, manifest.benchmark.command):
        assert command is not None
        assert command[1] == "${PROJECT_ROOT}/.vibesys/shared/evaluate.py"
        assert command[command.index("--task") + 1] == task
