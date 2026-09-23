"""Execution contract and registry for orchestration implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from vibesys.api.contracts import OrchestrationDescriptor

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.api.contracts import AnyRunRequest, RunStatus, RunView
    from vibesys.orchestration import ResumeProjection
    from vibesys.runtime import VibeSysRuntime
    from vs_project.api import OrchestrationRunManifest, Project


@dataclass(frozen=True, slots=True)
class RunDescription:
    """Policy-owned facts published when a run starts."""

    max_rounds: int = 1
    expected_roles: tuple[str, ...] = ()


class ExecutableOrchestration(Protocol):
    """Compatibility entry point for policies typed to either request DTO."""

    def execute(self, request: Any, runtime: VibeSysRuntime) -> bool: ...  # noqa: ANN401


@runtime_checkable
class Orchestration(ExecutableOrchestration, Protocol):
    """Complete policy contract resolved by the framework core."""

    def prepare(self, request: Any, runtime: VibeSysRuntime) -> None: ...  # noqa: ANN401

    def describe(self, request: Any) -> RunDescription: ...  # noqa: ANN401

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView: ...

    def project_committed(
        self, namespace: str, state: BaseModel, *, run_id: str
    ) -> RunView | None: ...

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection: ...


def empty_run_view(
    *,
    run_id: str,
    status: RunStatus,
    loop: str,
) -> RunView:
    """Neutral view for policies without persisted read models."""
    from vibesys.api.contracts import RunView  # noqa: PLC0415

    return RunView(
        run_id=run_id,
        loop=loop,
        status=status,
        current_round=0,
        experiment_revision=0,
    )


class _ExecuteOnlyAdapter:
    """Normalize prior execute-only implementations at registration."""

    def __init__(self, implementation: ExecutableOrchestration) -> None:
        self._implementation = implementation

    def execute(self, request: AnyRunRequest, runtime: VibeSysRuntime) -> bool:
        return self._implementation.execute(request, runtime)

    def prepare(self, request: AnyRunRequest, runtime: VibeSysRuntime) -> None:
        prepare_policy = getattr(self._implementation, "prepare", None)
        if callable(prepare_policy):
            prepare_policy(request, runtime)

    def describe(self, request: AnyRunRequest) -> RunDescription:
        describe = getattr(self._implementation, "describe", None)
        if callable(describe):
            return describe(request)
        return RunDescription()

    def view(
        self,
        project: Project,
        run_id: str,
        *,
        status: RunStatus,
        loop: str,
    ) -> RunView:
        view = getattr(self._implementation, "view", None)
        if callable(view):
            return view(project, run_id, status=status, loop=loop)
        return empty_run_view(run_id=run_id, status=status, loop=loop)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        projector = getattr(self._implementation, "project_committed", None)
        if callable(projector):
            return projector(namespace, state, run_id=run_id)
        return None

    def resume_projection(self, manifest: OrchestrationRunManifest) -> ResumeProjection:
        projector = getattr(self._implementation, "resume_projection", None)
        if callable(projector):
            return projector(manifest)
        message = "execute-only orchestration has no resume projection"
        raise ValueError(message)


def project_run(
    policy: Orchestration | None,
    project: Project,
    *,
    run_id: str,
    status: RunStatus,
    loop: str,
) -> RunView:
    """Project a known policy, or a neutral view for an unavailable plugin."""
    if policy is None:
        return empty_run_view(run_id=run_id, status=status, loop=loop)
    return policy.view(project, run_id, status=status, loop=loop)


class OrchestrationRegistry:
    """Map stable orchestration IDs to implementations."""

    def __init__(self) -> None:
        self._implementations: dict[str, Orchestration] = {}

    def register(self, kind: str, implementation: ExecutableOrchestration) -> None:
        try:
            OrchestrationDescriptor(id=kind, config_version=1, options={})
        except ValidationError as exc:
            msg = f"invalid orchestration ID {kind!r}"
            raise ValueError(msg) from exc
        if kind in self._implementations:
            msg = f"orchestration {kind!r} is already registered"
            raise ValueError(msg)
        self._implementations[kind] = (
            implementation
            if isinstance(implementation, Orchestration)
            else _ExecuteOnlyAdapter(implementation)
        )

    def resolve(self, kind: str) -> Orchestration:
        try:
            return self._implementations[kind]
        except KeyError as exc:
            msg = f"orchestration {kind!r} is not registered"
            raise ValueError(msg) from exc
