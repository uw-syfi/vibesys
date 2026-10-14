"""Every dynamic agent prompt shows the run environment's notes exactly once.

live-slurm: three live dynamic runs on Slurm gave agents no environment notes, so they never
learned about the gate client, the agent GPU command client, or that GPU commands run in
the host's environment. The search's run-level prompt variables come from the plan the policy
resolves from the run facts, so these tests plan a run and render every requestable template
with exactly the variables the render owner would pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    CONTEXTS,
    FRESH_TURNS,
    RENDERER,
    TEMPLATE_FILES,
)
from tests.vibesys.orchestration.dynamic.core_policy._inputs import plan_of, resolved
from tests.vibesys.orchestration.dynamic.prompt_variables import context_variables

from vibesys.orchestration.dynamic.search.api import PromptTemplate
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

# The distinctive note a run environment contributes; no template wording contains it.
_NOTES = "ZQ-ENV-NOTE: reach the gate with the gate client; GPU commands run on the host."

# `FRESH_TURNS` open a fresh agent turn. Each runs commands in a sandbox or plans work for
# agents that do, so each must carry the notes. A resume or a correction continues a provider
# session that already received them, so those must not repeat them.


def _plan_variables(root: Path, notes: str) -> dict[str, object]:
    run = resolved(root)
    facts = run.facts.model_copy(update={"environment_notes": notes})
    plan = plan_of(
        type(run)(
            facts=facts,
            evaluation_plan=run.evaluation_plan,
            environment=run.environment,
            baseline_commit=run.baseline_commit,
            artifacts=run.artifacts,
        )
    )
    return dict(plan.prompt_variables)


def _render(template: PromptTemplate, context: BaseModel, run_variables: dict[str, object]) -> str:
    variables = context_variables(context)
    return RENDERER.render_template(TEMPLATE_FILES[template], **{**run_variables, **variables})


def test_every_template_is_classified_as_a_fresh_turn_or_a_continuation() -> None:
    assert set(FRESH_TURNS) <= set(PromptTemplate)


@pytest.mark.parametrize("template", FRESH_TURNS, ids=lambda item: item.value)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=examples(pr=10, nightly=25),
)
@given(data=st.data())
def test_a_fresh_turn_prompt_shows_the_environment_notes_exactly_once(
    template: PromptTemplate, data: st.DataObject, tmp_path: Path
) -> None:
    context = data.draw(CONTEXTS[template])
    variables = _plan_variables(tmp_path, _NOTES)

    text = _render(template, context, variables)

    assert text.count(_NOTES) == 1


@pytest.mark.parametrize("template", FRESH_TURNS, ids=lambda item: item.value)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=5)
@given(data=st.data())
def test_a_run_without_environment_notes_shows_no_notes_section(
    template: PromptTemplate, data: st.DataObject, tmp_path: Path
) -> None:
    context = data.draw(CONTEXTS[template])
    variables = _plan_variables(tmp_path, "")

    assert "Run environment notes" not in _render(template, context, variables)
