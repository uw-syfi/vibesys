"""Core facts and explicit validation for assembling a RunRequest.

Entrypoints use these functions to parse input and execution configuration,
then select the built-in catalog and launch implementations through launch.
Catalog validation here requires an explicit registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.composition import resolve_agent_driver, resolve_agent_specs
from vibesys.config import BUNDLED_RESOURCES
from vibesys.inputs import (
    InputBundle,
    InputSynthesisError,
    SynthesizedInputSpec,
    load_input_bundle,
    load_project_task,
    synthesize_input_bundle,
    with_operator_constraints,
)
from vibesys.repository import (
    REPOSITORY_SLUG,
    generate_experiment_name,
    repository_name_from_experiment,
    validate_experiment_name,
)
from vibesys.run.contracts import ProfilerKind, RunRequest
from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.run.profilers import validate_run_request as validate_execution_request
from vibesys.run.skill_sources import resolve_skill_source_dirs
from vs_agent.api.images import build_task_image
from vs_runtime.api.infrastructure import (
    RunEnvironmentSpec,
    build_run_environment,
    make_run_environment_spec,
    run_environment_record,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.plugin_catalog import OrchestrationRegistry
    from vs_project.api import OrchestrationDescriptor


def default_skill_roots() -> tuple[Path, ...]:
    """Return the bundled skill collection, when one is installed."""
    skills = BUNDLED_RESOURCES.directory("skills")
    return () if skills is None else (skills,)


def load_objective(bundle: InputBundle) -> str:
    """Return one input bundle's objective text."""
    return bundle.objective


__all__ = [
    "REPOSITORY_SLUG",
    "InputBundle",
    "InputSynthesisError",
    "RunEnvironmentSpec",
    "SynthesizedInputSpec",
    "build_task_image",
    "default_skill_roots",
    "experiment_origin_matches",
    "generate_experiment_name",
    "load_input_bundle",
    "load_objective",
    "load_project_task",
    "make_run_environment_spec",
    "repository_name_from_experiment",
    "resolve_agent_driver",
    "resolve_skill_source_dirs",
    "run_environment_record",
    "supported_profilers",
    "synthesize_input_bundle",
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


def validate_run_request(request: RunRequest, *, registry: OrchestrationRegistry) -> None:
    """Reject invalid execution settings and policy role keys before host probes."""
    validate_execution_request(request)
    registration = registry.resolve(request.orchestration.id)
    registration.parse_options(request.orchestration)
    resolve_agent_specs(
        request.config,
        registration.plugin.agents,
        backend=request.agent_backend,
        provider=request.cli_provider,
    )


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


def experiment_origin_matches(destination: Path, repository: str) -> bool:
    """Return whether `destination`'s git `origin` remote already points at `repository`.

    `repository` is a GitHub `OWNER/NAME` slug. Delegates to
    `ExperimentRepository.origin_matches` with a no-op logger so callers get
    the git-origin check without importing `ExperimentRepository` itself,
    which also carries `push`/`create_remote`.
    """
    return ExperimentRepository(destination, lambda _message: None).origin_matches(repository)
