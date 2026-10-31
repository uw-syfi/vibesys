"""Host wiring shared by strategies on the core lifecycle: gates, sandbox check and role kinds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
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
from vibesys.orchestration.dynamic.search.api import OPERATIONS as DYNAMIC
from vibesys.orchestration.evolve.search.api import OPERATIONS as EVOLVE
from vibesys.orchestration.input_check.operations import OPERATIONS as INPUT_CHECK
from vibesys.orchestration.issue_queue.search.api import OPERATIONS as ISSUE_QUEUE
from vibesys.orchestration.multi.search.api import OPERATIONS as MULTI
from vibesys.orchestration.single.search.api import OPERATIONS as SINGLE
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    EvidenceKind,
    MeasurementRecipe,
    RevisionId,
    RevisionRef,
    StageBudget,
)
from vs_runtime.api import AgentCapability, WorkspaceAccess
from vs_runtime.api.core import build_operation_catalog, operation_roles
from vs_runtime.api.infrastructure import ParallelCandidateObstacle
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_runtime.api.core import Cancelled, ExecutionContext, Inspection

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
        accuracy_gated=(EvidenceKind.BENCHMARK,),
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy=st.booleans(), benchmark=st.booleans())
def test_an_agent_measures_exactly_the_configured_gates(*, accuracy: bool, benchmark: bool) -> None:
    policy = agent_evaluation_policy(_recipe(), accuracy=accuracy, benchmark=benchmark)
    plan = policy.plan(CANDIDATE, measured_input="sha256:in", now_at=0.0)
    expected = [n for n, on in (("accuracy", accuracy), ("benchmark", benchmark)) if on]
    assert [stage.stage_id for stage in plan.stages] == expected
    assert plan.accuracy_stage == ("accuracy" if accuracy else None)
    assert plan.accuracy_gated == ((EvidenceKind.BENCHMARK,) if accuracy else ())
    # The agent's plan is the recipe's plan over those stages: one identity.
    assert plan == _recipe().plan(
        CANDIDATE, purpose="local-validation", now_at=0.0, stages=tuple(expected)
    ).model_copy(update={"measured_input": "sha256:in"})


@dataclass
class FakeEnvironment:
    env_kind: str
    parallel_candidate_obstacle: ParallelCandidateObstacle | None


def test_an_environment_that_opens_candidate_sandboxes_is_accepted() -> None:
    require_candidate_sandboxes(FakeEnvironment("docker", None), loop="dynamic")


def _refusal_text(obstacle: ParallelCandidateObstacle, *, loop: str = "serial") -> str:
    with pytest.raises(ConfigurationError) as raised:
        require_candidate_sandboxes(FakeEnvironment("local", obstacle), loop=loop)
    return str(raised.value)


def test_an_environment_that_cannot_is_refused_naming_the_loop_and_the_obstacle() -> None:
    text = _refusal_text(ParallelCandidateObstacle.NO_OWN_CONTAINER)
    assert "serial loop" in text
    assert "'local'" in text
    assert "its agent does not run in a container of its own" in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(st.sampled_from(ParallelCandidateObstacle), st.sampled_from(ParallelCandidateObstacle))
def test_every_declared_obstacle_has_its_own_stated_reason(
    left: ParallelCandidateObstacle, right: ParallelCandidateObstacle
) -> None:
    left_text, right_text = _refusal_text(left), _refusal_text(right)

    assert " because " in left_text
    assert (left_text == right_text) == (left is right)


def test_role_kinds_grow_from_reading_to_measuring_and_writing() -> None:
    assert READER.capabilities == frozenset()
    assert CONTINUING_READER.capabilities < MEASURING
    assert AgentCapability.MCP_SERVERS in MEASURING
    assert MEASURING_READER.capabilities == MEASURING_WRITER.capabilities == MEASURING
    assert MEASURING_READER.tools == MEASURING_WRITER.tools
    assert MEASURING_READER.access is WorkspaceAccess.READ_ONLY
    assert MEASURING_WRITER.access is WorkspaceAccess.READ_WRITE


class _AnyOwner:
    """An owner that does nothing: only the catalog's declaration of its operation is under test."""

    async def execute(self, request: OperationRequest, context: ExecutionContext) -> BaseModel:
        raise NotImplementedError

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        raise NotImplementedError

    async def cancel(self, request: OperationRequest, context: ExecutionContext) -> Cancelled:
        raise NotImplementedError


PRODUCTION_OPERATIONS = {
    "dynamic": DYNAMIC,
    "evolve": EVOLVE,
    "issue_queue": ISSUE_QUEUE,
    "multi": MULTI,
    "single": SINGLE,
    "input_check": INPUT_CHECK,
}


@pytest.mark.parametrize("name", PRODUCTION_OPERATIONS)
def test_every_operation_a_production_strategy_registers_declares_a_rerun_its_lifecycle_allows(
    name: str,
) -> None:
    """Owning an operation without a policy that fits its lifecycle is refused at the entry."""
    registrations = operation_roles(PRODUCTION_OPERATIONS[name])
    owners = {role: _AnyOwner() for role in registrations}
    catalog = build_operation_catalog(registrations, owners)
    for entry in catalog.entries:
        assert entry.rerun is not None, entry.schema.kind
