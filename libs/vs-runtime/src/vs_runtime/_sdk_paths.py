"""Validate local dependencies against owned checkout and packaged SDK roots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class InputProjectError(ValueError):
    """Raised when an input project's local dependencies are invalid."""

    @classmethod
    def outside_sdk(cls, path: object) -> InputProjectError:
        """Build an error for a declaration escaping the owned SDK roots."""
        return cls(f"Input dependency points outside sdk/: {path}")

    @classmethod
    def missing_pyproject(cls, candidate: Path, raw_path: str) -> InputProjectError:
        """Build an error for a source that is not an installable project."""
        return cls(f"Input dependency has no pyproject.toml: {candidate} (declared as {raw_path})")

    @classmethod
    def dependency(cls, name: str, cause: InputProjectError) -> InputProjectError:
        """Add the dependency name to a source-resolution error."""
        return cls(f"Input dependency {name!r}: {cause}")


@dataclass(frozen=True)
class SDKRoots:
    """Checkout and packaged roots used while materializing one input project."""

    checkout: Path
    packaged: Path | None


def resolve_sdk_source(
    project_dir: Path,
    raw_path: str,
    *,
    checkout_sdk_root: Path,
    packaged_sdk_root: Path | None,
) -> Path:
    """Resolve one declared SDK path without allowing arbitrary local paths."""
    declared = Path(raw_path)
    if declared.is_absolute():
        raise InputProjectError.outside_sdk(raw_path)

    checkout_root = checkout_sdk_root.resolve()
    packaged_root = packaged_sdk_root.resolve() if packaged_sdk_root is not None else None
    direct = (project_dir / declared).resolve()

    direct_root = _containing_root(direct, checkout_root, packaged_root)
    if direct_root is not None and (direct / "pyproject.toml").is_file():
        return direct
    if packaged_root is not None and direct_root == packaged_root:
        return _require_installable(direct, raw_path)

    normalized = Path(os.path.normpath(raw_path))
    parts = normalized.parts
    try:
        sdk_index = parts.index("sdk")
    except ValueError as exc:
        raise InputProjectError.outside_sdk(raw_path) from exc
    suffix = parts[sdk_index + 1 :]
    if not suffix or any(part in {"", ".", ".."} for part in suffix):
        raise InputProjectError.outside_sdk(raw_path)

    if packaged_root is not None:
        packaged = (packaged_root / Path(*suffix)).resolve()
        if _is_relative_to(packaged, packaged_root):
            return _require_installable(packaged, raw_path)

    if direct_root == checkout_root:
        return _require_installable(direct, raw_path)
    raise InputProjectError.outside_sdk(raw_path)


def relative_sdk_source(
    source: Path,
    *,
    checkout_sdk_root: Path,
    packaged_sdk_root: Path | None,
) -> Path:
    """Return an SDK package path relative to the tree that owns it."""
    resolved = source.resolve()
    roots = (checkout_sdk_root.resolve(),)
    if packaged_sdk_root is not None:
        roots += (packaged_sdk_root.resolve(),)
    for root in roots:
        if _is_relative_to(resolved, root):
            return resolved.relative_to(root)
    raise InputProjectError.outside_sdk(source)


def _require_installable(candidate: Path, raw_path: str) -> Path:
    if not (candidate / "pyproject.toml").is_file():
        raise InputProjectError.missing_pyproject(candidate, raw_path)
    return candidate


def _containing_root(path: Path, checkout: Path, packaged: Path | None) -> Path | None:
    for root in (checkout, packaged):
        if root is not None and _is_relative_to(path, root):
            return root
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = ["InputProjectError", "SDKRoots", "relative_sdk_source", "resolve_sdk_source"]
