"""What the planner is offered for profile work is what the run's capture can supply.

A planner that is offered a measurement field the capture cannot supply wastes a whole
profile workstream on a request nothing can satisfy. The fields in the planner's reply
schema, in its prompt, and in plan validation all come from one value, the capture's
supported fields, so these tests generate that value and check all three agree.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st
from tests.vibesys.domain_sections import EMPTY_DOMAIN_SECTIONS
from tests.vibesys.orchestration.dynamic.prompt_variables import context_variables
from tests.vibesys.orchestration.dynamic.strategy._replies import plan_reply, profile
from tests.vibesys.orchestration.dynamic.strategy._run import config

import vibesys.orchestration.dynamic.core_policy.prompts as templates
from vibesys.orchestration.dynamic.core_policy.api import (
    planner_reply_example,
    reply_schemas,
    run_prompt_variables,
)
from vibesys.orchestration.dynamic.models import PortfolioPlan, ProfileOffer, planner_response_type
from vibesys.orchestration.dynamic.strategy.api import (
    PLANNER_REPLY,
    DynamicStrategyState,
    PlannerCorrectionPrompt,
    PlannerPrompt,
    validate,
)
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import RevisionId, RevisionRef
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ProfileField

_RENDERER = TemplateRenderer(Path(templates.__file__).parent, fallback_roots=(CORE_PROTOCOL_DIR,))
_CAPABILITIES = st.lists(st.sampled_from(ProfileField), unique=True).map(tuple)


def _offered_by_schema(fields: tuple[ProfileField, ...]) -> set[str]:
    schema = reply_schemas(config(profiling=True, profile_fields=fields))[
        PLANNER_REPLY
    ].model_json_schema()
    required = schema["$defs"]["ProfileDecision"]["properties"]["required_fields"]
    if fields:
        return set(schema["$defs"]["ProfileField"]["enum"])
    assert required["maxItems"] == 0
    assert "ProfileField" not in schema["$defs"]
    return set()


@given(fields=_CAPABILITIES)
def test_the_planner_schema_offers_exactly_the_supported_fields(
    fields: tuple[ProfileField, ...],
) -> None:
    assert _offered_by_schema(fields) == {field.value for field in fields}


@given(fields=_CAPABILITIES, revisions=st.sets(st.sampled_from(("a1", "b2")), max_size=2))
def test_parsing_binds_the_same_offer_the_schema_shows(
    fields: tuple[ProfileField, ...], revisions: set[str]
) -> None:
    reply = planner_response_type(tuple(revisions), ProfileOffer(fields))

    schema = reply.model_json_schema()

    assert set(schema["$defs"].get("ProfileField", {}).get("enum", ())) == {f.value for f in fields}


def _prompt(fields: tuple[ProfileField, ...]) -> PlannerPrompt:
    return PlannerPrompt(
        capacity=1,
        in_flight=0,
        remaining=2,
        base_revision=RevisionRef(revision_id=RevisionId(root="a1"), digest="git-commit:a1"),
        base_accuracy_passed=True,
        offer_snapshot="snapshot",
        profiling=True,
        profile_fields=fields,
    )


def _variables(context: PlannerPrompt | PlannerCorrectionPrompt) -> dict[str, object]:
    variables = context_variables(context)
    return {
        **run_prompt_variables(
            domain_sections=EMPTY_DOMAIN_SECTIONS,
            objective="Raise throughput.",
            environment_notes="",
            agent_evaluation=False,
        ),
        **variables,
    }


@given(fields=_CAPABILITIES)
def test_the_planner_prompt_names_exactly_the_supported_fields(
    fields: tuple[ProfileField, ...],
) -> None:
    prompt = _prompt(fields)
    correction = PlannerCorrectionPrompt(planner=prompt, error=None, scheduled=0)
    texts = [
        _RENDERER.render_template("portfolio.j2", **_variables(prompt)),
        _RENDERER.render_template("portfolio_correction.j2", **_variables(correction)),
    ]
    for text in texts:
        for field in ProfileField:
            assert (field.value in text) is (field in fields)


@given(fields=_CAPABILITIES)
def test_the_prompt_example_is_valid_for_every_offer(fields: tuple[ProfileField, ...]) -> None:
    planner_response_type((), ProfileOffer(fields)).model_validate_json(planner_reply_example())


@given(supported=_CAPABILITIES, requested=_CAPABILITIES)
def test_a_plan_requiring_an_unsupported_field_is_a_violation_naming_it(
    supported: tuple[ProfileField, ...], requested: tuple[ProfileField, ...]
) -> None:
    plan = PortfolioPlan.model_validate_json(
        plan_reply(profile("p1", required_fields=[field.value for field in requested]))
    )

    check = validate(plan, DynamicStrategyState(), (), capacity=1, offer=ProfileOffer(supported))

    unsupported = set(requested) - set(supported)
    assert check.valid is not bool(unsupported)
    for field in unsupported:
        assert any(field.value in item.detail for item in check.violations)
    if unsupported:
        assert [item.path for item in check.violations] == ["workstreams[0].required_fields"]
