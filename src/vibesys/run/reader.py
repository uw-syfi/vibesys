"""The resources a conversation about a finished run reads it through, assembled from its saved state.

A live run's resources (``open_run_resources``) take the project's write lease, materialize and
repair its workspace, reap its orphaned agents, arm its repository and start its root
environment session. A reader does none of that: a finished run is only read. It rebuilds, from
the request that resuming the run restores from its record, the part a chat agent needs
(``AuxiliaryResources``): the project, how to open the run's environment, and the agent settings.
A conversation then opens its own environment session from that recipe, as it does in a live run.

The environment request is the reduced one a reader needs. It names the project, its confinement
(the same ``ProjectPathPolicy`` the run's agents are held to) and the private socket directory the
chat tools are served in. It carries no evaluator tools, profiler support, model mounts or
commands, because a reader evaluates, profiles and builds nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.composition import (
    _vibesys_runtime_host_resources,
    resolve_model,
    resolve_provider,
)
from vibesys.config import as_config
from vibesys.constants import PROJECT_ROOT
from vibesys.orchestration.skill_selection import resolve_skill_source_paths
from vibesys.run.environment import open_run_environment
from vibesys.run.evaluation_socket import evaluation_socket_mount
from vibesys.run.gpu_events import CoreGpuContentionEvents
from vibesys.run.integration import AuxiliaryResources
from vibesys.run.project_policy import build_project_path_policy
from vs_agent.api import AgentBackend
from vs_project.api import Project
from vs_runtime.api.infrastructure import (
    RunEnvironmentRequest,
    build_run_environment,
    make_run_environment_spec,
    require_built_task_image,
    resolve_run_environment_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.run.contracts import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentSpec
    from vs_sandbox.api import ComputeBackendImpl


class NotAResumeError(ValueError):
    """A reader was asked to open a request that does not name a saved run."""

    def __init__(self) -> None:
        """State what a reader needs of its request."""
        super().__init__("a reader opens a saved run: the request must resume one")


def open_reader_resources(
    request: RunRequest,
    integration: LocalRunIntegration,
    *,
    agent_specs: Mapping[str, AgentSpec],
    backend_factory: Callable[..., ComputeBackendImpl],
) -> AuxiliaryResources:
    """The resources to converse about the saved run ``request`` resumes, changing nothing of it.

    Raises ``NotAResumeError`` for a request that resumes no run, and the project's own error
    when the run is not recorded in the project.
    """
    if request.resume is None:
        raise NotAResumeError
    config = as_config(request.config)
    bundle = request.input_bundle
    run_id = request.resume.run_id
    project_root = request.project_root_for(run_id)
    project = Project.open(project_root, state_stores=integration.state_stores)
    project.state.load_run(run_id)
    # The run's own log directory is its record: a reader writes what it logs (an agent's log, a
    # container's startup output) beside its own state.
    log_dir = project.state.local_namespace(run_id, "reader").external_directory("logs")

    spec = resolve_run_environment_spec(
        request.run_environment or make_run_environment_spec(),
        request.backend,
        task_dockerfile=bundle.dockerfile_path,
        platform=sys.platform,
        log=_discard,
    )
    require_built_task_image(spec, bundle.dockerfile_path)
    environment = build_run_environment(spec)
    clock = integration.clock

    provider = resolve_provider(config, request.cli_provider)
    model = resolve_model(config, provider)
    agent_backend = str(request.agent_backend or config.agent.backend or AgentBackend.CLI)
    backend_impl = backend_factory(
        request.backend,
        log_dir=log_dir,
        log=_discard,
        image=environment.backend_image,
        clock=clock,
        events=CoreGpuContentionEvents(integration.events),
    )
    evaluator_source = (
        None
        if bundle.evaluator_path is None
        else Path(bundle.evaluator_path).expanduser().resolve()
    )
    environment_request = RunEnvironmentRequest(
        log_dir=log_dir,
        agent_homes_dir=Project.agent_homes_directory_for(project_root, run_id),
        workspace=project_root,
        ref_dir=None,
        backend=backend_impl,
        agent_backend=agent_backend,
        cli_provider=provider,
        run_id=run_id,
        framework_root=PROJECT_ROOT,
        clock=clock,
        system=integration.system,
        # Agents of a CLI run reach the run's private socket directory in their container; the
        # chat tools are served there.
        environment_bind_mounts=(
            (evaluation_socket_mount(project_root, run_id),)
            if agent_backend == AgentBackend.CLI
            else ()
        ),
        project_path_policy=build_project_path_policy(
            project_root, evaluator_source=evaluator_source
        ),
        state_namespace=project.state.local_namespace(run_id, "skypilot"),
        docker_in_docker=bundle.docker_in_docker,
    )
    return AuxiliaryResources(
        project=project,
        run_id=run_id,
        environment_request=environment_request,
        open_environment_session=lambda opened: open_run_environment(environment, opened),
        agent_backend=agent_backend,
        provider=provider,
        model=model,
        role_models=tuple(
            role.model
            for role in agent_specs.values()
            if role.model is not None and role.model != model and role.provider == provider
        ),
        config=config,
        allow_unvetted_model=request.allow_unvetted_model,
        backend=request.backend,
        skill_source_dirs=tuple(resolve_skill_source_paths(request.skills_dirs)),
        # An agent in a container (Docker, Modal, SkyPilot, Slurm: every environment that starts
        # one) brings its own resources; only an agent on the host needs the framework's.
        host_resources=() if environment.uses_docker else _vibesys_runtime_host_resources(),
        clock=clock,
        threads=integration.threads,
    )


def _discard(_text: str) -> None:
    """Drop a line the run would log: a reader keeps no log of its own."""


__all__ = ["NotAResumeError", "open_reader_resources"]
