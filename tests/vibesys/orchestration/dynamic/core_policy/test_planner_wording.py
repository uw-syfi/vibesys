"""What the planner is told, in words: where a new workstream starts, how far a failed candidate got, what to fix.

The search sends typed facts; these tests render the planner's templates over them and check that
each fact the planner must act on is in the text: the base a parent-less workstream starts from, the
partial measurement of a candidate whose benchmark failed, and, for a reply that could not be
scheduled, the field and the value to fix (the reason of a provider that gave up on the schema
included).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    METRIC,
    PLANNER_PROMPTS,
    TURN,
    planner_fields,
    render,
    revision,
    tokens,
)

from vibesys.metrics import MetricRow, PartialRow
from vibesys.orchestration.dynamic.search.api import (
    PlannerCorrection,
    PlannerPrompt,
    PromptTemplate,
    ViolationCode,
)
from vs_core.api import EvidenceId
from vs_core.api.search import ReplyError, ReplyErrorCode
from vs_sim.api.testing import examples

PARTIALS = st.builds(
    PartialRow,
    name=tokens("measure"),
    value=st.integers(0, 10**6).map(float),
    direction=st.sampled_from(("max", "min")),
    unit=st.none() | tokens("u"),
    target=st.none() | st.integers(1, 10**6).map(float),
    completed=st.integers(0, 71),
    required=st.just(72),
    progress_unit=st.none() | tokens("p"),
)
"""What a benchmark that stopped early had measured, with the progress it reached."""


def where(path: tuple[str | int, ...]) -> str:
    """A field path as the planner reads it: ``workstreams[0].hypothesis_id``."""
    text = ""
    for step in path:
        text += f"[{step}]" if isinstance(step, int) else f"{'.' if text else ''}{step}"
    return text


def with_partial_parent(fields: dict[str, object], partial: PartialRow) -> PlannerPrompt:
    """A planning request that offers one retained candidate whose benchmark stopped at ``partial``."""
    buildable = {
        "option_id": "optionp",
        "hypothesis_id": "hyp-partial",
        "revision": revision("rev-p1"),
        "accuracy_evidence": EvidenceId(root="evp"),
        "submission_index": 1,
        "latest_verified": True,
        "best_partial": True,
        "partial": partial,
    }
    return PlannerPrompt.model_validate({**fields, "buildable": [buildable]}, strict=False)


# -- a candidate whose benchmark failed ------------------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(fields=planner_fields(), partial=PARTIALS)
def test_a_candidate_offered_as_a_parent_is_shown_with_how_far_its_failed_benchmark_got(
    fields: dict[str, object], partial: PartialRow
) -> None:
    # r14: every candidate failed the benchmark and the planner saw no number for any of them.
    # The history row's partial is `test_planner_partial_measurement.py`'s.
    text = render(PromptTemplate.PORTFOLIO, with_partial_parent(fields, partial))

    offered = next(line for line in text.splitlines() if line.startswith("- option `optionp`"))
    shown = f"partial {partial.name} = {partial.value}"
    assert shown in offered
    assert "best partial" in offered
    for fact in (partial.target, partial.unit, partial.progress_unit):
        assert fact is None or str(fact) in offered
    assert f"progress {partial.completed} of {partial.required}" in offered
    assert ("pass requires" in offered) is (partial.target is not None)


@settings(max_examples=examples(pr=8, nightly=100))
@given(fields=planner_fields(), metric=METRIC)
def test_a_passing_candidate_is_shown_by_its_metric_and_not_as_a_partial(
    fields: dict[str, object], metric: MetricRow
) -> None:
    buildable = {
        "option_id": "optionm",
        "hypothesis_id": "hyp-metric",
        "revision": revision("rev-m1"),
        "accuracy_evidence": EvidenceId(root="evm"),
        "submission_index": 1,
        "latest_verified": True,
        "best_partial": False,
        "metric": metric,
    }
    context = PlannerPrompt.model_validate({**fields, "buildable": [buildable]}, strict=False)

    text = render(PromptTemplate.PORTFOLIO, context)

    offered = next(line for line in text.splitlines() if line.startswith("- option `optionm`"))
    assert "; partial " not in offered
    assert f"{metric.name} = {metric.value}" in offered


# -- where a workstream without a parent starts ----------------------------------------------------


@settings(max_examples=examples(pr=8, nightly=100))
@given(context=PLANNER_PROMPTS)
def test_a_workstream_that_names_no_parent_is_told_it_starts_from_main(
    context: PlannerPrompt,
) -> None:
    # A new workstream starts from the run's main (the input until a merge moves it), never from
    # the best candidate so far: any other candidate is reachable only as a named parent.
    text = " ".join(render(PromptTemplate.PORTFOLIO, context).split())

    assert "Main revision (a new workstream that names no parent starts from it): " in text
    assert f"`{context.base_revision.revision_id.root}`" in text
    if context.main_revision == context.base_revision:
        assert "the base revision; no merge has moved it yet" in text
    else:
        assert f"`{context.main_revision.revision_id.root}`" in text
    assert "root or the best trusted candidate" not in text
    if context.buildable:
        assert "selects the main revision" in text
        assert "never silently starts from base" in text
    else:
        assert "leave `parent_hypothesis_id` null" in text


# -- a reply that could not be scheduled -----------------------------------------------------------


def violation_facts(
    code: ViolationCode, path: tuple[str | int, ...], word: str
) -> dict[str, object]:
    """A violation of ``code`` at ``path``, with the facts that rule names, each made of ``word``."""
    named: dict[str, object] = {"path": path, "code": code}
    match code:
        case ViolationCode.OVER_CAPACITY:
            named |= {"requested": 7, "capacity": 3}
        case ViolationCode.REVISION_WITHOUT_PARENT:
            named["usable"] = ({"hypothesis_id": f"{word}h", "revision": f"{word}r"},)
        case ViolationCode.REVISION_NOT_OFFERED:
            named |= {
                "subject": f"{word}s",
                "revision": f"{word}v",
                "usable": ({"hypothesis_id": f"{word}h", "revision": f"{word}r"},),
            }
        case ViolationCode.PROFILE_FIELDS_UNSUPPORTED:
            named |= {"fields": (f"{word}f",), "supplied": (f"{word}g",)}
        case (
            ViolationCode.CONTINUATION_HAS_PARENT
            | ViolationCode.PROFILE_NOT_OFFERED
            | ViolationCode.MERGE_IN_FLIGHT
            | ViolationCode.MERGE_UNCHECKABLE
        ):
            pass
        case _:
            named["subject"] = f"{word}s"
    return named


def facts_named(code: ViolationCode, word: str) -> list[str]:
    """The facts the planner must see for a violation of ``code`` built by ``violation_facts``."""
    match code:
        case ViolationCode.OVER_CAPACITY:
            return ["requested 7 workstreams, but capacity is 3"]
        case ViolationCode.REVISION_WITHOUT_PARENT:
            return [f"('{word}h', '{word}r')"]
        case ViolationCode.REVISION_NOT_OFFERED:
            return [f"'{word}v'", f"'{word}s'", f"('{word}h', '{word}r')"]
        case ViolationCode.PROFILE_FIELDS_UNSUPPORTED:
            return [f"{word}f", f"{word}g"]
        case (
            ViolationCode.CONTINUATION_HAS_PARENT
            | ViolationCode.PROFILE_NOT_OFFERED
            | ViolationCode.MERGE_IN_FLIGHT
            | ViolationCode.MERGE_UNCHECKABLE
        ):
            return []
        case _:
            return [f"'{word}s'"]


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    code=st.sampled_from(ViolationCode),
    path=st.lists(
        st.sampled_from(("workstreams", "implement", "hypothesis_id", "parent_revision", 0, 1)),
        min_size=1,
        max_size=4,
    ).map(tuple),
    word=tokens("zz"),
    fields=planner_fields(),
)
def test_a_correction_names_the_field_and_the_value_of_every_rule_the_reply_broke(
    code: ViolationCode, path: tuple[str | int, ...], word: str, fields: dict[str, object]
) -> None:
    problem = {"kind": "violations", "violations": (violation_facts(code, path, word),)}
    context = PlannerCorrection.model_validate(
        {**fields, "of": TURN, "problem": problem}, strict=False
    )

    text = render(PromptTemplate.PORTFOLIO_CORRECTION, context)

    (corrected,) = text.split("Correction required: ")[1:]
    assert corrected.startswith(f"{where(path)}: ")
    for fact in facts_named(code, word):
        assert fact in corrected


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    fields=planner_fields(),
    path=st.lists(
        st.sampled_from(("workstreams", "implement", "hypothesis_id", 0)), min_size=1
    ).map(tuple),
    reason=tokens("reason"),
    code=st.sampled_from(ReplyErrorCode),
)
def test_a_schema_error_is_corrected_at_its_field_with_the_validators_own_words(
    fields: dict[str, object], path: tuple[str | int, ...], reason: str, code: ReplyErrorCode
) -> None:
    # The validator that rejected an id (or the provider that gave up on the schema) says what is
    # wrong in its own words; the planner must read them to fix the reply.
    error = ReplyError(path=path, code=code, message=reason)
    problem = {"kind": "unparseable", "errors": (error,)}
    context = PlannerCorrection.model_validate(
        {**fields, "of": TURN, "problem": problem}, strict=False
    )

    text = render(PromptTemplate.PORTFOLIO_CORRECTION, context)

    assert "the reply violated the plan schema" in text
    line = next(line for line in text.splitlines() if line.startswith(f"- {where(path)}: "))
    assert line.endswith(f": {reason}")
    assert f"({code.value})" in line
