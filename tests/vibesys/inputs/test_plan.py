"""The ``[plan]`` table of an input manifest: what it accepts, what it rejects, and where it says so.

A plan's steps are generated as a dependency graph (each step depends only on earlier ones), so
every generated plan is one a run could follow; each rejection is made by breaking one generated
plan in one way, and the error must name the key path of what broke.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.task_project import task_path, write_task

from vibesys.inputs import (
    InputManifest,
    PlanStep,
    TaskPlan,
    load_task,
    outline_problems,
    render_input_manifest,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

DESIGN = "reference/README.md"
BASE = {
    "version": 1,
    "agent": {"domain": "generic"},
    "accuracy": {"command": ["python", "accuracy.py"]},
    "benchmark": {"command": ["python", "benchmark.py"]},
}
TEXT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), min_size=1, max_size=30
)


@st.composite
def plans(draw: st.DrawFn) -> dict[str, Any]:
    """A valid ``[plan]`` table: steps that depend only on steps declared before them."""
    count = draw(st.integers(min_value=0, max_value=6))
    steps = []
    for index in range(count):
        earlier = [f"s{n}" for n in range(index)]
        steps.append(
            {
                "id": f"s{index}",
                "title": draw(TEXT),
                "documents": draw(
                    st.lists(st.sampled_from([DESIGN, "reference/a.md"]), max_size=2)
                ),
                "done_when": draw(TEXT),
                "evaluate": draw(st.sampled_from(["none", "trusted"])),
                "depends_on": draw(st.lists(st.sampled_from(earlier), unique=True))
                if earlier
                else [],
            }
        )
    order = draw(st.permutations(range(count)))
    return {
        "documents": [DESIGN],
        "deviations": draw(st.sampled_from(["record", "forbid"])),
        "steps": [steps[i] for i in order],
    }


def manifest(plan: dict[str, Any]) -> InputManifest:
    return InputManifest.model_validate({**BASE, "plan": plan})


@settings(max_examples=examples(pr=30, nightly=300))
@given(plan=plans())
def test_a_plan_whose_steps_form_a_graph_is_accepted_and_round_trips_through_toml(
    plan: dict[str, Any],
) -> None:
    parsed = manifest(plan)
    assert parsed.plan is not None
    assert [step.id for step in parsed.plan.steps] == [step["id"] for step in plan["steps"]]
    assert InputManifest.model_validate(tomllib.loads(render_input_manifest(parsed))) == parsed


def _broken(plan: dict[str, Any], how: str, at: int) -> tuple[dict[str, Any], str]:
    """``plan`` broken one way at step ``at``, and the text its error must contain."""
    steps: list[dict[str, Any]] = [
        {**step, "depends_on": list(step["depends_on"])} for step in plan["steps"]
    ]
    step = steps[at]
    match how:
        case "duplicate":
            steps.append(dict(step))
            return {**plan, "steps": steps}, f"steps[{len(steps) - 1}].id: duplicate step id"
        case "unknown":
            step["depends_on"].append("nowhere")
            where = f"steps[{at}].depends_on[{len(step['depends_on']) - 1}]: unknown step"
            return {**plan, "steps": steps}, where
        case "itself":
            step["depends_on"].append(step["id"])
            where = f"steps[{at}].depends_on[{len(step['depends_on']) - 1}]: step"
            return {**plan, "steps": steps}, where
        case "cycle":
            # a new step that depends on ``step`` and that ``step`` depends on in turn
            steps.append({**step, "id": "loop", "depends_on": [step["id"]]})
            step["depends_on"].append("loop")
            return {**plan, "steps": steps}, "steps: dependency cycle among"
        case _:
            message = f"unknown way to break a plan: {how}"
            raise AssertionError(message)


@settings(max_examples=examples(pr=30, nightly=300))
@given(
    plan=plans().filter(lambda plan: plan["steps"]),
    how=st.sampled_from(["duplicate", "unknown", "itself", "cycle"]),
    pick=st.integers(min_value=0),
)
def test_a_plan_broken_in_one_way_is_rejected_naming_the_key_path(
    plan: dict[str, Any], how: str, pick: int
) -> None:
    broken, where = _broken(plan, how, pick % len(plan["steps"]))

    with pytest.raises(ValidationError, match="plan") as raised:
        manifest(broken)

    assert where in str(raised.value)


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ({"documents": [DESIGN], "mode": "follow"}, "plan.mode"),
        ({"documents": []}, "plan.documents"),
        ({"documents": ["/etc/passwd"]}, "relative to the task directory"),
        ({"documents": ["reference/../../x.md"]}, "parent path components"),
        ({"documents": [DESIGN], "deviations": "sometimes"}, "plan.deviations"),
        (
            {
                "documents": [DESIGN],
                "steps": [{"id": "A", "title": "t", "done_when": "d", "evaluate": "none"}],
            },
            r"plan.steps.0.id",
        ),
        (
            {
                "documents": [DESIGN],
                "steps": [{"id": "a", "title": "t", "done_when": "d", "evaluate": "gpu"}],
            },
            r"plan.steps.0.evaluate",
        ),
        (
            {
                "documents": [DESIGN],
                "steps": [
                    {"id": "a", "title": "t", "done_when": "d", "evaluate": "none", "gpus": 4}
                ],
            },
            r"plan.steps.0.gpus",
        ),
    ],
)
def test_a_plan_table_rejects_unknown_keys_and_bad_values_naming_them(
    plan: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        manifest(plan)


def test_the_outline_rules_apply_to_steps_however_they_were_written() -> None:
    steps = (
        PlanStep(id="a", title="t", done_when="d", evaluate="none", depends_on=("b",)),
        PlanStep(id="b", title="t", done_when="d", evaluate="trusted", depends_on=("a",)),
    )
    assert outline_problems(steps) == ["steps: dependency cycle among 'a', 'b'"]
    assert outline_problems(steps[:1]) == ["steps[0].depends_on[0]: unknown step 'b'"]
    with pytest.raises(ValidationError, match="cycle"):
        TaskPlan(documents=(DESIGN,), steps=steps)


def _task_with_plan(tmp_path: Path, plan: dict[str, Any]) -> None:
    write_task(tmp_path, render_input_manifest(manifest(plan)))
    reference = task_path(tmp_path) / "reference"
    reference.mkdir()
    (reference / "README.md").write_text("# Design\n", encoding="utf-8")


def test_a_loaded_task_carries_its_plan(tmp_path: Path) -> None:
    plan = {
        "documents": [DESIGN],
        "steps": [
            {"id": "a", "title": "t", "documents": [DESIGN], "done_when": "d", "evaluate": "none"}
        ],
    }
    _task_with_plan(tmp_path, plan)

    bundle = load_task(tmp_path)

    assert bundle.plan == manifest(plan).plan


@pytest.mark.parametrize(
    ("document", "error", "message"),
    [
        ("reference/missing.md", FileNotFoundError, r"plan\.steps\[0\]\.documents\[0\]"),
        ("reference", ValueError, r"plan\.steps\[0\]\.documents\[0\]: 'reference' is not a file"),
        ("outside.md", ValueError, r"plan\.steps\[0\]\.documents\[0\]"),
    ],
)
def test_a_task_whose_plan_names_a_file_it_does_not_hold_is_refused(
    tmp_path: Path, document: str, error: type[Exception], message: str
) -> None:
    plan = {
        "documents": [DESIGN],
        "steps": [
            {"id": "a", "title": "t", "documents": [document], "done_when": "d", "evaluate": "none"}
        ],
    }
    _task_with_plan(tmp_path, plan)
    # a link out of the task directory: present on disk, but not the task's own file
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")
    (task_path(tmp_path) / "outside.md").symlink_to(tmp_path / "secret.md")

    with pytest.raises(error, match=message):
        load_task(tmp_path)
