"""A run stored before stages could require outputs still matches its digests when resumed.

The digests below were computed by the code on main before ``required_outputs`` existed. A
stage that requires nothing must serialize byte-identically to that code, or every stored
intent, receipt, submission budget key and recipe digest stops matching and a run started
earlier cannot resume.
"""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest as digest

_CANDIDATE = core.RevisionRef(revision_id=core.RevisionId(root="c"), digest="digest-c")
_NAMES = ("accuracy", "benchmark", "profile")


def _recipe() -> core.MeasurementRecipe:
    return core.MeasurementRecipe(
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="r"),
        stages=tuple(
            core.StageBudget(stage_id=name, execution_seconds=seconds)
            for name, seconds in zip(_NAMES, (10.0, 20.0, 30.0), strict=True)
        ),
        queue_allowance=5.0,
        accuracy_stage="accuracy",
    )


def test_stages_that_require_nothing_keep_the_digests_stored_by_main() -> None:
    recipe = _recipe()
    plan = recipe.plan(_CANDIDATE, purpose="official", now_at=1.0)
    identity = core.MeasurementIdentity.from_plan(plan, _CANDIDATE)
    scope = core.Scope(owner=core.initial_state().run.run_id, generation=0)
    decision = core.Measure(decision_id=core.DecisionId(root="d"), scope=scope, plan=plan)
    request = core.MeasurementRequested(scope=scope, plan=plan)
    assert digest(recipe) == "a5af68801ca8c2ec7dae9e831874844b8c3be0611aaf0bc20083f1e2a5366564"
    assert digest(plan) == "a5e18155702d93b588379a327986f35304e3a7fb337c8d476e5f020ecfbb5260"
    assert digest(identity) == "90a714606a5a0ab0984d11502d70b1d75616bae4d5fd4e4f9b54ec8d50d8604f"
    assert digest(identity)[:24] == "90a714606a5a0ab0984d1150"  # the submission budget key
    assert digest(request) == "8604c1ab61451ba3fd312fdd7419c89d9c4b3cec5d0eeab6d2203479c94fb9ce"
    assert digest(decision) == "23ee8b64fece1477f051306e0a3444efcdd1dd1224812fe137f94e86da00f1ac"


@given(
    stage=st.sampled_from(_NAMES),
    outputs=st.lists(st.text(min_size=1, max_size=6), min_size=1, max_size=3, unique=True),
)
def test_a_required_output_changes_every_identity_and_survives_a_reload(
    stage: str, outputs: list[str]
) -> None:
    base = _recipe()
    required = base.requiring(stage, tuple(outputs))
    plan = required.plan(_CANDIDATE, purpose="official", now_at=1.0, stages=(stage,))
    unrequired = base.plan(_CANDIDATE, purpose="official", now_at=1.0, stages=(stage,))
    assert digest(required) != digest(base)
    assert digest(plan) != digest(unrequired)
    assert core.MeasurementIdentity.from_plan(
        plan, _CANDIDATE
    ) != core.MeasurementIdentity.from_plan(unrequired, _CANDIDATE)
    assert core.MeasurementPlan.model_validate_json(plan.model_dump_json()) == plan
    assert core.MeasurementRecipe.model_validate_json(required.model_dump_json()) == required


@given(stage=st.sampled_from(_NAMES))
def test_stored_text_without_the_field_loads_as_requiring_nothing(stage: str) -> None:
    recipe = _recipe()
    plan = recipe.plan(_CANDIDATE, purpose="official", now_at=1.0, stages=(stage,))
    text = plan.model_dump_json()
    assert "required_outputs" not in text
    assert core.MeasurementPlan.model_validate_json(text) == plan
