"""An agent's measurement and a trusted one are planned from one recipe, so they share an identity."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    EvidenceKind,
    MeasurementRecipe,
    RevisionId,
    RevisionRef,
    StageBudget,
)
from vs_runtime.api.core import AgentEvaluationPolicy

CANDIDATE = RevisionRef(revision_id=RevisionId(root="c"), digest="digest-c")
_NAMES = ("accuracy", "benchmark")


@given(
    seconds=st.tuples(st.integers(0, 10_000).map(float), st.integers(0, 10_000).map(float)),
    queue=st.integers(0, 1_000).map(float),
    now=st.integers(0, 1_000_000).map(float),
    gated=st.booleans(),
)
def test_the_agent_plan_is_the_recipes_plan(
    seconds: tuple[float, float], queue: float, now: float, *, gated: bool
) -> None:
    recipe = MeasurementRecipe(
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="r"),
        stages=tuple(
            StageBudget(stage_id=name, execution_seconds=s)
            for name, s in zip(_NAMES, seconds, strict=True)
        ),
        queue_allowance=queue,
        accuracy_stage="accuracy" if gated else None,
        accuracy_gated=(EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK),
    )
    policy = AgentEvaluationPolicy.from_recipe(recipe)
    assert policy.plan(CANDIDATE, measured_input="sha256:in", now_at=now) == recipe.plan(
        CANDIDATE, purpose="local-validation", now_at=now
    ).model_copy(update={"measured_input": "sha256:in"})
    assert policy.measurement == recipe
