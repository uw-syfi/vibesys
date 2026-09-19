"""Helpers that define the Python packages owned by the VibeSys distribution."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

PACKAGE_SOURCE_ROOTS = (
    Path("src"),
    Path("libs/vs-evaluator-protocol/src"),
    Path("libs/vs-feature-flags/src"),
    Path("libs/vs-github/src"),
    Path("libs/vs-issue-board/src"),
    Path("libs/vs-loop-state/src"),
    Path("libs/vs-project/src"),
    Path("libs/vs-prompts/src"),
    Path("libs/vs-sandbox/src"),
)
_BUILD_AND_CACHE_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
    }
)


def release_has_native_payload() -> bool:
    """Return whether setuptools is building a target-specific release wheel."""
    return os.environ.get("VIBESYS_WHEEL_TARGET") is not None


def clear_distribution_build_outputs(build_lib: Path, packages: list[str]) -> None:
    """Remove stale build-tree copies of packages owned by this distribution."""
    top_level_packages = {package.partition(".")[0] for package in packages}
    if any(not package.isidentifier() for package in top_level_packages):
        raise ValueError(  # noqa: TRY003  # tracked: #288
            "distribution package names must be Python identifiers"
        )
    for package in top_level_packages:
        destination = build_lib / package
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination)


def discover_distribution_packages(repo_root: Path) -> tuple[list[str], dict[str, str]]:
    """Return every import package and its source directory for setuptools."""
    packages: list[str] = []
    package_dirs: dict[str, str] = {}

    for relative_root in PACKAGE_SOURCE_ROOTS:
        source_root = repo_root / relative_root
        discovered: list[str] = []
        for top_level_init in source_root.glob("*/__init__.py"):
            top_level = top_level_init.parent
            directories = (
                top_level,
                *sorted(path for path in top_level.rglob("*") if path.is_dir()),
            )
            for directory in directories:
                relative = directory.relative_to(source_root)
                if all(
                    part.isidentifier() and part not in _BUILD_AND_CACHE_DIRECTORIES
                    for part in relative.parts
                ):
                    discovered.append(".".join(relative.parts))
        packages.extend(discovered)
        package_dirs.update(
            {
                package: (relative_root / Path(*package.split("."))).as_posix()
                for package in discovered
            }
        )

    return sorted(packages), package_dirs
