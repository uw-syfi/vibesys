"""Typed loader for ``examples/registry.toml`` and the fail-closed discovery around it.

``examples/registry.toml`` is the one place that declares every runnable
example. The guard tests in ``tests/architecture/test_example_registry.py``
read it through this module, so the CI checks iterate the registry rather than
a glob, and an example that is added without an entry fails a test.
"""

from __future__ import annotations

import configparser
import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator
from scripts.example_repositories import is_example_repository_path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "examples" / "registry.toml"
REGISTRY_RELATIVE = "examples/registry.toml"

#: Directory names never descended into while looking for unregistered examples.
_PRUNED_DIRS = frozenset({".git", "target", "node_modules", "__pycache__"})


class Layout(StrEnum):
    """How an example carries its task definitions."""

    TASK = "task"  # `.vibesys/tasks/<task>/` inside the example directory
    LEGACY = "legacy"  # root `vibesys.input.toml` and `OBJECTIVE.md`


class Status(StrEnum):
    """What CI can promise about an example."""

    VALIDATED = "validated"  # every static check passes and CI can satisfy `requires`
    KNOWN_FAILING = (
        "known-failing"  # a listed check fails today; must start failing the build when fixed
    )
    LIVE_ONLY = "live-only"  # static checks pass; a live run needs something CI lacks


class Requirement(StrEnum):
    """Something an example needs beyond a plain checkout."""

    OVERLAY = "overlay"  # `.vibesys/` fetched by scripts/example_repositories.py
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    GPU = "gpu"
    MODEL_WEIGHTS = "model-weights"


class Check(StrEnum):
    """Static checks that can be registered as known-failing."""

    VALIDATE = "validate"  # the `vibesys validate` contract, plus command path references
    TRUST_POLICY = "trust-policy"  # evaluator-owned paths are read-only for the agent


#: Requirements the `validate-examples` CI job provides.
CI_SATISFIABLE = frozenset({Requirement.OVERLAY})


class ExampleEntry(BaseModel):
    """One registered example."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str  # repo-relative POSIX path
    layout: Layout
    tasks: Literal["all"] | tuple[str, ...] = "all"  # legacy examples have no tasks
    requires: tuple[Requirement, ...] = ()
    status: Status
    reason: str | None = None
    tracking: str | None = None  # issue or PR link for a known-failing entry
    failing_checks: tuple[Check, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        if self.layout is Layout.LEGACY and self.tasks != "all":
            problems.append("legacy examples have no tasks; omit `tasks`")
        if self.status is Status.VALIDATED:
            unsatisfiable = sorted(set(self.requires) - CI_SATISFIABLE)
            if unsatisfiable:
                problems.append(
                    f"status validated cannot require {unsatisfiable}; use live-only or known-failing"
                )
            if self.reason or self.tracking or self.failing_checks:
                problems.append("reason, tracking and failing_checks are only for other statuses")
        if self.status is Status.LIVE_ONLY:
            if not self.reason:
                problems.append("live-only needs a reason")
            if self.failing_checks:
                problems.append("failing_checks is only for known-failing")
        if self.status is Status.KNOWN_FAILING and not (
            self.reason and self.tracking and self.failing_checks
        ):
            problems.append("known-failing needs reason, tracking and failing_checks")
        if problems:
            message = f"{self.path}: " + "; ".join(problems)
            raise ValueError(message)
        return self

    @property
    def root(self) -> Path:
        """Absolute example root."""
        return REPO_ROOT / self.path

    @property
    def needs_overlay(self) -> bool:
        """Whether the task definitions only exist after the overlay fetch."""
        return Requirement.OVERLAY in self.requires

    def expects_failure(self, check: Check) -> bool:
        """Whether ``check`` is registered as failing today."""
        return check in self.failing_checks


class Registry(BaseModel):
    """The parsed ``examples/registry.toml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    example: tuple[ExampleEntry, ...]

    @model_validator(mode="after")
    def _unique_paths(self) -> Self:
        paths = [entry.path for entry in self.example]
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        if duplicates:
            message = f"duplicate registry paths: {duplicates}"
            raise ValueError(message)
        return self


def load_registry(path: Path = REGISTRY_PATH) -> Registry:
    """Parse and validate the registry."""
    return Registry.model_validate(tomllib.loads(path.read_text()))


def require_overlays() -> bool:
    """Whether a missing overlay must fail rather than skip (set by CI)."""
    return os.environ.get("VIBESYS_REQUIRE_EXAMPLE_OVERLAYS") == "1"


def overlay_missing(entry: ExampleEntry) -> bool:
    """Whether ``entry`` needs its overlay and the checkout does not have it."""
    return entry.needs_overlay and not (entry.root / ".vibesys" / "tasks").is_dir()


def submodule_example_paths() -> set[str]:
    """Return the ``.gitmodules`` paths that have the runnable-example shape."""
    config = configparser.ConfigParser()
    config.read(REPO_ROOT / ".gitmodules")
    paths = (Path(config.get(section, "path")) for section in config.sections())
    return {path.as_posix() for path in paths if is_example_repository_path(path)}


def _looks_like_example(directory: Path) -> bool:
    return (
        (directory / "vibesys.input.toml").is_file()
        or (directory / "OBJECTIVE.md").is_file()
        or (directory / ".vibesys" / "tasks").is_dir()
    )


def unregistered_examples(registered: set[str]) -> list[str]:
    """Return example directories that look runnable and are not registered.

    Walks ``examples/`` and stops at the first directory that is registered or
    looks like an example, so candidate sources, ``reference/`` trees, and
    nested submodules inside an example are never enumerated. Overlay
    submodules count even when their overlay has not been fetched.
    """
    found = {path for path in submodule_example_paths() if path not in registered}
    for current, dirs, _files in os.walk(REPO_ROOT / "examples"):
        relative = Path(current).relative_to(REPO_ROOT).as_posix()
        if relative in registered:
            dirs.clear()
            continue
        if _looks_like_example(Path(current)):
            found.add(relative)
            dirs.clear()
            continue
        dirs[:] = sorted(d for d in dirs if d not in _PRUNED_DIRS)
    return sorted(found)


def suggested_entry(path: str) -> str:
    """Return the registry entry to paste for an unregistered example."""
    layout = Layout.TASK if (REPO_ROOT / path / ".vibesys" / "tasks").is_dir() else Layout.LEGACY
    overlay = path in submodule_example_paths()
    requires = '["overlay"]' if overlay else "[]"
    return (
        f'[[example]]\npath = "{path}"\nlayout = "{layout}"\n'
        f'requires = {requires}\nstatus = "validated"  # or live-only / known-failing, see docs/contributing/examples.md'
    )
