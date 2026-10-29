"""A comparison baseline's number reaches the planner only when its task opted in.

``visible_to_agents`` defaults to false. The host passes the planner the comparisons that are both
measured and visible, as data; a hidden one must appear in no rendered prompt, whatever its
reading, and only the planner's prompts show a visible one. These tests render every dynamic template with generated readings and look for the hidden
labels and numbers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    CONTEXTS,
    FRESH_TURNS,
    RENDERER,
    TEMPLATE_FILES,
    variables,
)

from vibesys.comparisons.api import ComparisonReport, visible_comparison_variables
from vibesys.orchestration.dynamic.search.api import PromptTemplate
from vs_runtime.api.comparisons import ComparisonMeasured, ComparisonNotMeasured, ComparisonSample
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

_PLANNER = (PromptTemplate.PORTFOLIO, PromptTemplate.PORTFOLIO_CORRECTION)

# A number and a label no template or generated context otherwise prints.
_VALUES = st.integers(min_value=1_000_001, max_value=9_999_999).map(lambda n: n + 0.25)


def _measured(value: float) -> ComparisonMeasured:
    return ComparisonMeasured(
        metric="tok", unit="tok/s", direction="max", samples=(ComparisonSample(value=value),)
    )


@st.composite
def _reports(draw: st.DrawFn) -> list[ComparisonReport]:
    rows = draw(st.lists(st.tuples(_VALUES, st.booleans(), st.booleans()), max_size=4))
    return [
        ComparisonReport(
            f"c{i}",
            f"Engine{i}Zq",
            _measured(value) if measured else ComparisonNotMeasured(),
            visible_to_agents=visible,
        )
        for i, (value, visible, measured) in enumerate(rows)
    ]


def _shown(report: ComparisonReport) -> bool:
    return report.visible_to_agents and isinstance(report.reading, ComparisonMeasured)


def _offered(reports: list[ComparisonReport]) -> list[Mapping[str, object]]:
    rows = visible_comparison_variables(reports).get("comparisons", [])
    return list(rows) if isinstance(rows, list) else []


@given(reports=_reports())
def test_only_measured_comparisons_that_opted_in_become_prompt_data(
    reports: list[ComparisonReport],
) -> None:
    assert [row["label"] for row in _offered(reports)] == [r.label for r in reports if _shown(r)]


@pytest.mark.parametrize("template", FRESH_TURNS, ids=lambda t: str(t.value))
@settings(max_examples=examples(pr=6, nightly=60), deadline=None)
@given(data=st.data())
def test_a_hidden_comparison_appears_in_no_rendered_prompt(
    template: PromptTemplate, data: st.DataObject
) -> None:
    reports = data.draw(_reports())
    context = data.draw(CONTEXTS[template])
    offered = _offered(reports)

    text = RENDERER.render_template(
        TEMPLATE_FILES[template],
        **{**variables(context), "comparisons": offered},
    )

    for report in reports:
        if isinstance(report.reading, ComparisonMeasured):
            number = f"{report.reading.value}"
            expected = _shown(report) and template in _PLANNER
            assert (report.label in text) is (number in text) is expected, template


@settings(max_examples=examples(pr=4, nightly=20), deadline=None)
@given(context=CONTEXTS[PromptTemplate.PORTFOLIO])
def test_a_comparison_that_opted_in_is_a_reference_line_in_the_planner_prompt(
    context: BaseModel,
) -> None:
    reports = [
        ComparisonReport("v", "vLLM", _measured(66.2), visible_to_agents=True),
        ComparisonReport("s", "SGLang", _measured(70.5)),
    ]

    text = RENDERER.render_template(
        TEMPLATE_FILES[PromptTemplate.PORTFOLIO],
        **{**variables(context), **visible_comparison_variables(reports)},
    )

    assert "- vLLM: 66.2 tok/s (better: max)" in text
    assert "SGLang" not in text
    assert "70.5" not in text
