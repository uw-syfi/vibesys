"""Execution and read projection contracts for registered orchestrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import ValidationError

from vibesys.orchestration.view import RunView
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.context import RunSetup
    from vibesys.orchestration.runtime import RunContext
    from vibesys.orchestration.view import RunStatus
    from vs_project.api import Project


class Orchestrator(Protocol):
    """A descriptor-validated policy that controls one run."""

    setup: RunSetup

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate the ID, config version, and typed options before setup."""
        ...

    async def run(self, ctx: RunContext) -> bool:
        """Run policy control flow using the host capabilities."""
        ...


class OrchestrationProjector(Protocol):
    """Read a policy's durable state for live and historical observation."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project the recorded state for one run."""
        ...

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a state just committed by the host."""
        ...


@dataclass(frozen=True, slots=True)
class OrchestrationRegistration:
    """The execution class and optional read projection for one stable ID."""

    orchestrator: type[Orchestrator]
    projector: OrchestrationProjector | None = None
    portable_namespaces: tuple[str, ...] = ()
    state_family: str | None = None


def empty_run_view(*, run_id: str, status: RunStatus, loop: str) -> RunView:
    """Identity and status view for a policy without a read projection."""
    return RunView(run_id=run_id, loop=loop, status=status)


def project_run(
    registration: OrchestrationRegistration | None,
    project: Project,
    *,
    run_id: str,
    status: RunStatus,
    loop: str,
) -> RunView:
    """Project a registered policy or return its neutral identity view."""
    projector = registration.projector if registration is not None else None
    if projector is None:
        return empty_run_view(run_id=run_id, status=status, loop=loop)
    return projector.view(project, run_id, status=status, loop=loop)


class OrchestrationRegistry:
    """Map stable orchestration IDs to concrete policy classes."""

    def __init__(self) -> None:
        """Create an empty registration table."""
        self._registrations: dict[str, OrchestrationRegistration] = {}

    def register(
        self,
        kind: str,
        orchestrator: type[Orchestrator],
        *,
        projector: OrchestrationProjector | None = None,
        portable_namespaces: tuple[str, ...] = (),
        state_family: str | None = None,
    ) -> None:
        """Register the policy constructor and its explicit read projection."""
        try:
            OrchestrationDescriptor(id=kind, config_version=1, options={})
        except ValidationError as exc:
            msg = f"invalid orchestration ID {kind!r}"
            raise ValueError(msg) from exc
        if kind in self._registrations:
            msg = f"orchestration {kind!r} is already registered"
            raise ValueError(msg)
        self._registrations[kind] = OrchestrationRegistration(
            orchestrator=orchestrator,
            projector=projector,
            portable_namespaces=portable_namespaces,
            state_family=state_family,
        )

    def resolve(self, kind: str) -> OrchestrationRegistration:
        """Return the registration or reject an unknown orchestration ID."""
        try:
            return self._registrations[kind]
        except KeyError as exc:
            msg = f"orchestration {kind!r} is not registered"
            raise ValueError(msg) from exc
