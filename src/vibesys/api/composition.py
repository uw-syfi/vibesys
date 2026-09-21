"""Facade verbs that avoid exposing heavy core classes on the public surface.

Both `build_run_environment` (`vibesys.sandbox.run_environment`) and
`ExperimentRepository` (`vibesys.run.experiment_repo`) construct live,
side-effecting objects (an opened sandbox environment; a git/GitHub
publication boundary with `push`/`sync`/`create_remote`). Callers that only
need one fact off of them -- the supported profiler set, or whether `origin`
already points at a given slug -- should not have to import those
constructors just to throw the object away afterward. These two functions
compute exactly that fact and keep the constructors themselves out of
`vibesys.api.__all__`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.run.experiment_repo import ExperimentRepository
from vibesys.sandbox.run_environment import build_run_environment

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.profilers import ProfilerKind
    from vibesys.sandbox.run_environment import RunEnvironmentSpec

__all__ = [
    "experiment_origin_matches",
    "supported_profilers",
]


def supported_profilers(spec: RunEnvironmentSpec) -> frozenset[ProfilerKind] | None:
    """Return the profiler kinds `spec`'s run environment supports.

    `None` means the environment supports every profiler kind (no
    restriction), matching `RunEnvironment.supported_profiler_kinds` and its
    use in `entrypoints.headless._validate_run_environment_profiler`. Building
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
