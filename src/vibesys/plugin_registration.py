"""Product metadata bound to one reusable orchestration plugin."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from vibesys.run.contracts import (
    IterationSupport,
    IterationTree,
    IterationUnavailable,
    IterationUnavailableReason,
    PluginProjection,
    RunStatus,
    RunView,
)
from vibesys.run.logged_run import read_logged_run
from vs_core.api import FlowState, OperationRegistration
from vs_runtime.api.core import OperationRole, RuntimeRecord
from vs_runtime.api.infrastructure import known_steer_targets

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_core.api import OperationRegistry
    from vs_project.api import OrchestrationDescriptor, Project
    from vs_runtime.api import CorePolicy, OrchestrationPlugin, OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import SteerTargets


class OrchestrationProjector(Protocol):
    """Read a policy's durable state for live and historical observation."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project the recorded state for one run."""
        ...

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a state just committed by the host."""
        ...

    def steer_targets(self, project: Project, run_id: str) -> SteerTargets | None:
        """What a steer may name in the run's last committed state; None before it has one."""
        ...


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
        iterations=projection.iterations,
        rounds=projection.rounds,
        experiment_revision=projection.experiment_revision,
        input_failure=projection.input_failure,
    )


@dataclass(frozen=True, slots=True)
class RuntimeRecordProjector[S: FlowState, P: BaseModel]:
    """Project the strategy state of a core-driven run from its committed runtime record.

    A run on the core keeps its state in its run.log, which folds to core's state beside the
    search's state of type `state_type` (`vibesys.run.logged_run`). `operations` decodes the log,
    so it must hold the operations the strategy recorded. `published` reads the record the
    strategy published from that state (None before it published one), and `project` is a
    pure function of that record and core's revision.
    """

    plugin_id: str
    state_type: type[S]
    operations: OperationRegistry
    published: Callable[[S], P | None]
    project: Callable[[P, int], PluginProjection]

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Project the run as its log leaves it, or identity and status when it holds no run."""
        logged = read_logged_run(
            project, run_id, registry=self.operations, state_type=self.state_type
        )
        if logged is None:
            return RunView(run_id=run_id, loop=loop, status=status)
        return self._view(logged.core.revision, logged.strategy, run_id=run_id, status=status)

    def steer_targets(self, project: Project, run_id: str) -> SteerTargets | None:
        """The roles, attempts and items a steer may name, from the log's committed state.

        The state is the one the run itself resolves a steer against, so a steer valid here is
        valid for the run as of that commit. None when the log holds no start of the run.
        """
        logged = read_logged_run(
            project, run_id, registry=self.operations, state_type=self.state_type
        )
        return None if logged is None else known_steer_targets(logged.core)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project a runtime record the host just committed under this plugin's namespace."""
        if namespace != self.plugin_id or not isinstance(state, RuntimeRecord):
            return None
        if not isinstance(state.envelope.strategy, self.state_type):
            return None
        envelope = state.envelope
        return self._view(
            envelope.revision, envelope.strategy, run_id=run_id, status=RunStatus.ACTIVE
        )

    def _view(
        self, revision: int, strategy: S | None, *, run_id: str, status: RunStatus
    ) -> RunView:
        """The record's view; a projection that fails validation is a view that says so.

        Every reader goes through here: the live commit observer, the iteration and experiment
        queries, and the run listing. Decoding the published record is reading durable state,
        so a corrupt or incompatible record fails loudly. Building the view from it is derived
        work: a bug there must not fail the reader (or a listing of every run), so it becomes
        `IterationUnavailableReason.PROJECTION_INVALID` with the error as its detail and no
        payload, and the commit observer reports it.
        """
        published = None if strategy is None else self.published(strategy)
        if published is None:
            return _plugin_run_view(self.plugin_id, run_id, status, _NOTHING_PUBLISHED)
        try:
            projection = self.project(published, revision)
        except ValueError as error:
            projection = PluginProjection(
                payload=None,
                iterations=IterationUnavailable(
                    reason=IterationUnavailableReason.PROJECTION_INVALID, detail=str(error)
                ),
                experiment_revision=revision,
            )
        return _plugin_run_view(
            self.plugin_id, run_id, status, _require_plugin_projection(projection)
        )


