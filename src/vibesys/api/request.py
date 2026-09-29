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

from vibesys.composition import resolve_agent_driver
from vibesys.config import BUNDLED_RESOURCES
from vibesys.inputs import (
    InputBundle,
    InputSynthesisError,
    SynthesizedInputSpec,
    load_input_bundle,
    load_project_task,
    synthesize_input_bundle,
)
from vibesys.repository import (
    REPOSITORY_SLUG,
    generate_experiment_name,
    repository_name_from_experiment,
    validate_experiment_name,
)
from vibesys.run.contracts import ProfilerKind
from vibesys.run.experiment_repo import ExperimentRepository
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

    from vs_project.api import OrchestrationDescriptor


def default_skill_roots() -> tuple[Path, ...]:
    """Return the bundled skill collection, when one is installed."""
    skills = BUNDLED_RESOURCES.directory("skills")
    return () if skills is None else (skills,)


def load_objective(bundle: InputBundle) -> str:
    """Return one input bundle's objective text."""
    return bundle.objective


def with_operator_constraints(objective: str, constraints: list[str]) -> str:
    """Add run-specific invariants without mutating the input bundle."""
    normalized = [constraint.strip() for constraint in constraints if constraint.strip()]
    if not normalized:
        return objective
    lines = "\n".join(f"- {constraint}" for constraint in normalized)
    return f"{objective.rstrip()}\n\n## Operator constraints\n\n{lines}\n"


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
    "orchestration_roles",
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
    # lint-waiver: LW-020007 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
    from vibesys.plugin_builtins import built_in_orchestrations  # noqa: PLC0415

    registration = built_in_orchestrations().resolve(descriptor.id)
    registration.parse_options(descriptor)


def orchestration_roles(orchestration_id: str) -> tuple[str, ...]:
    """Return the agent role IDs a built-in orchestration declares, in order."""
    # lint-waiver: LW-101320 [PLC0415]; the product catalog imports every built-in policy, so it loads only when a caller needs it.
    # > Module scope would import every policy whenever the request facade loads;
    # > a shared cached loader adds indirection for two call sites.
    from vibesys.plugin_builtins import built_in_orchestrations  # noqa: PLC0415

    plugin = built_in_orchestrations().resolve(orchestration_id).plugin
    return tuple(str(role.id) for role in plugin.agents)


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
