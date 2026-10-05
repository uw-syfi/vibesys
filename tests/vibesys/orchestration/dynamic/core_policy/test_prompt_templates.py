"""Every prompt the strategy can request renders from the core policy's templates.

The strategy sends a typed `PromptContext`; the render owner passes its fields (plus the
run-level `objective`) to the template under strict undefined variables. A template that
reads a name no context supplies fails the render, which the planner sees as "prompt
render failed" and ends the run, so each template is exercised over its context space.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vibesys.orchestration.dynamic.core_policy.prompts as templates
from vibesys.orchestration.dynamic.strategy.api import (
    EvidenceCitation,
    ImplementPrompt,
    MetricRow,
    PlannerCorrectionPrompt,
    PlannerPrompt,
    ProfilePrompt,
    PromptContext,
    PromptTemplate,
    ReplyCorrectionPrompt,
    ResumePrompt,
    ReviewPrompt,
)
from vs_core.api import RevisionId, RevisionRef
from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from pydantic import BaseModel

_ROOT = Path(templates.__file__).parent
_OBJECTIVE = "Raise queue throughput."


def _revision(name: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=name), digest=f"git-commit:{name}")


_REVISIONS = st.sampled_from(("a1", "b2", "c3")).map(_revision)
_TEXT = st.text(alphabet="abcdefg hij", min_size=1, max_size=12)
_CITATIONS = st.lists(
    st.builds(EvidenceCitation, location=_TEXT, revision=st.none() | _TEXT, purpose=_TEXT),
    max_size=2,
).map(tuple)
_METRICS = st.lists(
    st.builds(
        MetricRow,
        name=_TEXT,
        value=st.floats(min_value=0, max_value=1e6),
        direction=st.sampled_from(("max", "min")),
        unit=st.none() | _TEXT,
    ),
    max_size=2,
).map(tuple)
_PLANNER = st.builds(
    PlannerPrompt,
    capacity=st.integers(0, 3),
    in_flight=st.integers(0, 3),
    remaining=st.integers(0, 9),
    base_revision=_REVISIONS,
    base_accuracy_passed=st.none() | st.booleans(),
    offer_snapshot=_TEXT,
    older_ids=st.lists(_TEXT, max_size=2).map(tuple),
    baseline=_METRICS,
    input_failure=st.none() | _TEXT,
    profiling=st.booleans(),
)
_CONTEXTS: dict[PromptTemplate, st.SearchStrategy[PromptContext]] = {
    PromptTemplate.PORTFOLIO: _PLANNER,
    PromptTemplate.PORTFOLIO_CORRECTION: st.builds(
        PlannerCorrectionPrompt,
        planner=_PLANNER,
        error=st.none() | _TEXT,
        scheduled=st.integers(0, 3),
    ),
    PromptTemplate.IMPLEMENT: st.builds(
        ImplementPrompt,
        hypothesis_id=_TEXT,
        hypothesis=_TEXT,
        task=_TEXT,
        pass_criteria=_TEXT,
        parent_revision=_REVISIONS,
        evidence=_CITATIONS,
        worktree_revision=st.none() | _REVISIONS,
        prior_revision=st.none() | _REVISIONS,
        feedback=st.none() | _TEXT,
    ),
    PromptTemplate.REVIEW: st.builds(
        ReviewPrompt,
        hypothesis_id=_TEXT,
        hypothesis=_TEXT,
        pass_criteria=_TEXT,
        candidate=_REVISIONS,
        summary=_TEXT,
        evidence=_CITATIONS,
    ),
    PromptTemplate.PROFILE_REQUEST: st.builds(
        ProfilePrompt, question=_TEXT, required_fields=st.just(()), target=_REVISIONS
    ),
    PromptTemplate.RESUME: st.builds(
        ResumePrompt,
        role=st.sampled_from(("implementer", "judge", "profiler")),
        retained_revision=st.none() | _REVISIONS,
        timed_out=st.booleans(),
        repeated_failure=st.none() | _TEXT,
    ),
    PromptTemplate.REPLY_CORRECTION: st.builds(
        ReplyCorrectionPrompt,
        role=st.sampled_from(("planner", "implementer", "judge", "profiler")),
        error=_TEXT,
    ),
}


def _variables(context: BaseModel) -> dict[str, object]:
    variables = {name: getattr(context, name) for name in type(context).model_fields}
    variables.pop("template")
    return {"objective": _OBJECTIVE, **variables}


def test_every_requestable_template_has_a_context_generator() -> None:
    assert set(_CONTEXTS) == set(PromptTemplate)


@pytest.mark.parametrize("template", list(PromptTemplate), ids=lambda item: item.value)
@given(data=st.data())
def test_a_template_renders_for_every_context_of_its_kind(
    template: PromptTemplate, data: st.DataObject
) -> None:
    context = data.draw(_CONTEXTS[template])
    renderer = TemplateRenderer(_ROOT)

    text = renderer.render_template(f"{template.value}.j2", **_variables(context))

    assert text.strip()
    if template in (PromptTemplate.PORTFOLIO, PromptTemplate.IMPLEMENT, PromptTemplate.REVIEW):
        assert _OBJECTIVE in text