def project_steer_targets(
    registration: OrchestrationRegistration | None, project: Project, *, run_id: str
) -> SteerTargets | None:
    """The targets a steer may name in a run, or None for a policy with no projector or record."""
    projector = registration.projector if registration is not None else None
    return None if projector is None else projector.steer_targets(project, run_id)


_NOTHING_PUBLISHED = PluginProjection(payload={}, iterations=IterationTree.from_nodes(()))
"""The view of a run whose strategy has not published a record yet."""


@dataclass(frozen=True, slots=True)
class OrchestrationRegistration:
    """Bind one runtime-neutral plugin to VibeSys product policy."""

    plugin: OrchestrationPlugin
    resume_policy: (
        Callable[
            [OrchestrationDescriptor, OrchestrationDescriptor],
            OrchestrationResumeDecision,
        ]
        | None
    ) = None
    project_max_rounds: Callable[[BaseModel], int | None] | None = None
    projector: OrchestrationProjector | None = None
    iteration_support: IterationSupport = IterationSupport.UNMAPPED

    def __post_init__(self) -> None:
        """Reject a malformed core policy at registration, before any run opens."""
        _validate_core_policy(self.plugin.id, self.plugin.core)

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


def _core_error(plugin_id: str, key: str, detail: str) -> ValueError:
    return ValueError(f"orchestration {plugin_id!r} core.{key}: {detail}")


def _validate_core_policy(plugin_id: str, core: CorePolicy) -> None:
    """Reject a malformed core slot at registration, naming the offending key.

    Dataclass annotations are not enforced at runtime, so a policy built with a wrong
    value would otherwise fail deep inside a run, after resources opened.
    """
    if not callable(core.plan):
        raise _core_error(plugin_id, "plan", "must be a callable factory")
    _validate_core_operations(plugin_id, core)
    templates = core.prompt_templates
    if not isinstance(templates, Path) or not templates.is_dir():
        detail = f"must be an existing directory, got {templates!r}"
        raise _core_error(plugin_id, "prompt_templates", detail)
    if not core.retention_label.strip():
        raise _core_error(plugin_id, "retention_label", "must be a non-empty label")
    for index, directory in enumerate(core.artifact_directories):
        path = PurePosixPath(directory)
        if not directory or path.is_absolute() or ".." in path.parts:
            detail = f"must be a relative path inside the workspace, got {directory!r}"
            raise _core_error(plugin_id, f"artifact_directories[{index}]", detail)


def _validate_core_operations(plugin_id: str, core: CorePolicy) -> None:
    roles: set[OperationRole] = set()
    kinds: set[str] = set()
    for index, operation in enumerate(core.operations):
        key = f"operations[{index}]"
        if not isinstance(operation.role, OperationRole):
            detail = f"must be an OperationRole, got {operation.role!r}"
            raise _core_error(plugin_id, f"{key}.role", detail)
        if not isinstance(operation.registration, OperationRegistration):
            raise _core_error(plugin_id, f"{key}.registration", "must be an OperationRegistration")
        kind = operation.registration.descriptor.kind
        if operation.role in roles:
            detail = f"role {operation.role.value!r} is registered twice"
            raise _core_error(plugin_id, f"{key}.role", detail)
        if kind in kinds:
            detail = f"operation kind {kind!r} is registered twice"
            raise _core_error(plugin_id, f"{key}.registration", detail)
        roles.add(operation.role)
        kinds.add(kind)


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
    "IterationSupport",
    "OrchestrationProjector",
    "OrchestrationRegistration",
    "RuntimeRecordProjector",
    "project_run",
    "project_steer_targets",
]
