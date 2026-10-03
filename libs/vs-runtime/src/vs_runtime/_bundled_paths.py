"""Resolve a data tree from a checkout or an installed package."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

PackageFiles = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class BundledResources:
    """Resolve one product's resource directories from a checkout or wheel.

    The checkout tree wins when present. Installed-package resources are the
    fallback, and callers can only select named descendants rather than
    supplying an unchecked relative path.
    """

    checkout: Path
    package: str
    packaged_subdir: str = "_resources"
    package_files: PackageFiles = field(default=files, repr=False, compare=False)

    def root(self) -> Path | None:
        """Return the active resource root, if either source exists."""
        return resolve_bundled_tree(
            self.checkout,
            package=self.package,
            packaged_subdir=self.packaged_subdir,
            package_files=self.package_files,
        )

    def directory(self, *children: str) -> Path | None:
        """Return an existing directory below the active resource root."""
        if not children or any(
            not child or child in {".", ".."} or Path(child).name != child for child in children
        ):
            message = "resource children must be non-empty direct names"
            raise ValueError(message)
        root = self.root()
        if root is None:
            return None
        candidate = root.joinpath(*children)
        return candidate if candidate.is_dir() else None


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


__all__ = [
    "BundledResources",
    "PackageFiles",
    "resolve_bundled_tree",
    "resolve_packaged_tree",
]
