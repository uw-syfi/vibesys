"""Typed loader for ``examples/registry.toml`` and the fail-closed discovery around it.

``examples/registry.toml`` is the one place that declares every runnable
example. The guard tests in ``tests/examples/test_example_registry.py``
read it through this module, so the CI checks iterate the registry rather than
a glob, and an example that is added without an entry fails a test.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

import pytest
from scripts.example_repositories import is_example_repository_path

from entrypoints.example_catalog import ExampleEntry, Layout, Registry
from entrypoints.example_catalog import load_registry as load_catalog_registry

REPO_ROOT = Path(__file__).resolve().parents[2]

FETCH_HINT = "run `uv run python scripts/example_repositories.py`"

#: Directory names never descended into while looking for unregistered examples.
_PRUNED_DIRS = frozenset({".git", "target", "node_modules", "__pycache__"})


def load_registry() -> Registry:
    """Parse and validate this checkout's registry."""
    return load_catalog_registry(REPO_ROOT)


def example_root(entry: ExampleEntry) -> Path:
    """Absolute root of a registered example in this checkout."""
    return REPO_ROOT / entry.path


def require_external_repos() -> bool:
    """Whether a missing external repo checkout must fail rather than skip (set by CI)."""
    return os.environ.get("VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS") == "1"


def external_repo_missing(entry: ExampleEntry) -> bool:
    """Whether ``entry`` is an external repo and its checkout is absent."""
    return entry.external_repo and not _checkout_present(entry.path)


def _checkout_present(path: str) -> bool:
    return (REPO_ROOT / path / ".vibesys" / "tasks").is_dir()


def require_external_repo_checkout(path: str) -> None:
    """Fail (CI) or skip (local) when the external repo example at ``path`` is not fetched.

    ``path`` is the repo-relative example root. Tests that read files from an
    external repo example call this first, so they neither error on a missing
    file nor silently lose coverage where CI has fetched the checkout.
    """
    if _checkout_present(path):
        return
    message = f"{path} is an external repo that is not fetched: {FETCH_HINT}"
    if require_external_repos():
        pytest.fail(message)
    pytest.skip(f"{message} (or set VIBESYS_REQUIRE_EXAMPLE_EXTERNAL_REPOS=1 to fail instead)")


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
    nested submodules inside an example are never enumerated. External-repo
    submodules count even when the external repo has not been fetched.
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
    entry = f'[[example]]\npath = "{path}"\nlayout = "{layout}"'
    return f"{entry}\nexternal_repo = true" if path in submodule_example_paths() else entry
