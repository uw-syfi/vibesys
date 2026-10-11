"""Every prompt the single strategy can request renders from the core policy's templates.

The strategy sends a typed `PromptContext`; the render owner passes its fields (plus the
run-level `objective`) to the template under strict undefined variables. A template that reads a
name no context supplies fails the render, which ends the round, so each template is exercised
over its context space.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vibesys.orchestration.single.core_policy.prompts as templates
from vibesys.orchestration.single.core_policy.api import run_prompt_variables
from vibesys.orchestration.single.strategy._prompts import (
    Bottleneck,
    FrontierRow,
    LastProfile,
    PendingClaim,
    RoundRow,
)
from vibesys.orchestration.single.strategy.api import PlanPrompt, PromptTemplate, RoundPrompt
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import RevisionId, RevisionRef
from vs_prompts.api import TemplateRenderer

_ROOT = Path(templates.__file__).parent
_OBJECTIVE = "Raise queue throughput."
_RENDERER = TemplateRenderer(_ROOT, fallback_roots=(CORE_PROTOCOL_DIR,))
_TEXT = st.text(alphabet="abcdefg hij", min_size=1, max_size=12)
_REVISIONS = st.sampled_from(("a1", "b2")).map(
    lambda name: RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")
)
_METRICS = st.lists(st.tuples(_TEXT, st.floats(0, 1e6)), max_size=2).map(tuple)
_PLAN = st.builds(
    PlanPrompt,
    round_number=st.integers(1, 5),
    max_rounds=st.integers(5, 9),
    plateau_warning=st.none() | _TEXT,
    provisional_candidates=st.integers(0, 3),
    official_eval_every=st.integers(1, 3),
    official_cadence_due=st.booleans(),
    benchmark_enabled=st.booleans(),
    history=st.lists(
        st.builds(
            RoundRow,
            round_number=st.integers(1, 5),
            hypothesis_id=_TEXT,
            claim=_TEXT,
            passed=st.booleans(),
            official=st.booleans(),
            judge_verdict=st.sampled_from(("pass", "fail", "deferred")),
            metric=st.none() | st.floats(0, 1e6),
            unit=st.none() | _TEXT,
            delta_pct=st.none() | st.floats(-50, 50),
            retained=st.none() | st.booleans(),
            commit=st.none() | _TEXT,
        ),
        max_size=3,
    ).map(tuple),
    frontier=st.lists(
        st.builds(
            FrontierRow,
            round_number=st.integers(1, 5),
            commit=_TEXT,
            official=st.booleans(),
            metrics=_METRICS,
        ),
        max_size=2,
    ).map(tuple),
    pending_claims=st.lists(
        st.builds(PendingClaim, round_number=st.integers(1, 5), commit=_TEXT, metrics=_METRICS),
        max_size=2,
    ).map(tuple),
    last_profile=st.none() | st.builds(LastProfile, analysis=_TEXT, bottlenecks=_TEXT),
    regression_round=st.none() | st.integers(1, 5),
    regression_kind=st.none() | _TEXT,
    exhaustion_feedback=st.none() | _TEXT,
    exhaustion_round=st.none() | st.integers(1, 5),
    active_component=st.none() | _TEXT,
    ranked_bottlenecks=st.lists(
        st.builds(Bottleneck, component=_TEXT, cost_share_pct=st.floats(0, 100)), max_size=2
    ).map(tuple),
    base_revision=_REVISIONS,
)
_ROUND = st.builds(
    RoundPrompt,
    round_number=st.integers(1, 5),
    retry=st.integers(1, 3),
    hypothesis_id=_TEXT,
    title=_TEXT,
    hypothesis=_TEXT,
    task=_TEXT,
    pass_criteria=_TEXT,
    base_revision=_REVISIONS,
    feedback=st.none() | _TEXT,
    no_valid_reply=st.none() | _TEXT,
    dominated_by_rounds=st.lists(st.integers(1, 5), max_size=2).map(tuple),
    official_due=st.booleans(),
    official_reason=st.none() | _TEXT,
    profiler_id=st.sampled_from(("none", "torch_profiler")),
)


def _variables(
    context: PlanPrompt | RoundPrompt, *, agent_evaluation: bool = False
) -> dict[str, object]:
    variables = {name: getattr(context, name) for name in type(context).model_fields}
    variables.pop("template")
    return {
        **run_prompt_variables(
            objective=_OBJECTIVE, environment_notes="", agent_evaluation=agent_evaluation
        ),
        **variables,
    }


@given(context=_PLAN)
def test_the_plan_prompt_renders_for_every_context(context: PlanPrompt) -> None:
    text = _RENDERER.render_template(f"{PromptTemplate.PLAN.value}.j2", **_variables(context))
    assert _OBJECTIVE in text
    assert f"Round {context.round_number} of {context.max_rounds}" in text
    for row in context.history:
        assert row.hypothesis_id in text


@given(context=_ROUND)
def test_the_round_prompt_renders_for_every_context(context: RoundPrompt) -> None:
    text = _RENDERER.render_template(f"{PromptTemplate.ROUND.value}.j2", **_variables(context))
    assert _OBJECTIVE in text
    assert context.task in text
    assert context.hypothesis_id in text
    assert (context.feedback is None) or context.feedback in text


@pytest.mark.parametrize("template", [PromptTemplate.ROUND])
@given(data=st.data(), offered=st.booleans())
def test_the_evaluation_tool_is_described_exactly_when_it_is_offered(
    template: PromptTemplate, data: st.DataObject, *, offered: bool
) -> None:
    context = data.draw(_ROUND)
    text = _RENDERER.render_template(
        f"{template.value}.j2", **_variables(context, agent_evaluation=offered)
    )
    assert ("submit_evaluation" in text) is offered


def test_the_prompts_name_no_progress_files() -> None:
    """The workspace is an isolated child of a revision: no template points at progress files."""
    for path in _ROOT.glob("*.j2"):
        text = path.read_text()
        assert "progress/" not in text, path
        assert "pareto-frontier.md" not in text, path
