"""Scripted executors: what the planner, implementer, judge and evaluators answer."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.strategy.api import (
    ParentVerification,
    RenderedArtifacts,
    RenderRoleArtifacts,
    VerifyParentRevision,
    dynamic_operation_registry,
)
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    EnsureSession,
    ExecuteRegisteredOperation,
    Request,
    ResourceId,
    SubmitMeasurement,
)
from vs_core.testing.drive import Answer, Running, Succeeded, Unknown

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CoreState


@dataclass
class Executors:
    """Scripted answers for one run, popped in the order requests are authorized."""

    planner: deque[str] = field(default_factory=deque)
    implementer: deque[str] = field(default_factory=deque)
    judge: deque[str] = field(default_factory=deque)
    parent_verified: Callable[[str], bool] = lambda _revision: True
    submit: Callable[[SubmitMeasurement], Answer] = lambda request: Running(
        resource_id=ResourceId(root=f"job:{request.request_id.root}")
    )
    seen: list[Request] = field(default_factory=list)

    def __call__(self, request: Request, _core: CoreState) -> Answer | tuple[Answer, ...]:
        self.seen.append(request)
        if isinstance(request, ExecuteRegisteredOperation):
            return self._operation(request)
        if isinstance(request, SubmitMeasurement):
            return self.submit(request)
        if isinstance(request, DispatchTurn):
            return self._turn(request)
        if isinstance(request, EnsureSession):
            return Succeeded(resource_id=ResourceId(root=f"lease:{request.spec.session_id.root}"))
        return Succeeded()

    def _turn(self, request: DispatchTurn) -> Answer:
        role = request.turn.session.role_id.root.removeprefix("dynamic-")
        queue = {
            "orchestrator": self.planner,
            "implementer": self.implementer,
            "judge": self.judge,
        }[role]
        if not queue:
            return Unknown()
        return Succeeded(output_json=queue.popleft())

    def _operation(self, request: ExecuteRegisteredOperation) -> Answer:
        codec = dynamic_operation_registry()
        typed = codec.decode(request.operation)
        if isinstance(typed, VerifyParentRevision):
            return Succeeded(
                outcome=ParentVerification(
                    status="succeeded", verified=self.parent_verified(typed.parent.revision_id.root)
                )
            )
        assert isinstance(typed, RenderRoleArtifacts)
        return Succeeded(
            outcome=RenderedArtifacts(
                status="succeeded",
                prompts=(
                    ArtifactRef(
                        artifact_id=ArtifactId(root=f"prompt:{request.operation_id.root}"),
                        digest="prompt",
                    ),
                ),
            )
        )
