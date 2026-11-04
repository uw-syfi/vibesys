"""A benchmark that stopped reaches the planner as the rate it achieved, not only as prose.

Regression for r19: ten warmups stopped at 7 to 16 tokens/s and every one reached the planner
with no partial measurement, so the planner could not tell a candidate close to the bar from one
far from it. The harness writes the achieved rate, the rate that passes and the progress as typed
values (`libs/vs-runtime/tests/test_trusted_benchmark_partial.py` decodes them, and
`tests/examples/test_qwen35_mi210_warmup_stop.py` parses the bundle's own stop text into them);
the planner's prompt must state every one of them.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    planner_fields,
    render,
    revision,
    tokens,
)

from vibesys.metrics import PartialRow
from vibesys.orchestration.dynamic.search.api import PlannerPrompt, PromptTemplate
from vs_sim.api.testing import examples

_MINIMAL_PLAN: dict[str, object] = {
    "capacity": 1,
    "in_flight": 0,
    "remaining": 1,
    "base_revision": revision("rev-a1"),
    "base_accuracy_passed": True,
    "main_revision": revision("rev-a1"),
    "offer_snapshot": "snap",
}
_VALUES = st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False)
_PARTIALS = st.builds(
    PartialRow,
    name=tokens("rate"),
    value=_VALUES,
    direction=st.sampled_from(("max", "min")),
    unit=st.none() | tokens("unit"),
    target=st.none() | _VALUES,
    completed=st.none() | st.integers(0, 10**4),
    required=st.none() | st.integers(1, 10**4),
    progress_unit=st.none() | tokens("prog"),
)


def _statement(partial: PartialRow) -> str:
    """What the prompt says of a partial measurement."""
    text = f"{partial.name} = {partial.value}"
    text += f" {partial.unit}" if partial.unit else ""
    text += f" (better: {partial.direction})"
    if partial.target is not None:
        text += f", pass requires {partial.target}"
    if partial.completed is not None and partial.required is not None:
        text += f", progress {partial.completed} of {partial.required}"
        text += f" {partial.progress_unit}" if partial.progress_unit else ""
    return text


def _rendered(partial: PartialRow, fields: dict[str, object]) -> str:
    """The planner prompt of a run whose one workstream ended with ``partial`` measured."""
    row = {
        "hypothesis_id": "hyp0",
        "sequence": 1,
        "title": "title0",
        "status": "finished",
        "strategy": "available",
        "partial": partial,
    }
    context = PlannerPrompt.model_validate({**fields, "history": [row]}, strict=False)
    return render(PromptTemplate.PORTFOLIO, context)


@settings(max_examples=examples(pr=8, nightly=100))
@given(partial=_PARTIALS, data=st.data())
def test_the_planner_is_told_what_a_stopped_benchmark_achieved_and_what_it_had_to_reach(
    partial: PartialRow, data: st.DataObject
) -> None:
    text = _rendered(partial, data.draw(planner_fields()))

    assert f"partial {_statement(partial)}" in text


def test_r19s_recorded_warmup_is_told_as_its_rate_the_rate_it_needed_and_its_progress() -> None:
    # r19's eval_eb00846: 1246 output tokens in 176 s, 9 of 72 rounds, against 14343 tokens in 180 s.
    recorded = PartialRow(
        name="warmup_output_tokens_per_s",
        value=1246 / 176,
        direction="max",
        unit="output tokens/s",
        target=14343 / 180,
        completed=9,
        required=72,
        progress_unit="rounds",
    )

    text = _rendered(recorded, _MINIMAL_PLAN)

    assert "partial warmup_output_tokens_per_s = 7.07954" in text
    assert "output tokens/s (better: max), pass requires 79.683" in text
    assert "progress 9 of 72 rounds" in text
