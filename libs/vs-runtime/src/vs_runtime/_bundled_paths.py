"""Resolve a data tree from a checkout or an installed package."""

from __future__ import annotations

from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

PackageFiles = Callable[[str], Any]


def resolve_bundled_tree(
    checkout: Path,
    *,
    package: str,
    packaged_subdir: str,
    package_files: PackageFiles = files,
) -> Path | None:
    """Prefer an existing checkout tree, then an installed package data tree.

    ``packaged_subdir`` must be one direct child name. This API deliberately
    does not accept arbitrary relative paths, so callers cannot use package
    resource lookup as a path escape hatch.
    """
    if checkout.is_dir():
        return checkout
    return resolve_packaged_tree(
        package=package, packaged_subdir=packaged_subdir, package_files=package_files
    )


def resolve_packaged_tree(
    *,
    package: str,
    packaged_subdir: str,
    package_files: PackageFiles = files,
) -> Path | None:
    """Return one installed package data directory when it exists."""
    if not packaged_subdir or Path(packaged_subdir).name != packaged_subdir:
        message = "packaged_subdir must be one non-empty child name"
        raise ValueError(message)
    try:
        packaged = Path(str(package_files(package))) / packaged_subdir
    except (ModuleNotFoundError, TypeError):
        return None
    return packaged if packaged.is_dir() else None


__all__ = ["PackageFiles", "resolve_bundled_tree", "resolve_packaged_tree"]
