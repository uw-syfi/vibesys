"""Composition point for built-in orchestration adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from vibesys.api._orchestrations.agent import AgentOrchestration
from vibesys.api._orchestrations.contracts import (
    OrchestrationRegistry,
    RunDescription,
    project_run,
)
from vibesys.api._orchestrations.evolve import EvolveOrchestration
from vibesys.api._orchestrations.plain import PlainOrchestration
from vibesys.api.contracts import LoopKind, RunRequest

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.api._orchestrations.runtime import _LocalVibeSysRuntime
    from vibesys.api.contracts import RunStatus, RunView
    from vibesys.api.run_request import RunRequestLike
    from vibesys.orchestration import ResumeProjection
    from vibesys.run.integration import LocalRunIntegration
    from vibesys.runtime import VibeSysRuntime
    from vs_project.api import OrchestrationRunManifest, Project


class _LegacyImplementation(Protocol):
    def execute(self, request: RunRequest, integration: LocalRunIntegration) -> bool: ...

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection: ...


class _BuiltinRequestError(ValueError):
    """A built-in policy requires its legacy request options."""


class _LegacyOrchestration:
    """Keep the existing loop adapters on their integration-only contract."""

    def __init__(self, implementation: _LegacyImplementation) -> None:
        self._implementation = implementation

    def execute(self, request: RunRequestLike, runtime: VibeSysRuntime) -> bool:
        local = cast("_LocalVibeSysRuntime", runtime)
        return self._implementation.execute(_legacy_request(request), local.legacy_integration)

    def prepare(self, request: RunRequestLike, runtime: VibeSysRuntime) -> None:
        """The existing loop constructs its own run context during execute."""
        del request, runtime

    def describe(self, request: RunRequestLike) -> RunDescription:
        describe = getattr(self._implementation, "describe", None)
        return describe(_legacy_request(request)) if callable(describe) else RunDescription()

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        view = getattr(self._implementation, "view", None)
        if callable(view):
            return view(project, run_id, status=status, loop=loop)
        return project_run(None, project, run_id=run_id, status=status, loop=loop)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        projector = getattr(self._implementation, "project_committed", None)
        return projector(namespace, state, run_id=run_id) if callable(projector) else None

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
        return self._implementation.resume_projection(manifest)


def _legacy_request(request: RunRequestLike) -> RunRequest:
    if not isinstance(request, RunRequest):
        message = (
            f"built-in orchestration {request.orchestration_id!r} requires RunRequest "
            "with loop selection; descriptor-only requests lack its legacy options"
        )
        raise _BuiltinRequestError(message)
    return request


def built_in_orchestrations() -> OrchestrationRegistry:
    """Register current public loop IDs, including the agent variant."""
    registry = OrchestrationRegistry()
    agent = _LegacyOrchestration(AgentOrchestration())
    registry.register(LoopKind.AGENT, agent)
    registry.register(LoopKind.PROFILE_GUIDED, agent)
    registry.register(LoopKind.PLAIN, _LegacyOrchestration(PlainOrchestration()))
    registry.register(LoopKind.EVOLVE, _LegacyOrchestration(EvolveOrchestration()))
    return registry


def resume_projection(manifest: OrchestrationRunManifest) -> ResumeProjection:
    """Delegate descriptor validation and CLI projection to its owner."""
    implementation = built_in_orchestrations().resolve(manifest.orchestration.id)
    projector = cast("_LegacyOrchestration", implementation)
    return projector.resume_projection(manifest)
