"""A planned profile's trusted outcome informs the next portfolio decision."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tests.vibesys.orchestration.dynamic.strategy._executors import Executors
from tests.vibesys.orchestration.dynamic.strategy._replies import (
    implement,
    implemented,
    plan_reply,
    profile,
    reviewed,
)
from tests.vibesys.orchestration.dynamic.strategy._run import run_shell

import vibesys.orchestration.dynamic.core_policy.prompts as templates
from vibesys.orchestration.dynamic.core_policy.api import run_prompt_variables
from vibesys.orchestration.dynamic.strategy.api import (
    PlannerPrompt,
    RenderRoleArtifacts,
    dynamic_operation_registry,
)
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import ExecuteRegisteredOperation
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.testing import HostOptions

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.strategy.api import ProfileHistoryRow


def _planner_prompts(executors: Executors) -> list[PlannerPrompt]:
    registry = dynamic_operation_registry()
    renders = [
        registry.decode(item.operation)
        for item in executors.seen
        if isinstance(item, ExecuteRegisteredOperation)
    ]
    return [
        item.context
        for item in renders
        if isinstance(item, RenderRoleArtifacts) and isinstance(item.context, PlannerPrompt)
    ]


def _text(prompt: PlannerPrompt) -> str:
    variables = {name: getattr(prompt, name) for name in type(prompt).model_fields}
    variables.pop("template")
    root = Path(templates.__file__).parent
    return TemplateRenderer(root, fallback_roots=(CORE_PROTOCOL_DIR,)).render_template(
        "portfolio.j2",
        **variables,
        **run_prompt_variables(
            objective="Raise throughput.", environment_notes="", agent_evaluation=False
        ),
    )


@pytest.mark.parametrize(
    ("reading", "status", "detail"),
    [
        ((True, "queue_step holds 75% of device time"), "observed", "queue_step holds 75%"),
        ((False, "profile capture process failed"), "failed", "profile capture process failed"),
    ],
)
def test_the_next_plan_receives_a_completed_profiles_intent_and_trusted_outcome(
    reading: tuple[bool, str], status: str, detail: str
) -> None:
    executors = Executors(
        planner=deque(
            [
                plan_reply(implement("A")),
                plan_reply(
                    profile(
                        "prof-A",
                        target_hypothesis_id="A",
                        question="Where does queue_step spend device time?",
                        decision_impact="Optimize queue_step if it dominates.",
                    )
                ),
                plan_reply(implement("after-profile")),
            ]
        ),
        profiler=deque([implemented()]),
        implementer=deque([implemented(), implemented()]),
        judge=deque([reviewed(), reviewed()]),
        profile_reading=lambda _commit: reading,
    )

    run_shell(
        executors,
        max_rounds=3,
        profiling=True,
        profile_measurement=True,
        host=HostOptions(lifecycle=frozenset({"profile-capture"})),
    )

    prompts = _planner_prompts(executors)
    assert len(prompts) == 3
    profiles = [row for row in prompts[2].history if getattr(row, "kind", None) == "profile"]
    assert len(profiles) == 1
    row = cast("ProfileHistoryRow", profiles[0])
    assert row.profile_id == "prof-A"
    assert row.target_hypothesis_id == "A"
    target = next(
        item for item in prompts[2].buildable if item.hypothesis_id == row.target_hypothesis_id
    )
    assert row.revision == target.revision
    assert row.revision != prompts[2].base_revision
    assert row.question == "Where does queue_step spend device time?"
    assert row.decision_impact == "Optimize queue_step if it dominates."
    assert row.status == status
    assert len(row.evidence_ids) == 1
    assert row.evidence_ids[0].root.startswith("profile:profile:")
    assert detail in (row.diagnosis or row.failure)

    rendered = _text(prompts[2])
    for expected in (
        "prof-A",
        status,
        "Where does queue_step spend device time?",
        "Optimize queue_step if it dominates.",
        row.evidence_ids[0].root,
        detail,
    ):
        assert expected in rendered
