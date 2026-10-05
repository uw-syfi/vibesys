"""Product metadata bound to one reusable orchestration plugin."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.run.contracts import PluginProjection, RunStatus, RunView

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_project.api import OrchestrationDescriptor, Project
    from vs_runtime.api import OrchestrationPlugin, OrchestrationResumeDecision


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
    project: Callable[[BaseModel], PluginProjection]

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Load the plugin's typed state and project its historical view."""
        del loop
        state_model = self.plugin.state
        if state_model is None:
            return _empty_run_view(run_id=run_id, status=status, loop=self.plugin.id)
        state = (
            project.state.portable_namespace(run_id, self.plugin.id)
            .slot("state.json", state_model)
            .load_optional()
        )
        projection = _require_plugin_projection(self.project(state)) if state is not None else None
        return _plugin_run_view(self.plugin.id, run_id, status, projection)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project the exact state model committed under this plugin's namespace."""
        state_model = self.plugin.state
        if namespace != self.plugin.id or state_model is None or type(state) is not state_model:
            return None
        return _plugin_run_view(
            self.plugin.id,
            run_id,
            RunStatus.ACTIVE,
            _require_plugin_projection(self.project(state)),
        )


def _require_plugin_projection(value: BaseModel) -> PluginProjection:
    """Reject projection callbacks that escape the product-owned contract."""
    if not isinstance(value, PluginProjection) or type(value) is not PluginProjection:
        message = (
            "orchestration projection callback must return exactly "
            f"PluginProjection, got {type(value).__name__}"
        )
        raise TypeError(message)
    return value


def _plugin_run_view(
    plugin_id: str,
    run_id: str,
    status: RunStatus,
    projection: PluginProjection | None,
) -> RunView:
    """Wrap the portable plugin projection in VibeSys-owned run identity."""
    if projection is None:
        return _empty_run_view(run_id=run_id, status=status, loop=plugin_id)
    return RunView(
        run_id=run_id,
        loop=plugin_id,
        status=status,
        projection=projection.payload,
        rounds=projection.rounds,
        experiment_revision=projection.experiment_revision,
    )


@dataclass(frozen=True, slots=True)
class OrchestrationRegistration:
    """Bind one runtime-neutral plugin to VibeSys product policy."""

    plugin: OrchestrationPlugin
    project: Callable[[BaseModel], PluginProjection] | None = None
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    ) = None
    project_max_rounds: Callable[[BaseModel], int | None] | None = None
    projector: OrchestrationProjector | None = None

    def __post_init__(self) -> None:
        """Bind exactly one read source: a projection of the declared state or a projector.

        `project` reads the plugin's declared state model, so it needs one. A
        plugin whose durable state lives elsewhere supplies a `projector` that
        owns reading it. A registration never has both.
        """
        if self.project is not None and self.projector is not None:
            message = "orchestration registration takes a projection or a projector, not both"
            raise ValueError(message)
        if self.project is not None and self.plugin.state is None:
            message = (
                "orchestration plugin projection requires a declared state model; "
                "a plugin without one supplies its own projector"
            )
            raise ValueError(message)
        if self.project is not None:
            object.__setattr__(self, "projector", _PluginProjector(self.plugin, self.project))

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
        return plugin.options.model_validate_json(json.dumps(descriptor.options), strict=True)


def _empty_run_view(*, run_id: str, status: RunStatus, loop: str) -> RunView:
    """Return identity and status for a policy without a read projection."""
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
        return _empty_run_view(run_id=run_id, status=status, loop=loop)
    return projector.view(project, run_id, status=status, loop=loop)


__all__ = [
    "OrchestrationProjector",
    "OrchestrationRegistration",
    "project_run",
]
