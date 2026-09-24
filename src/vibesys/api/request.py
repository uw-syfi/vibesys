"""The surface for assembling a `RunRequest` before a run exists.

`vibesys.api` (the package `__init__`) is the run/observe contract: creating a
session from an already-built `RunRequest`, and reading back its events and
views. This module is the other half: everything needed to build that
`RunRequest` in the first place -- loading or synthesizing the input bundle,
loading the objective, describing the run environment and an optional task
image, naming and locating the experiment repository, and resolving which
skills ship with the run.

The `entrypoints` package (VibeSys's headless entrypoint) is the primary
consumer: it parses CLI arguments into calls against this module to build a
`RunRequest`, then hands that request to `vibesys.api.create_session`.
`server` does not currently build requests itself, but this module is where
that capability would live if a server-initiated run is added later.

Imports come directly from the core modules that own these symbols, not from
`vibesys.api`, to avoid a cycle between the two facade modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.agent_spec_config import resolve_agent_driver
from vibesys.evaluators.input_manifest import InputBundle, load_input_bundle, load_project_task
from vibesys.evaluators.input_synthesis import (
    InputSynthesisError,
    SynthesizedInputSpec,
    synthesize_input_bundle,
)
from vibesys.evaluators.objective import load_objective, with_operator_constraints
from vibesys.profilers import CLI_PROFILER_CHOICES, coerce_profiler_kind
from vibesys.repository import (
    REPOSITORY_SLUG,
    generate_experiment_name,
    repository_name_from_experiment,
    validate_experiment_name,
)
from vibesys.resource_paths import default_skill_roots
from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    build_run_environment,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.sandbox.task_image import build_task_image
from vibesys.skills import resolve_skill_source_dirs

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.profilers import ProfilerKind
    from vs_project.api import OrchestrationDescriptor

__all__ = [
    "CLI_PROFILER_CHOICES",
    "REPOSITORY_SLUG",
    "InputBundle",
    "InputSynthesisError",
    "RunEnvironmentSpec",
    "SynthesizedInputSpec",
    "build_task_image",
    "coerce_profiler_kind",
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
    "with_operator_constraints",
]


def validate_descriptor(descriptor: OrchestrationDescriptor) -> None:
    """Validate a selected policy before the CLI creates run resources."""
    from vibesys.loops.registry import built_in_orchestrations  # noqa: PLC0415

    built_in_orchestrations().resolve(descriptor.id).orchestrator(descriptor)


def supported_profilers(spec: RunEnvironmentSpec) -> frozenset[ProfilerKind] | None:
    """Return the profiler kinds `spec`'s run environment supports.

    `None` means the environment supports every profiler kind (no
    restriction), matching `RunEnvironment.supported_profiler_kinds` and its
    use in `entrypoints.cli._validate_run_environment_profiler`. Building
    the environment just to read this one attribute is intentional here so
    callers never need to import `build_run_environment` (which returns a
    live, potentially side-effecting environment handle) themselves.
    """
    return build_run_environment(spec).supported_profiler_kinds


def experiment_origin_matches(destination: Path, repository: str) -> bool:
    """Return whether `destination`'s git `origin` remote already points at `repository`.

    `repository` is a GitHub `OWNER/NAME` slug. Delegates to
    `ExperimentRepository.origin_matches` with a no-op logger so callers get
    the git-origin check without importing `ExperimentRepository` itself,
    which also carries `push`/`sync`/`create_remote`.
    """
    return ExperimentRepository(destination, lambda _message: None).origin_matches(repository)
