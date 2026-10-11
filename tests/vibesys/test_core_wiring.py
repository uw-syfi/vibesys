"""Host wiring shared by strategies on the core lifecycle: gates, sandbox check and role kinds."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.core_roles import (
    CONTINUING_READER,
    MEASURING,
    MEASURING_READER,
    MEASURING_WRITER,
    READER,
)
from vibesys.core_wiring import agent_evaluation_policy, require_candidate_sandboxes
from vibesys.errors import ConfigurationError
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    MeasurementRecipe,
    RevisionId,
    RevisionRef,
    StageBudget,
)
from vs_runtime.api import AgentCapability, WorkspaceAccess

CANDIDATE = RevisionRef(revision_id=RevisionId(root="c"), digest="d")


def _recipe() -> MeasurementRecipe:
    return MeasurementRecipe(
        evaluator_digest="e",
        workload_digest="w",
        environment_digest="n",
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="r"),
        stages=(
            StageBudget(stage_id="accuracy", execution_seconds=10.0),
            StageBudget(stage_id="benchmark", execution_seconds=20.0),
            StageBudget(stage_id="profile", execution_seconds=30.0),
        ),
        queue_allowance=5.0,
        accuracy_stage="accuracy",
    )


@given(accuracy=st.booleans(), benchmark=st.booleans())
def test_an_agent_measures_exactly_the_configured_gates(*, accuracy: bool, benchmark: bool) -> None:
    policy = agent_evaluation_policy(_recipe(), accuracy=accuracy, benchmark=benchmark)
    plan = policy.plan(CANDIDATE, now_at=0.0)
    expected = [n for n, on in (("accuracy", accuracy), ("benchmark", benchmark)) if on]
    assert [stage.stage_id for stage in plan.stages] == expected
    assert plan.accuracy_stage == ("accuracy" if accuracy else None)
    # The agent's plan is the recipe's plan over those stages: one identity.
    assert plan == _recipe().plan(
        CANDIDATE, purpose="local-validation", now_at=0.0, stages=tuple(expected)
    )


@dataclass
class FakeEnvironment:
    env_kind: str
    parallel_candidate_obstacle: str | None


def test_an_environment_that_opens_candidate_sandboxes_is_accepted() -> None:
    require_candidate_sandboxes(FakeEnvironment("docker", None), loop="dynamic")


def test_an_environment_that_cannot_is_refused_naming_the_loop_and_the_obstacle() -> None:
    environment = FakeEnvironment("local", "it has one working tree")
    with pytest.raises(ConfigurationError) as raised:
        require_candidate_sandboxes(environment, loop="serial")
    text = str(raised.value)
    assert "serial loop" in text
    assert "'local'" in text
    assert "it has one working tree" in text


def test_role_kinds_grow_from_reading_to_measuring_and_writing() -> None:
    assert READER.capabilities == frozenset()
    assert CONTINUING_READER.capabilities < MEASURING
    assert AgentCapability.MCP_SERVERS in MEASURING
    assert MEASURING_READER.capabilities == MEASURING_WRITER.capabilities == MEASURING
    assert MEASURING_READER.tools == MEASURING_WRITER.tools
    assert MEASURING_READER.access is WorkspaceAccess.READ_ONLY
    assert MEASURING_WRITER.access is WorkspaceAccess.READ_WRITE
