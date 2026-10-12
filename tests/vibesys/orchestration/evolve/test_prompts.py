"""Every prompt the evolve strategy can request renders from the core policy's templates.

The strategy sends a typed `PromptContext`; the render owner passes its fields (plus the
run-level variables) to the template under strict undefined variables. A template that reads a
name no context supplies fails the render, which ends the turn, so each template is exercised
over its context space, and the profiler template over every runnable profiler.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from tests.support.prompt_context import context_variables
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS

import vibesys.orchestration.evolve.core_policy.prompts as templates
from vibesys.orchestration.evolve.core_policy.api import reply_schemas, run_prompt_variables
from vibesys.orchestration.evolve.population import Individual
from vibesys.orchestration.evolve.strategy.api import (
    JudgePrompt,
    MutatePrompt,
    ObjectiveAxis,
    ProfilerPrompt,
    PromptTemplate,
)
from vibesys.orchestration.profilers import ACTIVE_PROFILER_KINDS
from vibesys.prompts import CORE_PROTOCOL_DIR, PROMPTS_DIR
from vs_core.api import RevisionId, RevisionRef, Value
from vs_prompts.api import TemplateRenderer

_ROOT = Path(templates.__file__).parent
_OBJECTIVE = "Raise queue throughput."
_RENDERER = TemplateRenderer(_ROOT, fallback_roots=(CORE_PROTOCOL_DIR, PROMPTS_DIR / "shared"))
_TEXT = st.text(alphabet="abcdefg hij", min_size=1, max_size=12)
_REVISIONS = st.sampled_from(("a1", "b2")).map(
    lambda name: RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")
)
_INDIVIDUALS = st.builds(
    Individual,
    id=st.integers(1, 9),
    generation=st.integers(0, 4),
    perf_metric=st.none() | st.floats(0, 1e6),
    perf_unit=st.none() | _TEXT,
    metrics=st.lists(st.tuples(_TEXT, st.floats(0, 1e6)), max_size=2).map(tuple),
    passed=st.booleans().filter(bool),
    summary=_TEXT,
    feedback=st.text(max_size=12),
)
_COLD = st.builds(
    MutatePrompt,
    base_revision=_REVISIONS,
    is_cold_start=st.booleans().filter(bool),
    repair_seed=st.booleans(),
    failed_lessons=st.lists(_TEXT, max_size=3).map(tuple),
    num_failed_attempts=st.integers(0, 4),
    reference_path=st.sampled_from((".", "reference")),
)
_CHILD = st.builds(
    MutatePrompt,
    base_revision=_REVISIONS,
    is_cold_start=st.booleans().filter(lambda value: not value),
    parent=_INDIVIDUALS,
    inspirations=st.lists(_INDIVIDUALS, max_size=2).map(tuple),
)
_JUDGE = st.builds(JudgePrompt, candidate=_REVISIONS)
_PROFILER = st.builds(
    ProfilerPrompt,
    candidate=_REVISIONS,
    profiler_kind=st.sampled_from(sorted(kind.value for kind in ACTIVE_PROFILER_KINDS)),
    profile_execution=st.sampled_from(("local", "remote")),
    benchmark_command=st.none() | _TEXT,
    objectives=st.lists(
        st.builds(ObjectiveAxis, name=_TEXT, direction=st.sampled_from(("max", "min"))),
        max_size=2,
        unique_by=lambda axis: axis.name,
    ).map(tuple),
    profiler_support_name=_TEXT,
    profiler_mcp_name=_TEXT,
)


def _render(template: PromptTemplate, context: Value, *, agent_evaluation: bool = True) -> str:
    variables = context_variables(context, reply_schemas())
    text = _RENDERER.render_template(
        f"{template.value}.j2",
        **run_prompt_variables(
            domain_sections=EMPTY_DOMAIN_SECTIONS,
            objective=_OBJECTIVE,
            environment_notes="note",
            agent_evaluation=agent_evaluation,
        ),
        **variables,
    )
    assert text.strip()
    return text


@given(context=_COLD | _CHILD)
def test_the_mutation_prompt_renders_for_every_context(context: MutatePrompt) -> None:
    text = _render(PromptTemplate.MUTATE, context)
    assert _OBJECTIVE in text
    assert ("Bootstrap the first passing seed" in text) is context.is_cold_start
    if context.parent is not None:
        assert f"#{context.parent.id}" in text


@given(context=_CHILD)
def test_the_mutation_prompt_shows_every_inspiration(context: MutatePrompt) -> None:
    text = _render(PromptTemplate.MUTATE, context)
    for item in context.inspirations:
        assert f"Individual #{item.id}" in text


@given(context=_JUDGE)
def test_the_review_prompt_renders_for_every_context(context: JudgePrompt) -> None:
    assert context.candidate.revision_id.root in _render(PromptTemplate.JUDGE, context)


@given(context=_PROFILER)
def test_the_profiler_prompt_renders_for_every_runnable_profiler(context: ProfilerPrompt) -> None:
    text = _render(PromptTemplate.PROFILER, context)
    assert context.candidate.revision_id.root in text
    for axis in context.objectives:
        assert axis.name in text


def test_the_evaluation_tool_is_explained_when_the_run_offers_it() -> None:
    context = MutatePrompt(
        base_revision=RevisionRef(revision_id=RevisionId(root="a1"), digest="git-commit:a1"),
        is_cold_start=True,
    )
    assert "submit_evaluation" in _render(PromptTemplate.MUTATE, context)
    assert "submit_evaluation" not in _render(
        PromptTemplate.MUTATE, context, agent_evaluation=False
    )
