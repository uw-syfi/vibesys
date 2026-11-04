"""A run stored before stages could require outputs still matches its digests when resumed.

The digests below were computed by the code on main before ``required_outputs`` existed, except
the plan's and the request's, which also drop ``deadline_at`` (a plan states no deadline). A
stage that requires nothing must serialize byte-identically to that code, or every stored
intent, receipt, submission budget key and recipe digest stops matching and a run started
earlier cannot resume. The digests were last re-pinned when the task's ``accuracy_gated``
trust declaration became part of the recipe, plan and identity: runs stored before that
declaration existed are refused rather than resumed.
"""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api import EvidenceKind

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
        accuracy_gated=(EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK),
    )


def test_stages_that_require_nothing_keep_the_digests_stored_by_main() -> None:
    recipe = _recipe()
    plan = recipe.plan(_CANDIDATE, purpose="official", now_at=1.0)
    identity = core.MeasurementIdentity.from_plan(plan, _CANDIDATE)
    scope = core.Scope(owner=core.initial_state().run.run_id, generation=0)
    decision = core.Measure(decision_id=core.DecisionId(root="d"), scope=scope, plan=plan)
    request = core.MeasurementRequested(decision_id=decision.decision_id, scope=scope, plan=plan)
    assert digest(recipe) == "508ccb3ff4dfe8a94b18c9584ca1e14bade087e56e45e9562f549da48f8edf4a"
    assert digest(plan) == "075c507fffa830d744b1cc5b937c275ab2a86d0022fd75d66bd8d085a2204b88"
    assert digest(identity) == "7ebd450cf7c5c10f8192b234b561e1acafe7afbd6cb50fcdf30858ea156bbeea"
    assert digest(identity)[:24] == "7ebd450cf7c5c10f8192b234"  # the submission budget key
    # The signal names the decision it answers (an answer core gives at once is addressed to it);
    # no run stores the signal, so its digest was re-pinned with the field.
    assert digest(request) == "63e655bccb6378345dacc4c4d32c310c65c92c218ac936ec0110a302d1b5e831"
    # A decision's own digest is not pinned: decisions no longer carry deadlines or
    # dependencies, so envelopes stored by main are refused rather than resumed (version 4).
    assert "required_outputs" not in decision.model_dump_json()


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
