"""Tests for the `vibesys.api.request` module: re-exports and composition verbs."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import vibesys.api.request
from vibesys.api.request import (
    CLI_PROFILER_CHOICES,
    REPOSITORY_SLUG,
    InputBundle,
    RunEnvironmentSpec,
    build_task_image,
    coerce_profiler_kind,
    default_skill_roots,
    experiment_origin_matches,
    generate_experiment_name,
    load_input_bundle,
    load_objective,
    load_project_task,
    make_run_environment_spec,
    repository_name_from_experiment,
    resolve_skill_source_dirs,
    supported_profilers,
    validate_experiment_name,
    with_operator_constraints,
)
from vibesys.sandbox.run_environment import build_run_environment

if TYPE_CHECKING:
    from pathlib import Path

_NAMES = [
    "InputBundle",
    "load_input_bundle",
    "load_project_task",
    "load_objective",
    "with_operator_constraints",
    "RunEnvironmentSpec",
    "make_run_environment_spec",
    "build_task_image",
    "CLI_PROFILER_CHOICES",
    "coerce_profiler_kind",
    "REPOSITORY_SLUG",
    "generate_experiment_name",
    "validate_experiment_name",
    "repository_name_from_experiment",
    "default_skill_roots",
    "resolve_skill_source_dirs",
    "supported_profilers",
    "experiment_origin_matches",
]


def test_request_names_are_exported_and_importable() -> None:
    """Every symbol this module owns is importable from it and listed in `__all__`."""
    exported = set(vibesys.api.request.__all__)
    for name in _NAMES:
        assert name in exported, f"{name!r} missing from vibesys.api.request.__all__"
        assert hasattr(vibesys.api.request, name), (
            f"{name!r} not importable from vibesys.api.request"
        )

    # Also exercise the direct `from vibesys.api.request import ...` names bound above,
    # so an unused-import lint would catch a broken re-export.
    assert InputBundle is not None
    assert load_input_bundle is not None
    assert load_project_task is not None
    assert load_objective is not None
    assert with_operator_constraints is not None
    assert RunEnvironmentSpec is not None
    assert make_run_environment_spec is not None
    assert build_task_image is not None
    assert CLI_PROFILER_CHOICES is not None
    assert coerce_profiler_kind is not None
    assert REPOSITORY_SLUG is not None
    assert generate_experiment_name is not None
    assert validate_experiment_name is not None
    assert repository_name_from_experiment is not None
    assert default_skill_roots is not None
    assert resolve_skill_source_dirs is not None
    assert supported_profilers is not None
    assert experiment_origin_matches is not None


def test_supported_profilers_matches_the_live_run_environment() -> None:
    """`supported_profilers` reads the same fact `build_run_environment(...)` would."""
    spec = make_run_environment_spec()  # local environment: no docker/modal/skypilot

    result = supported_profilers(spec)

    assert result == build_run_environment(spec).supported_profiler_kinds
    # The local environment supports every profiler kind (no restriction).
    assert result is None


def test_experiment_origin_matches_is_false_for_a_non_matching_repo(tmp_path: Path) -> None:
    """A directory with no matching `origin` remote never matches."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # noqa: S607
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/other-repo.git"],  # noqa: S607
        cwd=tmp_path,
        check=True,
    )

    assert experiment_origin_matches(tmp_path, "example/target-repo") is False


def test_experiment_origin_matches_is_false_without_a_git_repo(tmp_path: Path) -> None:
    """A destination that is not a git repository at all also reports no match."""
    assert experiment_origin_matches(tmp_path, "example/target-repo") is False
