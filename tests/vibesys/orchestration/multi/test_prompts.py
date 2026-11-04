"""Every prompt the multi strategy can request renders from the core policy's templates.

The strategy sends a typed `PromptContext`; the render owner passes its fields (plus the
run-level variables) to the template under strict undefined variables. A template that reads a
name no context supplies fails the render, which ends the turn, so each template is exercised
over its context space, and the profiler template over every runnable profiler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS
from tests.vibesys.reply_variables import context_variables

import vibesys.orchestration.multi.core_policy.prompts as templates
from vibesys.orchestration.multi.core_policy.api import run_prompt_variables
from vibesys.orchestration.multi.search.api import (
    ArchiveConflictView,
    ConflictMetric,
    Dominator,
    GateFacts,
    HypothesisPlan,
    ImplementerClaims,
    ImplementPrompt,
    JudgePrompt,
    PreRoundPrompt,
    ProfilerPrompt,
    PromptTemplate,
    ValidationFailure,
    multi,
)
from vibesys.orchestration.profilers import ACTIVE_PROFILER_KINDS
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR
from vibesys.serial_search.api import PROMPTS_DIR as SERIAL_SEARCH_PROMPTS
from vibesys.serial_search.api import LastProfile, MeasurementNoise, PlanPrompt, PlanTemplate
from vs_core.api import RevisionId, RevisionRef, Value
from vs_prompts.api import TemplateRenderer
from vs_sim.api.testing import examples

_ROOT = Path(templates.__file__).parent
_OBJECTIVE = "Raise queue throughput."
_RENDERER = TemplateRenderer(
    _ROOT, fallback_roots=(SERIAL_SEARCH_PROMPTS, CORE_PROTOCOL_DIR, PROMPTS_DIR / "shared")
)
_TEXT = st.text(alphabet="abcdefg hij", min_size=1, max_size=12)
_REVISIONS = st.sampled_from(("a1", "b2")).map(
    lambda name: RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")
)
_PLAN = st.builds(
    HypothesisPlan,
    hypothesis_id=_TEXT,
    title=_TEXT,
    hypothesis=_TEXT,
    task=_TEXT,
    pass_criteria=_TEXT,
)
_GATES = st.builds(
    GateFacts,
    revert_applied=st.booleans(),
    revert_round=st.none() | st.integers(1, 4),
    revert_commit=st.none() | _TEXT,
    revalidation_pending=st.booleans(),
    approved_evaluation_artifact=st.none() | _TEXT,
    approved_perf_metric=st.none() | st.floats(0, 1e6),
    approved_perf_unit=st.none() | _TEXT,
)
_CONFLICT = st.none() | st.builds(
    ArchiveConflictView,
    dominators=st.lists(
        st.builds(
            Dominator,
            round_number=st.integers(1, 4),
            metrics=st.lists(
                st.builds(
                    ConflictMetric,
                    name=_TEXT,
                    value=st.floats(0, 1e6),
                    direction=st.sampled_from(("max", "min")),
                ),
                max_size=2,
            ).map(tuple),
        ),
        min_size=1,
        max_size=2,
    ).map(tuple),
)
_VALIDATION = st.none() | st.builds(
    ValidationFailure,
    recipe=_TEXT,
    error=_TEXT,
    output=st.text(max_size=20),
    recipe_unusable=st.booleans(),
)
_REPAIR = {
    "feedback": st.none() | _TEXT,
    "validation_failure": _VALIDATION,
    "no_valid_reply": st.none() | _TEXT,
    "pareto_archive_conflict": _CONFLICT,
}
_IMPLEMENT = st.builds(
    ImplementPrompt,
    round_number=st.integers(1, 5),
    retry=st.integers(1, 3),
    plan=_PLAN,
    base_revision=_REVISIONS,
    framework=_GATES,
    benchmark_enabled=st.booleans(),
    active_component=st.none() | _TEXT,
    continuation_step=st.none() | _TEXT,
    **_REPAIR,
)
_CLAIMS = st.builds(
    ImplementerClaims,
    summary=_TEXT,
    expected_behavior=_TEXT,
    status=st.sampled_from(("implemented", "continue", "blocked")),
    evidence=st.text(max_size=12),
    next_step=st.text(max_size=12),
    perf_metric=st.none() | st.floats(0, 1e6),
    candidate_disposition=st.sampled_from(("unassessed", "pareto_frontier")),
    validation_recipe_artifact=st.none() | st.just("validation/recipes.json"),
)
_JUDGE = st.builds(
    JudgePrompt,
    round_number=st.integers(1, 5),
    retry=st.integers(1, 3),
    plan=_PLAN,
    claims=_CLAIMS,
    framework=_GATES,
    pareto_archive_conflict=_CONFLICT,
    benchmark_enabled=st.booleans(),
)
_PRE_ROUND = st.builds(
    PreRoundPrompt,
    round_number=st.integers(1, 5),
    has_history=st.booleans(),
    regression_kind=st.none() | _TEXT,
    regression_round=st.none() | st.integers(1, 4),
    exhaustion_feedback=st.none() | _TEXT,
    exhaustion_round=st.none() | st.integers(1, 4),
)
_PROFILER = st.builds(
    ProfilerPrompt,
    round_number=st.integers(1, 5),
    profile_focus=_TEXT,
)
_PLANNING = st.builds(
    PlanPrompt,
    round_number=st.integers(1, 5),
    max_rounds=st.integers(5, 9),
    base_revision=_REVISIONS,
    last_profile=st.none()
    | st.builds(
        LastProfile,
        source=st.just("profiler"),
        analysis=_TEXT,
        bottlenecks=_TEXT,
    ),
    measurement_noise=st.none()
    | st.builds(
        MeasurementNoise,
        noise=st.floats(0, 0.5),
        resolution=st.floats(0, 2),
        candidate_samples=st.integers(1, 5),
        base_samples=st.integers(1, 5),
    ),
)


def _variables(
    context: Value, *, agent_evaluation: bool = True, profiler_kind: str = "linux_cpu"
) -> dict[str, object]:
    variables = context_variables(context, multi.reply_schemas)
    return {
        **run_prompt_variables(
            domain_sections=EMPTY_DOMAIN_SECTIONS,
            objective=_OBJECTIVE,
            environment_notes="note",
            agent_evaluation=agent_evaluation,
            validation_recipe_schema='{"version": 1}',
            profiler_id=profiler_kind,
            profile_execution="local",
            profiler_support_name="support",
            profiler_mcp_name="profiler",
            benchmark_command="bench",
            reference_path=".",
            interface="inprocess",
        ),
        **variables,
    }


def _render(
    template: PromptTemplate | PlanTemplate, context: Value, *, profiler_kind: str = "linux_cpu"
) -> str:
    text = _RENDERER.render_template(
        f"{template.value}.j2", **_variables(context, profiler_kind=profiler_kind)
    )
    assert text.strip()
    return text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_PRE_ROUND, kind=st.sampled_from(("none", "linux_cpu")))
def test_the_routing_prompt_renders_for_every_context(context: PreRoundPrompt, kind: str) -> None:
    assert _OBJECTIVE in _render(PromptTemplate.PRE_ROUND, context, profiler_kind=kind)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    context=_PROFILER,
    kind=st.sampled_from(sorted(kind.value for kind in ACTIVE_PROFILER_KINDS)),
)
def test_the_profiler_prompt_renders_for_every_runnable_profiler(
    context: ProfilerPrompt, kind: str
) -> None:
    text = _render(PromptTemplate.PROFILER, context, profiler_kind=kind)
    assert "Write nothing in the workspace" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_PLANNING)
def test_the_planning_prompt_shows_a_fresh_profile(
    context: PlanPrompt,
) -> None:
    text = _render(PlanTemplate.PLAN, context)
    if context.last_profile is not None:
        assert "fresh profiler result" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_PLANNING)
def test_the_planning_prompt_states_the_measurement_noise_or_that_it_is_not_known(
    context: PlanPrompt,
) -> None:
    """The designer sets its bar knowing how far a result must clear it, or that nobody knows."""
    text = _render(PlanTemplate.PLAN, context)
    found = context.measurement_noise
    if found is None:
        assert "Measurement noise: not yet measured" in text
    else:
        assert f"varies by {found.noise * 100:.2f}% of the base" in text
        assert f"more than {found.resolution * 100:.2f}% of the base" in text
        assert "not yet measured" not in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_IMPLEMENT)
def test_the_implementation_prompt_renders_for_every_context(context: ImplementPrompt) -> None:
    text = _render(PromptTemplate.IMPLEMENT, context)
    if context.continuation_step is None:
        assert context.plan.task in text
    if context.feedback is not None:
        assert context.feedback in text
    if context.validation_failure is not None and context.validation_failure.recipe_unusable:
        assert "could not be used" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_IMPLEMENT.filter(lambda item: item.continuation_step is not None))
def test_a_continuation_asks_for_the_bounded_step_the_implementer_named(
    context: ImplementPrompt,
) -> None:
    text = _render(PromptTemplate.IMPLEMENT, context)
    assert context.continuation_step is not None
    assert context.continuation_step in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_JUDGE)
def test_the_review_prompt_renders_for_every_context(context: JudgePrompt) -> None:
    text = _render(PromptTemplate.JUDGE, context)
    assert context.claims.summary in text
    if context.pareto_archive_conflict is not None:
        assert "conflicts with the live noise-aware archive" in text


@pytest.mark.parametrize("kind", sorted(kind.value for kind in ACTIVE_PROFILER_KINDS))
def test_every_runnable_profiler_has_a_template_in_the_shared_tree(kind: str) -> None:
    assert (PROMPTS_DIR / "shared" / "profilers" / f"{kind}.j2").is_file()


_MARKDOWN_LINE = st.from_regex(r"(- )?[a-z]{1,6}( [a-z]{1,6}){0,2}", fullmatch=True)
_MARKDOWN_PROSE = st.lists(_MARKDOWN_LINE, min_size=2, max_size=5).map("\n\n".join)
_REVISION = RevisionRef(revision_id=RevisionId(root="a1"), digest="git-commit:a1")
_GATES_OFF = GateFacts(revert_applied=False, revalidation_pending=False)


def _prose_plan(prose: str) -> HypothesisPlan:
    return HypothesisPlan(
        hypothesis_id="h1",
        title="t",
        hypothesis=prose,
        task=prose,
        pass_criteria=prose,
        invariants=prose,
    )


def _implement_context(plan: HypothesisPlan) -> ImplementPrompt:
    return ImplementPrompt(
        round_number=1,
        retry=1,
        plan=plan,
        base_revision=_REVISION,
        framework=_GATES_OFF,
        benchmark_enabled=True,
    )


def _judge_context(plan: HypothesisPlan, prose: str) -> JudgePrompt:
    return JudgePrompt(
        round_number=1,
        retry=1,
        plan=plan,
        claims=ImplementerClaims(
            summary=prose,
            expected_behavior=prose,
            status="implemented",
            evidence=prose,
            next_step=prose,
            candidate_disposition="unassessed",
        ),
        framework=_GATES_OFF,
        benchmark_enabled=True,
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(prose=_MARKDOWN_PROSE)
def test_a_multiline_markdown_field_of_an_earlier_agent_stays_inside_its_list_item(
    prose: str,
) -> None:
    plan = _prose_plan(prose)
    indented = [f"  {line}" if line else "" for line in prose.splitlines()]
    cases = (
        (
            _render(PromptTemplate.IMPLEMENT, _implement_context(plan)),
            ("Claim", "Task", "Pass criteria", "Invariants"),
        ),
        (
            _render(PromptTemplate.JUDGE, _judge_context(plan, prose)),
            (
                "Claim",
                "Task",
                "Pass criteria",
                "Invariants",
                "Summary",
                "Expected behavior",
                "Next step",
                "Evidence",
            ),
        ),
    )
    for text, labels in cases:
        lines = text.splitlines()
        for label in labels:
            start = lines.index(f"- {label}:")
            assert lines[start + 1 : start + 1 + len(indented)] == indented


@pytest.mark.parametrize(
    "template",
    [
        PromptTemplate.PRE_ROUND,
        PromptTemplate.PROFILER,
        PromptTemplate.IMPLEMENT,
        PromptTemplate.JUDGE,
    ],
)
def test_every_reply_prompt_tells_the_agent_its_prose_fields_are_markdown(
    template: PromptTemplate,
) -> None:
    plan = _prose_plan("claim")
    contexts: dict[PromptTemplate, Value] = {
        PromptTemplate.PRE_ROUND: PreRoundPrompt(round_number=1, has_history=False),
        PromptTemplate.PROFILER: ProfilerPrompt(round_number=1, profile_focus="focus"),
        PromptTemplate.IMPLEMENT: _implement_context(plan),
        PromptTemplate.JUDGE: _judge_context(plan, "claim"),
    }
    assert "as Markdown" in _render(template, contexts[template])


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_IMPLEMENT, offered=st.booleans())
def test_the_implementer_is_told_about_the_evaluation_tool_exactly_when_it_is_offered(
    context: ImplementPrompt, *, offered: bool
) -> None:
    """The role holds the tool; without its prompt naming it, no agent ever calls it."""
    text = _RENDERER.render_template(
        f"{PromptTemplate.IMPLEMENT.value}.j2",
        **_variables(context, agent_evaluation=offered),
    )
    assert ("submit_evaluation" in text) is offered
    assert ("Measure early, on the target" in text) is offered


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=_PLANNING)
def test_the_planning_prompt_states_what_the_implementer_can_read_of_an_evaluation(
    context: PlanPrompt,
) -> None:
    """A plan cannot demand evidence its implementer has no way to read (round 8 of a live run).

    The designer asked for a report quoting the benchmark's server log lines; the implementer
    can read only the evaluation's stage verdicts, metrics, summary tails and comparison, so it
    reported blocked and the round ended with no measurement.
    """
    text = _render(PlanTemplate.PLAN, context)
    assert "cannot read the benchmark's server or process logs" in text
    assert "the round ends without a measurement" in text
