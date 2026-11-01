"""Public composition contract for the runtime's core executors."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from tests.support.skeleton_strategy import DIGEST, SkeletonStrategy

from vs_core.api import (
    Access,
    ContractError,
    EnsureSession,
    LifecycleClass,
    OperationSchemaRef,
    RequestId,
    RoleId,
    RunFacts,
    RunId,
    SchemaRef,
    Scope,
    SessionId,
    SessionSpec,
)
from vs_core.api.testing import sample_limits
from vs_project.api import Project
from vs_runtime.api import PollingEvaluationExecutor
from vs_runtime.api.core import (
    CoreStartup,
    ExecutorRefusal,
    ExecutorRole,
    MeasurementServices,
    core_executors,
    empty_catalog,
    new_core_state,
    revision_ref,
)
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import InlineBlockingRunner, ManualClock, Sim

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import RuntimeWorkspaces


def test_executors_without_sessions_return_the_typed_session_refusal(
    tmp_path: Path, sim: Sim
) -> None:
    workspaces = cast(
        "RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace(path=tmp_path / "workspace"))
    )
    wired = core_executors(
        receipts=Project.open(tmp_path).state.state_store_namespace("run"),
        workspaces=workspaces,
        measurement=MeasurementServices(
            PollingEvaluationExecutor(FakeEvaluation(), workspaces, clock=ManualClock())
        ),
        sessions=None,
        threads=sim.threads(),
        blocking=InlineBlockingRunner(),
    )
    request = EnsureSession(
        request_id=RequestId(root="ensure-without-sessions"),
        scope=Scope(owner=RunId(root="zero-agent-run"), generation=0),
        spec=SessionSpec(
            session_id=SessionId(root="unavailable-session"),
            role_id=RoleId(root="implementer"),
            policy="fresh",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
    )

    refusal = wired.executors.refusal(request)

    assert isinstance(refusal, ExecutorRefusal)
    assert refusal.request_id == request.request_id
    assert refusal.role is ExecutorRole.SESSIONS
    assert refusal.detail == ("sessions executor not bound; owning lane must supply implementation")


def test_a_declaration_requiring_an_unoffered_operation_is_refused_by_name() -> None:
    declaration = SkeletonStrategy().declaration.model_copy(
        update={
            "required_operations": (
                OperationSchemaRef(
                    kind="needs-this-operation",
                    request_schema=SchemaRef(name="request", version=1),
                    outcome_schema=SchemaRef(name="outcome", version=1),
                    lifecycle=LifecycleClass.QUERY,
                ),
            )
        }
    )
    facts = RunFacts(
        objective="x",
        baseline=revision_ref("0" * 40),
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
    )

    with pytest.raises(ContractError, match="required operation unavailable") as refused:
        new_core_state(
            "run",
            facts,
            declaration,
            offered=empty_catalog(),
            startup=CoreStartup(deadline_at=10.0, limits=sample_limits()),
        )

    assert "needs-this-operation" in str(refused.value)
