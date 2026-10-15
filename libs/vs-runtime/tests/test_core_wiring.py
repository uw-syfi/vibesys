"""Public composition contract for the runtime's core executor bindings."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vs_core.api import (
    Access,
    EnsureSession,
    RequestId,
    RoleId,
    RunId,
    Scope,
    SessionId,
    SessionSpec,
)
from vs_project.api import Project
from vs_runtime.api import PollingEvaluationExecutor
from vs_runtime.api.core import (
    ExecutorRefusal,
    ExecutorRole,
    MeasurementServices,
    core_bindings,
)
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import InlineBlockingRunner, ManualClock, Sim

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import RuntimeWorkspaces


def test_bindings_without_sessions_return_the_typed_session_refusal(
    tmp_path: Path, sim: Sim
) -> None:
    workspaces = cast(
        "RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace(path=tmp_path / "workspace"))
    )
    bindings = core_bindings(
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

    refusal = bindings.executors.refusal(request)

    assert isinstance(refusal, ExecutorRefusal)
    assert refusal.request_id == request.request_id
    assert refusal.role is ExecutorRole.SESSIONS
    assert refusal.detail == ("sessions executor not bound; owning lane must supply implementation")
