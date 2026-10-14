"""Every compute backend is wired into the product's per-backend tables.

A new ``ComputeBackend`` member needs product decisions beyond its sandbox
implementation (registered in ``vs_sandbox``, checked by that library's
``test_compute_backend_registry.py``): the profiler ``auto`` picks for it, and
the platform guidance its agents may read. These checks fail CI naming the
backend and the table that lacks it. Prompt fragments are checked by
``test_backend_fragments.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.config import BUNDLED_RESOURCES
from vibesys.constants import ComputeBackend
from vibesys.orchestration.profilers import (
    PROFILER_DEFINITIONS,
    ProfilerKind,
    default_profiler_for_backend,
)
from vibesys.orchestration.skill_selection import PLATFORMS_PARENT

if TYPE_CHECKING:
    from pathlib import Path

_BACKENDS = tuple(ComputeBackend)
_BACKEND_NAMES = frozenset(backend.value for backend in ComputeBackend)


def _platform_dirs() -> tuple[Path, ...]:
    """Every ``references/platforms`` directory in the bundled skills."""
    skills = BUNDLED_RESOURCES.directory("skills")
    assert skills is not None
    return tuple(sorted(skills.glob(f"*/{'/'.join(PLATFORMS_PARENT)}")))


@pytest.mark.parametrize("backend", _BACKENDS, ids=str)
def test_every_backend_has_a_default_profiler_that_accepts_it(backend: ComputeBackend) -> None:
    kind = default_profiler_for_backend(backend)

    assert kind is ProfilerKind.NONE or kind in PROFILER_DEFINITIONS, kind
    if kind in PROFILER_DEFINITIONS:
        backends = PROFILER_DEFINITIONS[kind].backends
        assert backends is None or backend in backends, (
            f"{backend.value}'s default profiler {kind.value} does not declare {backend.value}"
        )


def test_the_bundled_skills_have_platform_guidance() -> None:
    """Guards the glob: an empty match would make the checks below vacuous."""
    assert _platform_dirs()


@pytest.mark.parametrize("platforms", _platform_dirs(), ids=lambda path: path.parents[1].name)
def test_platform_guidance_covers_exactly_the_known_backends(platforms: Path) -> None:
    """Pruning hides ``platforms/<backend>`` by backend name, so each name must be a backend.

    A directory named for no backend is never pruned and reaches every agent; a
    backend with no directory gets no platform guidance from that skill.
    """
    present = frozenset(path.name for path in platforms.iterdir() if path.is_dir())

    assert not present - _BACKEND_NAMES, (
        f"{platforms}: directories named for no ComputeBackend: {sorted(present - _BACKEND_NAMES)}"
    )
    assert not _BACKEND_NAMES - present, (
        f"{platforms}: no guidance for backends {sorted(_BACKEND_NAMES - present)}"
    )
