"""Execution and read projection contracts for registered orchestrations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from vibesys.orchestration.view import RoundSummary, RunStatus, RunView
from vs_project.api import OrchestrationDescriptor

if TYPE_CHECKING:
    from vs_project.api import Project
    from vs_runtime.api import OrchestrationPlugin, PluginProjection


class OrchestrationProjector(Protocol):
    """Read a policy's durable state for live and historical observation."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project the recorded state for one run."""
        ...

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a state just committed by the host."""
        ...


@dataclass(frozen=True, slots=True)
class _PluginProjector:
    """Adapt one runtime-neutral plugin projection to VibeSys read views."""

    plugin: OrchestrationPlugin

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Load the plugin's typed state and project its historical view."""
        del loop
        state_model = self.plugin.state
        callback = self.plugin.project
        if state_model is None or callback is None:
            return empty_run_view(run_id=run_id, status=status, loop=self.plugin.id)
        state = (
            project.state.portable_namespace(run_id, self.plugin.id)
            .slot("state.json", state_model)
            .load_optional()
        )
        projection = callback(state) if state is not None else None
        return _plugin_run_view(self.plugin.id, run_id, status, projection)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project the exact state model committed under this plugin's namespace."""
        state_model = self.plugin.state
        callback = self.plugin.project
        if (
            namespace != self.plugin.id
            or state_model is None
            or callback is None
            or type(state) is not state_model
        ):
            return None
        return _plugin_run_view(self.plugin.id, run_id, RunStatus.ACTIVE, callback(state))


def _plugin_run_view(
    plugin_id: str,
    run_id: str,
    status: RunStatus,
    projection: PluginProjection | None,
) -> RunView:
    """Wrap the portable plugin projection in VibeSys-owned run identity."""
    if projection is None:
        return empty_run_view(run_id=run_id, status=status, loop=plugin_id)
    return RunView(
        run_id=run_id,
        loop=plugin_id,
        status=status,
        projection=projection.payload,
        rounds=tuple(RoundSummary.model_validate(item.model_dump()) for item in projection.rounds),
        experiment_revision=projection.experiment_revision,
    )


@dataclass(frozen=True, slots=True)
class OrchestrationRegistration:
    """The runtime plugin and optional read projection for one stable ID."""

    plugin: OrchestrationPlugin
    projector: OrchestrationProjector | None = None
    portable_namespaces: tuple[str, ...] = ()
    state_family: str | None = None

    def parse_options(self, descriptor: OrchestrationDescriptor) -> BaseModel:
        """Validate one descriptor and return the plugin's typed options."""
        plugin = self.plugin
        if descriptor.id != plugin.id:
            message = (
                f"selected orchestration {descriptor.id!r} does not match plugin {plugin.id!r}"
            )
            raise ValueError(message)
        if descriptor.config_version != plugin.config_version:
            message = (
                f"orchestration {plugin.id!r} requires config version "
                f"{plugin.config_version}, got {descriptor.config_version}"
            )
            raise ValueError(message)
        return plugin.options.model_validate_json(
            json.dumps(descriptor.options),
            strict=True,
        )


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
    """Map stable orchestration IDs to runtime plugins."""

    def __init__(self) -> None:
        """Create an empty registration table."""
        self._registrations: dict[str, OrchestrationRegistration] = {}

    def register_plugin(
        self,
        plugin: OrchestrationPlugin,
        *,
        state_family: str | None = None,
    ) -> None:
        """Register a runtime plugin in the product catalog."""
        try:
            OrchestrationDescriptor(id=plugin.id, config_version=plugin.config_version, options={})
        except ValidationError as exc:
            message = f"invalid orchestration plugin ID {plugin.id!r}"
            raise ValueError(message) from exc
        if plugin.id in self._registrations:
            msg = f"orchestration {plugin.id!r} is already registered"
            raise ValueError(msg)
        self._registrations[plugin.id] = OrchestrationRegistration(
            plugin=plugin,
            projector=_PluginProjector(plugin) if plugin.project is not None else None,
            portable_namespaces=(plugin.id,) if plugin.state is not None else (),
            state_family=state_family,
        )

    def resolve(self, kind: str) -> OrchestrationRegistration:
        """Return the registration or reject an unknown orchestration ID."""
        try:
            return self._registrations[kind]
        except KeyError as exc:
            msg = f"orchestration {kind!r} is not registered"
            raise ValueError(msg) from exc
