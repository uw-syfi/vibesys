"""The measurement contract validates graph proofs and separates bounds from input."""

from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st
from protovalidate import ValidationError

from vs_core.api import MeasurementStage, RevisionRef
from vs_core.api.replay import classify_context
from vs_proto.api import core_leaves, validate
from vs_runtime.api.core_values import to_proto
from vs_runtime.api.replay import replay_measurement_plan
from vs_runtime.api.slurm_evaluation_stack import plan


@given(edges=st.lists(st.integers(min_value=0, max_value=255), min_size=1, max_size=8))
def test_dependency_witness_validates_acyclic_graph_and_rejects_a_back_edge(
    edges: list[int],
) -> None:
    stages = tuple(
        MeasurementStage(
            stage_id=f"stage-{index}",
            depends_on=tuple(f"stage-{parent}" for parent in range(index) if bits & (1 << parent)),
            execution_budget=1,
        )
        for index, bits in enumerate(edges)
    )
    native = replay_measurement_plan(plan().model_copy(update={"stages": stages}))
    validate(native)
    original = classify_context(native, contract="measurement.v1")
    policy = deepcopy(native)
    policy.queue_allowance += 1
    for stage in policy.stages:
        stage.execution_seconds += 1
        stage.dependency_rank += 1
    changed_policy = classify_context(policy, contract="measurement.v1")
    assert original.identity == changed_policy.identity
    assert original.content != changed_policy.content
    revision = to_proto(RevisionRef.of_git_commit("different"))
    assert isinstance(revision, core_leaves.RevisionRef)
    policy.candidate = revision
    assert classify_context(policy, contract="measurement.v1").identity != original.identity
    native.stages[0].depends_on = [native.stages[-1].id]
    with pytest.raises(ValidationError, match="invalid ReplayMeasurementPlan"):
        validate(native)
