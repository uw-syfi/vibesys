"""Locate the VibeSys input SDK in a checkout or installed wheel."""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

from vibesys.constants import PROJECT_ROOT
from vs_runtime.api.infrastructure import resolve_bundled_tree, resolve_packaged_tree

if TYPE_CHECKING:
    from pathlib import Path


def packaged_sdk_root() -> Path | None:
    """Return the wheel-staged VibeSys SDK tree, or ``None``."""
    return resolve_packaged_tree(package="vibesys", packaged_subdir="_sdk", package_files=files)


def sdk_root() -> Path | None:
    """Return the active SDK tree, preferring a repository checkout."""
    return resolve_bundled_tree(
        PROJECT_ROOT / "sdk",
        package="vibesys",
        packaged_subdir="_sdk",
        package_files=files,
    )


__all__ = ["packaged_sdk_root", "sdk_root"]
