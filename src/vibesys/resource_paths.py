"""Locate framework resources in a repository checkout or an installed wheel.

Profiler support packages and the preset skills library live under
``resources/`` in a checkout. The wheel build stages the same trees into the
``vibesys._resources`` package directory (see ``packaging/resources_packaging.py``), so
an installed ``vibesys`` finds them without a checkout. The checkout always
wins when both exist: a developer editing ``resources/`` must see their edits,
not a stale staged copy.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from vibesys.constants import PROJECT_ROOT


def packaged_resources_dir() -> Path | None:
    """Return the wheel-staged ``vibesys/_resources`` directory, or ``None``."""
    try:
        base = Path(str(files("vibesys"))) / "_resources"
    except (ModuleNotFoundError, TypeError):  # pragma: no cover - defensive
        return None
    return base if base.is_dir() else None


def resources_root() -> Path | None:
    """Return the active resources tree: checkout first, wheel staging second."""
    checkout = PROJECT_ROOT / "resources"
    if checkout.is_dir():
        return checkout
    return packaged_resources_dir()


def profiler_support_dir(kind_value: str) -> Path | None:
    """Return the support package directory for a profiler kind, or ``None``."""
    root = resources_root()
    if root is None:
        return None
    candidate = root / "profilers" / kind_value
    return candidate if candidate.is_dir() else None


#: Workspace-staged name for ``profiler_support_common_dir()``. Distinct from
#: the checkout's ``_common`` (an underscore-prefixed name reads as private
#: infrastructure, not agent-facing) so every profiler plugin's ``server.py``
#: can find it by trying both names as a sibling of its own directory.
PROFILERS_COMMON_STAGED_NAME = "profilers_common"


def profiler_support_common_dir() -> Path | None:
    """Return the shared ``capture_runtime`` support directory, or ``None``.

    This is ``resources/profilers/_common`` (or its wheel-staged copy): the
    engine-agnostic capture-lifecycle runtime every profiler plugin imports.
    It is staged into an agent workspace as a sibling of each
    ``<kind>_profiler/`` directory, named ``profilers_common`` (see
    :data:`PROFILERS_COMMON_STAGED_NAME`).
    """
    root = resources_root()
    if root is None:
        return None
    candidate = root / "profilers" / "_common"
    return candidate if candidate.is_dir() else None


def default_skill_roots() -> tuple[Path, ...]:
    """Return the preset skill roots, empty when no resources tree is found."""
    root = resources_root()
    if root is None:
        return ()
    skills = root / "skills"
    return (skills,) if skills.is_dir() else ()


def evaluator_packages_dir() -> Path | None:
    """Return the bundled evaluator package collection, or ``None``."""
    root = resources_root()
    if root is None:
        return None
    evaluators = root / "evaluators"
    return evaluators if evaluators.is_dir() else None
