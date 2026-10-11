"""The example catalog: ``examples/registry.toml`` and what it says about each example.

A VibeSys source checkout keeps runnable examples under ``examples/<category>/``.
Each is a project root whose tasks live in ``.vibesys/tasks/<task>/``, and each
is declared once in ``examples/registry.toml``. This module owns that file's
format (the one definition the CLI and the registry tests share) and turns it
into an :class:`ExampleList`, the typed listing ``vibesys example list --json``
prints and the desktop helper reads.

Reading the catalog never imports the run engine: ``vibesys example`` is a light
host command (``tests/architecture/test_light_entrypoints.py``).
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from vs_project.api import Project, ProjectError

REGISTRY_RELATIVE = PurePosixPath("examples/registry.toml")
"""Where the registry lives in a checkout."""

EXAMPLES_DIRNAME = "examples"


class Layout(StrEnum):
    """How an example carries its task definitions."""

    TASK = "task"  # `.vibesys/tasks/<task>/` inside the example directory
    LEGACY = "legacy"  # root `vibesys.input.toml` and `OBJECTIVE.md`


class Check(StrEnum):
    """Static checks the registry tests run for every example and every task."""

    VALIDATE = "validate"  # the `vibesys validate` contract
    PATH_REFS = "path-refs"  # `${PROJECT_ROOT}/...` paths in task commands exist
    TRUST_POLICY = "trust-policy"  # files the commands read are read-only for the agent


class KnownFailing(BaseModel):
    """A check that fails today; strict xfail, so it must be removed once it passes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: Check
    reason: str
    tracking: str  # issue or PR link


class Skip(BaseModel):
    """A check that cannot run because it needs source absent from the checkout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: Check
    reason: str


class ExampleEntry(BaseModel):
    """One registered example."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str  # repo-relative POSIX path
    layout: Layout
    tasks: Literal["all"] | tuple[str, ...] = "all"  # legacy examples have no tasks
    # True when the task files and app source come from another repository that
    # the checkout pins as a submodule under examples/<category>/repositories/.
    external_repo: bool = False
    known_failing: tuple[KnownFailing, ...] = ()
    skips: tuple[Skip, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        if self.layout is Layout.LEGACY and self.tasks != "all":
            problems.append("legacy examples have no tasks; omit `tasks`")
        declared = [item.check for item in (*self.known_failing, *self.skips)]
        if len(declared) != len(set(declared)):
            problems.append("a check may be declared known_failing or skipped once")
        relative = PurePosixPath(self.path)
        if not relative.is_relative_to(EXAMPLES_DIRNAME) or relative == PurePosixPath(
            EXAMPLES_DIRNAME
        ):
            problems.append(f"path must be under {EXAMPLES_DIRNAME}/")
        if problems:
            message = f"{self.path}: " + "; ".join(problems)
            raise ValueError(message)
        return self

    @property
    def name(self) -> str:
        """The catalog name: the path below ``examples/`` (``data-structures/stack``)."""
        return PurePosixPath(self.path).relative_to(EXAMPLES_DIRNAME).as_posix()

    @property
    def category(self) -> str:
        """The first directory below ``examples/`` (``data-structures``)."""
        return PurePosixPath(self.name).parts[0]

    def known_failure(self, check: Check) -> KnownFailing | None:
        """Return the registered known failure for ``check``, if any."""
        return next((item for item in self.known_failing if item.check is check), None)

    def skip(self, check: Check) -> Skip | None:
        """Return the explicit skip for ``check``, if any."""
        return next((item for item in self.skips if item.check is check), None)


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


class ExampleSummary(BaseModel):
    """One example in ``vibesys example list --json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The value ``vibesys example new`` accepts, e.g. ``data-structures/stack``."""
    category: str
    """The ``examples/<category>/`` directory the example is catalogued under."""
    tasks: tuple[str, ...]
    """Task names, sorted; empty for an external repository that is not fetched."""
    external_repo: bool
    """Whether ``example new`` clones another repository instead of copying files."""


class ExampleList(BaseModel):
    """``vibesys example list --json``: every catalogued example, by category then name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    checkout: str
    """The resolved VibeSys checkout whose ``examples/`` the listing was read from."""
    examples: tuple[ExampleSummary, ...]


class CatalogError(Exception):
    """The catalog is missing, malformed, or does not name the requested example."""


def load_registry(checkout: Path) -> Registry:
    """Parse and validate ``checkout``'s registry; ``CatalogError`` names the file."""
    path = checkout / REGISTRY_RELATIVE
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        message = (
            f"no example catalog at {path}; run from a VibeSys source checkout or pass --checkout"
        )
        raise CatalogError(message) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        message = f"cannot read {path}: {exc}"
        raise CatalogError(message) from exc
    try:
        return Registry.model_validate(document)
    except ValidationError as exc:
        message = f"invalid {path}: {exc}"
        raise CatalogError(message) from exc


def example_tasks(root: Path) -> tuple[str, ...]:
    """Return the sorted task names under ``root/.vibesys/tasks``; empty when there are none.

    An external repository that is not fetched has no directory yet, so no tasks.
    """
    if not root.is_dir():
        return ()
    try:
        project = Project.open(root)
        if not project.is_initialized():
            return ()
        return tuple(sorted(task.name.value for task in project.discover_tasks()))
    except ProjectError as exc:
        message = f"{root}: {exc}"
        raise CatalogError(message) from exc


def list_examples(checkout: Path) -> ExampleList:
    """Describe every registered example of ``checkout``, sorted by category and name."""
    root = checkout.expanduser().resolve()
    entries = sorted(load_registry(root).example, key=lambda entry: (entry.category, entry.name))
    return ExampleList(
        checkout=str(root),
        examples=tuple(
            ExampleSummary(
                name=entry.name,
                category=entry.category,
                tasks=example_tasks(root / entry.path),
                external_repo=entry.external_repo,
            )
            for entry in entries
        ),
    )


def find_example(registry: Registry, name: str) -> ExampleEntry:
    """Return the entry ``name`` selects: a catalog name, or its unique last component."""
    wanted = name.strip("/")
    exact = [entry for entry in registry.example if wanted in {entry.name, entry.path}]
    short = [entry for entry in registry.example if PurePosixPath(entry.name).name == wanted]
    matches = exact or short
    if len(matches) == 1:
        return matches[0]
    if matches:
        names = ", ".join(sorted(entry.name for entry in matches))
        message = f"example {name!r} is ambiguous; use one of: {names}"
    else:
        message = f"no example named {name!r}; `vibesys example list` shows the catalog"
    raise CatalogError(message)
