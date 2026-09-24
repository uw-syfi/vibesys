"""Internal compatibility bridge for the built-in loops' existing call contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.loops.legacy_request import RunRequest
from vibesys.loops.roles import expected_agent_roles
from vibesys.orchestration.contracts import RunDescription, empty_run_view
from vibesys.profilers import ProfilerKind

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.api.contracts import RunStatus, RunView
    from vibesys.api.run_request import RunRequestLike
    from vibesys.run.integration import LocalRunIntegration
    from vibesys.runtime import VibeSysRuntime
    from vs_project.api import Project


class _BuiltinRequestError(ValueError):
    """A built-in policy requires its legacy request options."""


@runtime_checkable
class _LegacyIntegrationRuntime(Protocol):
    """Internal capability required by built-in compatibility adapters."""

    @property
    def legacy_integration(self) -> LocalRunIntegration: ...


def legacy_request(request: RunRequestLike) -> RunRequest:
    """Narrow a request at the built-in compatibility boundary."""
    if not isinstance(request, RunRequest):
        message = (
            f"built-in orchestration {request.orchestration_id!r} requires RunRequest "
            "with loop selection; descriptor-only requests lack its legacy options"
        )
        raise _BuiltinRequestError(message)
    return request


def legacy_integration(runtime: VibeSysRuntime) -> LocalRunIntegration:
    """Access the old run integration only within built-in adapters."""
    if not isinstance(runtime, _LegacyIntegrationRuntime):
        message = "built-in orchestration runtime requires legacy_integration"
        raise TypeError(message)
    return runtime.legacy_integration


class LegacyBuiltinDefaults:
    """Lifecycle defaults for built-ins that do not own a read model."""

    namespace: str

    def prepare(self, request: RunRequestLike, runtime: VibeSysRuntime) -> None:
        """Existing loop entrypoints construct their own run contexts."""
        del runtime
        legacy_request(request)

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Return a neutral view for loops without a persisted read model."""
        del project
        return empty_run_view(run_id=run_id, status=status, loop=loop)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """No live projection is defined for this built-in."""
        del namespace, state, run_id
        return None

    def history_namespaces(self) -> tuple[str, ...]:
        """Expose this policy's portable state to run history queries."""
        return (self.namespace,)


def required_objective(request: RunRequest) -> str:
    """Require the objective used by the agent and evolve built-ins."""
    if request.objective is None:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="missing_objective",
                stage="dispatch",
                message=f"RunRequest for outer loop {request.orchestration_id!r} must set objective",
            )
        )
    return request.objective


def built_in_description(request: RunRequest, *, round_budget: bool) -> RunDescription:
    """Preserve a built-in policy's existing run-start presentation."""
    roles = expected_agent_roles(request.orchestration_id)
    if request.profiler_kind is ProfilerKind.NONE:
        roles = tuple(role for role in roles if role != "profiler")
    return RunDescription(
        max_rounds=(request.max_rounds if request.max_rounds is not None else 1)
        if round_budget
        else 1,
        expected_roles=roles,
    )
