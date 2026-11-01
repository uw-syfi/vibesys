"""``project.task_goal``: a project's own task goal and objectives, through ``vs_project``'s layout.

The reply is what the task's files say (``OBJECTIVE.md`` cut to its bound, the axes of
``objectives.toml``), the same facts the example catalogue reads for a task, and it is gated by
``project_goal`` so a strict client that did not negotiate it never sees the method.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host
from tests.support.task_project import task_path, write_task

from entrypoints.example_catalog import read_task_detail
from server.host_api import (
    HelperError,
    HelperErrorCode,
    HelperFeature,
    ProjectTaskGoalReply,
    ProjectTaskGoalRequest,
)
from server.host_examples import GOAL_MAX_CHARS, bounded_goal
from server.host_helper import ChannelState, Execute, Received, Send, step
from server.host_helper_requests import answer
from vs_project.api import Project
from vs_sim.api.testing import SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_GOALS = st.text(max_size=3 * GOAL_MAX_CHARS // 2)
_AXES = st.lists(
    st.tuples(st.text("abcxyz_", min_size=1, max_size=6), st.sampled_from(["max", "min"])),
    max_size=3,
    unique_by=lambda axis: axis[0],
)
_KEEP = settings(
    max_examples=examples(pr=25, nightly=150),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)


def _objectives(axes: list[tuple[str, str]]) -> str:
    return "".join(f'[[objective]]\nname = "{n}"\ndirection = "{d}"\n\n' for n, d in axes)


def _ask(project: Path, task: str) -> ProjectTaskGoalReply | HelperError:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = ProjectTaskGoalRequest(request_id="g", deadline_ms=1, project=str(project), task=task)
    reply = answer(request, host, threads.event())
    assert isinstance(reply, ProjectTaskGoalReply | HelperError)
    return reply


@example(goal="\r", axes=[])
@given(goal=_GOALS, axes=_AXES)
@_KEEP
def test_the_reply_is_the_goal_cut_to_its_bound_and_the_declared_axes(
    tmp_path_factory: pytest.TempPathFactory, goal: str, axes: list[tuple[str, str]]
) -> None:
    project = tmp_path_factory.mktemp("goal")
    directory = write_task(project, "", objective=goal, task="mine")
    if axes:
        (directory / "objectives.toml").write_text(_objectives(axes), encoding="utf-8")

    reply = _ask(project, "mine")

    assert isinstance(reply, ProjectTaskGoalReply)
    # The file is read as text, so a lone carriage return reads as a newline.
    expected, truncated = bounded_goal((directory / "OBJECTIVE.md").read_text(encoding="utf-8"))
    assert reply.goal.task == "mine"
    assert (reply.goal.goal, reply.goal.goal_truncated) == (expected, truncated)
    assert [(o.metric, o.direction) for o in reply.goal.objectives] == axes


@given(goal=_GOALS, axes=_AXES)
@_KEEP
def test_it_says_what_the_example_catalogue_says_of_the_same_task(
    tmp_path_factory: pytest.TempPathFactory, goal: str, axes: list[tuple[str, str]]
) -> None:
    project = tmp_path_factory.mktemp("same")
    manifest = (
        '[catalogue]\ndomain = "databases"\n'
        '[catalogue.hardware]\naccelerator = "CPU"\ncount = 1\nvendor = "cpu"\n'
    )
    directory = write_task(project, manifest, objective=goal, task="mine")
    if axes:
        (directory / "objectives.toml").write_text(_objectives(axes), encoding="utf-8")

    detail = read_task_detail(Project.open(project).select_task("mine"))
    reply = _ask(project, "mine")

    assert detail is not None
    assert isinstance(reply, ProjectTaskGoalReply)
    assert (detail.task, detail.goal, detail.goal_truncated, detail.objectives) == (
        reply.goal.task,
        reply.goal.goal,
        reply.goal.goal_truncated,
        reply.goal.objectives,
    )


def test_a_task_the_project_lacks_or_unreadable_files_are_typed_failures_naming_the_project(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    directory = write_task(project, "", task="mine")
    cases = {
        "absent": (HelperErrorCode.TASK_INVALID, lambda: None),
        "mine": (
            HelperErrorCode.TASK_INVALID,
            lambda: (directory / "objectives.toml").write_text("[[objective]]\n", encoding="utf-8"),
        ),
    }
    for task, (code, damage) in cases.items():
        damage()
        reply = _ask(project, task)
        assert isinstance(reply, HelperError)
        assert reply.code is code
        assert reply.path == str(project.resolve())

    (directory / "objectives.toml").unlink()
    (directory / "OBJECTIVE.md").write_bytes(b"\xff\xfe not text")
    broken = _ask(project, "mine")
    assert isinstance(broken, HelperError)
    assert broken.code is HelperErrorCode.TASK_INVALID
    assert "UTF-8" in broken.message
    assert task_path(project, "mine").exists()


def test_a_directory_with_no_tasks_is_uninitialized_and_a_missing_one_is_missing(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    for project, code in (
        (empty, HelperErrorCode.PROJECT_UNINITIALIZED),
        (tmp_path / "missing", HelperErrorCode.PROJECT_MISSING),
    ):
        reply = _ask(project, "mine")
        assert isinstance(reply, HelperError)
        assert reply.code is code


def test_the_method_needs_the_project_goal_feature() -> None:
    hello = {
        "type": "hello",
        "request_id": "h",
        "deadline_ms": 5000,
        "client_versions": [1, 1],
        "features": [],
        "app_version": "test",
    }
    ask: dict[str, Any] = {
        "type": "project.task_goal",
        "request_id": "g",
        "deadline_ms": 5000,
        "project": "/p",
        "task": "mine",
    }

    def refusal(features: list[str]) -> HelperError | None:
        state, _ = step(
            ChannelState(),
            Received(json.dumps({**hello, "features": features}).encode(), 0.0),
            "v",
        )
        _, outputs = step(state, Received(json.dumps(ask).encode(), 0.0), "v")
        if any(isinstance(output, Execute) for output in outputs):
            return None
        (sent,) = [output.message for output in outputs if isinstance(output, Send)]
        assert isinstance(sent, HelperError)
        return sent

    refused = refusal([])
    assert refused is not None
    assert refused.code is HelperErrorCode.FEATURE_NOT_NEGOTIATED
    assert refusal([HelperFeature.PROJECT_GOAL.value]) is None
