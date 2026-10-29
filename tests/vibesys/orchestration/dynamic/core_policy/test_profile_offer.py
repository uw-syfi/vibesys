"""What the planner is offered for profile work is what the run's capture can supply.

A planner that is offered a measurement field the capture cannot supply wastes a whole
profile workstream on a request nothing can satisfy. The fields in the planner's prompt and in
plan validation both come from one value, the capture's supported fields, so these tests
generate that value and check the two agree.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._values import portfolio, profile_entry, run_record
from tests.vibesys.orchestration.dynamic.core_policy._contexts import (
    PLANNER_PROMPTS,
    TURN,
    render,
    revision,
)

from vibesys.orchestration.dynamic.core_policy.api import planner_reply_example
from vibesys.orchestration.dynamic.models import ProfileField, ProfileOffer
from vibesys.orchestration.dynamic.search.api import (
    PlannerCorrection,
    PlannerPrompt,
    PromptTemplate,
    dynamic,
    validate,
)
from vs_sim.api.testing import examples

_CAPABILITIES = st.lists(st.sampled_from(ProfileField), unique=True).map(tuple)


def _prompt(fields: tuple[ProfileField, ...], *, profiling: bool = True) -> PlannerPrompt:
    return PlannerPrompt(
        capacity=1,
        in_flight=0,
        remaining=2,
        base_revision=revision("base"),
        base_accuracy_passed=True,
        main_revision=revision("base"),
        offer_snapshot="snapshot",
        profiling=profiling,
        profile_fields=tuple(field.value for field in fields),
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(fields=_CAPABILITIES)
def test_the_planner_prompt_names_exactly_the_supported_fields(
    fields: tuple[ProfileField, ...],
) -> None:
    text = render(PromptTemplate.PORTFOLIO, _prompt(fields))

    for field in ProfileField:
        assert (field.value in text) is (field in fields)
    assert ("This run's capture supplies no named measurement field" in text) is not bool(fields)


@settings(max_examples=examples(pr=8, nightly=100))
@given(fields=_CAPABILITIES)
def test_a_correction_offers_the_same_fields_as_the_request_it_corrects(
    fields: tuple[ProfileField, ...],
) -> None:
    request = _prompt(fields)
    correction = PlannerCorrection.model_validate(
        {
            **request.model_dump(exclude={"template"}),
            "of": TURN,
            "problem": {"kind": "no-reply"},
        },
        strict=False,
    )

    asked = render(PromptTemplate.PORTFOLIO, request)
    corrected = render(PromptTemplate.PORTFOLIO_CORRECTION, correction)

    for field in ProfileField:
        assert (field.value in asked) is (field.value in corrected) is (field in fields)


@settings(max_examples=examples(pr=8, nightly=100))
@given(request=PLANNER_PROMPTS)
def test_a_run_without_profiling_offers_no_profile_work(request: PlannerPrompt) -> None:
    quiet = request.model_copy(update={"profiling": False})

    text = render(PromptTemplate.PORTFOLIO, quiet)

    assert "profile workstream" not in text
    assert "required_fields" not in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(fields=_CAPABILITIES)
def test_the_prompt_example_is_valid_whatever_the_offer(fields: tuple[ProfileField, ...]) -> None:
    text = render(PromptTemplate.PORTFOLIO, _prompt(fields))

    assert planner_reply_example() in text
    for role in dynamic.roles:
        if role.name == "orchestrator":
            role.reply.model_validate_json(planner_reply_example())


@settings(max_examples=examples(pr=8, nightly=100))
@given(supported=_CAPABILITIES, requested=_CAPABILITIES)
def test_a_plan_requiring_an_unsupported_field_is_a_violation_naming_it(
    supported: tuple[ProfileField, ...], requested: tuple[ProfileField, ...]
) -> None:
    plan = portfolio(profile_entry("p1", required_fields=[field.value for field in requested]))
    record = run_record()

    check = validate(
        plan, record.world().ledger, record.offered(), capacity=1, offer=ProfileOffer(supported)
    )

    unsupported = set(requested) - set(supported)
    assert check.valid is not bool(unsupported)
    for field in unsupported:
        assert any(field.value in item.fields for item in check.violations)
    if unsupported:
        assert [item.path for item in check.violations] == [("workstreams", 0, "required_fields")]
