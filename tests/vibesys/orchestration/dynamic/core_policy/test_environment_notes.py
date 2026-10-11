"""Every dynamic agent prompt shows the run environment's notes exactly once.

live-slurm: three live dynamic runs on Slurm gave agents no environment notes, so they never
learned about the gate client, the agent GPU command client, or that GPU commands run in
the host's environment. The loop's run-level prompt variables come from the plan the policy
resolves from the run facts, so these tests plan a run and render every requestable template
with exactly the variables the render owner would pass.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    COMMIT,
    bounds_of,
    options_of,
    resolved,
    run_request,
)
from tests.vibesys.orchestration.dynamic.core_policy.test_prompt_templates import (
    _CONTEXTS,
    _ROOT,
)

from vibesys.orchestration.dynamic import PLUGIN
from vibesys.orchestration.dynamic.strategy.api import PromptTemplate
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import RevisionRef
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import CoreRunContext

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

# The distinctive note a run environment contributes; no template wording contains it.
_NOTES = "ZQ-ENV-NOTE: reach the gate with the gate client; GPU commands run on the host."

# Templates that open a fresh agent turn. Each runs commands in a sandbox or plans work
# for agents that do, so each must carry the notes. A resume or a correction continues a
# provider session that already received them, so those must not repeat them.
_FRESH_TURNS = (
    PromptTemplate.PORTFOLIO,
    PromptTemplate.PORTFOLIO_CORRECTION,
    PromptTemplate.IMPLEMENT,
    PromptTemplate.REVIEW,
    PromptTemplate.PROFILE_REQUEST,
)


def _plan_variables(root: Path, notes: str) -> dict[str, object]:
    request = run_request(root)
    run = resolved(root)
    bounds = bounds_of(request)
    assert PLUGIN.core is not None
    plan = PLUGIN.core.plan(
        CoreRunContext(
            run_id="policy",
            options=options_of(request),
            facts=run.facts.model_copy(update={"environment_notes": notes}),
            baseline=RevisionRef.of_git_commit(COMMIT),
            evaluation_plan=run.evaluation_plan,
            environment=replace(
                run.environment,
                env_kind="docker",
                cli_sandboxed=True,
                share_agent_session=False,
                parallel_candidate_blocker=None,
            ),
            artifacts=run.artifacts,
            evaluation_capacity=1,
            queue_allowance_seconds=bounds.queue_allowance_seconds,
            observe_interval_seconds=bounds.observe_interval_seconds,
            observe_backoff_cap_seconds=bounds.observe_backoff_cap_seconds,
            max_run_seconds=bounds.max_run_seconds,
        )
    )
    return dict(plan.prompt_variables)


def _render(template: PromptTemplate, context: BaseModel, run_variables: dict[str, object]) -> str:
    variables = {name: getattr(context, name) for name in type(context).model_fields}
    variables.pop("template")
    return TemplateRenderer(_ROOT, fallback_roots=(CORE_PROTOCOL_DIR,)).render_template(
        f"{template.value}.j2", **{**run_variables, **variables}
    )


def test_every_template_is_classified_as_a_fresh_turn_or_a_continuation() -> None:
    assert set(_FRESH_TURNS) <= set(PromptTemplate)


@pytest.mark.parametrize("template", _FRESH_TURNS, ids=lambda item: item.value)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=25)
@given(data=st.data())
def test_a_fresh_turn_prompt_shows_the_environment_notes_exactly_once(
    template: PromptTemplate, data: st.DataObject, tmp_path: Path
) -> None:
    context = data.draw(_CONTEXTS[template])
    variables = _plan_variables(tmp_path, _NOTES)

    text = _render(template, context, variables)

    assert text.count(_NOTES) == 1


@pytest.mark.parametrize("template", _FRESH_TURNS, ids=lambda item: item.value)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=5)
@given(data=st.data())
def test_a_run_without_environment_notes_shows_no_notes_section(
    template: PromptTemplate, data: st.DataObject, tmp_path: Path
) -> None:
    context = data.draw(_CONTEXTS[template])
    variables = _plan_variables(tmp_path, "")

    assert "Run environment notes" not in _render(template, context, variables)
