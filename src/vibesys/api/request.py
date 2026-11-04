"""Core facts and explicit validation for assembling a RunRequest.

Entrypoints use these functions to parse input and execution configuration,
then select the built-in catalog and launch implementations through launch.
Catalog validation here requires an explicit registry.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.composition import require_one_login_per_container, resolve_run_agent_specs
from vibesys.config import BUNDLED_SKILLS
from vibesys.inputs import (
    InputBundle,
    InputSynthesisError,
    SynthesizedInputSpec,
    load_objectives,
    load_project_task,
    load_task,
    synthesize_input_bundle,
    with_operator_constraints,
)
from vibesys.orchestration.agent_options import attribution_command_of
from vibesys.repository import (
    REPOSITORY_SLUG,
    generate_experiment_name,
    repository_name_from_experiment,
    validate_experiment_name,
)
from vibesys.run.contracts import ProfilerKind, RunRequest
from vibesys.run.core_run import ensure_resumable
from vibesys.run.experiment_repo import origin_matches
from vibesys.run.profilers import validate_run_request as validate_execution_request
from vibesys.run.skill_sources import resolve_skill_source_dirs
from vs_agent.api.images import (
    DockerBuildRunner,
    build_task_image,
    is_task_image_reference,
    local_image_id,
    task_image_inputs,
    task_image_reference,
)
from vs_project.api import GitRemoteRepository
from vs_runtime.api.infrastructure import (
    EditorImagePlan,
    RunEnvironmentSpec,
    TaskImageError,
    TaskImageFacts,
    build_run_environment,
    derived_environment_name,
    editor_image_plan,
    make_run_environment_spec,
    migrate_recorded_run_environment,
    resolve_run_environment_spec,
    run_environment_record,
)
from vs_sandbox.api import (
    accelerator_unavailable,
    backend_default_image,
    backend_provided,
    sysbox_unavailable,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.plugin_catalog import OrchestrationRegistry
    from vs_project.api import Git, OrchestrationDescriptor
    from vs_runtime.api.infrastructure import RunEnvironment
    from vs_sandbox.api import ComputeBackend
    from vs_sim.api import CommandProbe


def default_skill_roots() -> tuple[Path, ...]:
    """Return the bundled skill collection, when one is installed."""
    skills = BUNDLED_SKILLS.root()
    return () if skills is None else (skills,)


def checkout_identity(root: Path, git: Git) -> tuple[str, bool]:
    """Identify the host checkout's committed tree and any local modifications.

    Unlike the host inventory's commit id, the epoch names the tree. Failed
    probes refuse launch rather than recording invented provenance.
    """
    tree = git.run_text(("rev-parse", "HEAD^{tree}"), cwd=root, timeout=2)
    status = git.run_text(
        ("status", "--porcelain", "--untracked-files=normal"), cwd=root, timeout=2
    )
    if tree.returncode or status.returncode or not tree.stdout.strip():
        message = "cannot identify the host checkout tree and dirty state"
        raise RuntimeError(message)
    return tree.stdout.strip(), bool(status.stdout.strip())


def load_objective(bundle: InputBundle) -> str:
    """Return one input bundle's objective text."""
    return bundle.objective


__all__ = [
    "REPOSITORY_SLUG",
    "DockerBuildRunner",
    "EditorImagePlan",
    "HostShortfall",
    "InputBundle",
    "InputSynthesisError",
    "RunEnvironmentSpec",
    "SynthesizedInputSpec",
    "TaskImageError",
    "TaskImageFacts",
    "backend_default_image",
    "build_task_image",
    "checkout_identity",
    "default_skill_roots",
    "derived_environment_name",
    "editor_image_plan",
    "ensure_resumable",
    "experiment_origin_matches",
    "generate_experiment_name",
    "is_task_image_reference",
    "load_objective",
    "load_objectives",
    "load_project_task",
    "load_task",
    "local_image_id",
    "make_run_environment_spec",
    "migrate_recorded_run_environment",
    "repository_name_from_experiment",
    "resolve_run_environment_spec",
    "resolve_skill_source_dirs",
    "run_environment_record",
    "run_environment_uses_docker",
    "run_host_shortfall",
    "supported_profilers",
    "synthesize_input_bundle",
    "task_image_inputs",
    "task_image_reference",
    "validate_descriptor",
    "validate_experiment_name",
    "validate_run_request",
    "with_operator_constraints",
]


def validate_descriptor(
    descriptor: OrchestrationDescriptor, *, registry: OrchestrationRegistry
) -> None:
    """Validate a selected policy against a caller-owned catalog."""
    registry.resolve(descriptor.id).parse_options(descriptor)


def validate_run_request(
    request: RunRequest, *, registry: OrchestrationRegistry, git: Git, read_only: bool = False
) -> None:
    """Reject invalid execution settings and policy role keys before host probes."""
    registration = registry.resolve(request.orchestration.id)
    options = registration.parse_options(request.orchestration)
    validate_execution_request(
        request, git=git, attribution_command=attribution_command_of(options)
    )
    specs = resolve_run_agent_specs(
        request.config,
        registration.plugin.agents,
        backend=request.agent_backend,
        provider=request.cli_provider,
        allow_unvetted_model=request.allow_unvetted_model,
    )
    require_one_login_per_container(
        specs, containerized=run_environment_uses_docker(request.run_environment)
    )
    if not read_only and request.resume is not None and registration.plugin.core is not None:
        ensure_resumable(request.project_root, request.resume.run_id)


def run_environment_uses_docker(spec: RunEnvironmentSpec | None) -> bool:
    """Whether a run in `spec`'s run environment starts local Docker containers.

    Read from the environment `spec` selects (the default one for `None`, as a run resolves
    it), as `supported_profilers` reads its profilers, so a caller never names a concrete
    environment.
    """
    return build_run_environment(spec or make_run_environment_spec()).uses_docker


@dataclass(frozen=True)
class HostShortfall:
    """What this host lacks for a run, before its image build."""

    reason: str
    """One line saying what is missing, with no advice about flags or settings."""
    backend: ComputeBackend | None
    """The run's compute backend when that is what the host lacks, else ``None``."""


def run_host_shortfall(request: RunRequest, probe: CommandProbe) -> HostShortfall | None:
    """What this host cannot give `request`'s run before its image build, or `None`.

    A run stops before its image build when its environment would hand the agent the
    compute backend's accelerator and this host lacks it, or when its task needs
    docker-in-docker and this host's Docker daemon registers no Sysbox. This asks both
    questions through `probe`, with the run's own decisions, so a run that passes it does
    not stop on either at launch. A backend the host lacks is reported with the backend,
    so a caller can say which one; the reason names what is missing, never a flag.
    """
    environment = _run_environment_of(request)
    docker_in_docker = request.input_bundle.docker_in_docker
    backend = request.backend
    if environment.attaches_backend_accelerator(
        docker_in_docker=docker_in_docker
    ) and not backend_provided(backend, probe):
        reason = accelerator_unavailable(backend, probe)
        return HostShortfall(
            reason=reason or f"the '{backend}' backend needs hardware this host does not report",
            backend=backend,
        )
    if environment.requires_sysbox_runtime(docker_in_docker=docker_in_docker):
        reason = sysbox_unavailable(probe)
        return None if reason is None else HostShortfall(reason=reason, backend=None)
    return None


def _run_environment_of(request: RunRequest) -> RunEnvironment:
    """The environment `request`'s run resolves to, as the run resolves it."""
    spec = resolve_run_environment_spec(
        request.run_environment or make_run_environment_spec(),
        request.backend,
        task_dockerfile=request.input_bundle.dockerfile_path,
        platform=sys.platform,
        log=lambda _line: None,
    )
    return build_run_environment(spec)


def supported_profilers(spec: RunEnvironmentSpec) -> frozenset[ProfilerKind] | None:
    """Return the profiler kinds `spec`'s run environment supports.

    `None` means the environment supports every profiler kind (no
    restriction), matching `RunEnvironment.supported_profiler_ids` and its
    use in `entrypoints.cli._validate_run_environment_profiler`. Building
    the environment just to read this one attribute is intentional here so
    callers never need to import `build_run_environment` (which returns a
    live, potentially side-effecting environment handle) themselves.
    """
    supported_ids = build_run_environment(spec).supported_profiler_ids
    if supported_ids is None:
        return None
    return frozenset(ProfilerKind(profiler_id) for profiler_id in supported_ids)


def experiment_origin_matches(destination: Path, repository: str, *, git: Git) -> bool:
    """Return whether `destination`'s git `origin` remote already points at `repository`.

    `repository` is a GitHub `OWNER/NAME` slug; `git` starts the Git command that reads it.
    """
    return origin_matches(GitRemoteRepository(destination, git=git), repository)
