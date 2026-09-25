"""Evolve profiler role family: one role per profiler kind for the candidate profiler.

Mirrors :mod:`vibesys.roles.profiler` (``multi``'s per-kind profiler roles),
but evolve renders from its own ``loops/evolve/`` template folder (which
falls back to the same ``prompts/shared/profilers/<kind>.j2`` files) and uses
a different fallback shape, so it lives in its own module rather than as an
addition to ``profiler.py``.
"""

from __future__ import annotations

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.profilers import PROFILER_DEFINITIONS, ProfilerKind
from vibesys.runtime import ReadOnly, Reuse, Role

__all__ = ["ALL_ROLES", "CANDIDATE_PROFILERS", "ProfilerSummary"]


def _fallback_candidate_profiler() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Profiler produced no structured response.",
        bottlenecks="n/a",
        suggestions="n/a",
        perf_metric=None,
        perf_unit=None,
    )


def _candidate_profiler_role(kind: ProfilerKind) -> Role:
    return Role(
        id="profiler",
        template=f"loops/evolve/profilers/{kind.value}.j2",
        reply=ProfilerSummary,
        fallback=_fallback_candidate_profiler,
        access=ReadOnly(),
        session=Reuse(),
        message="Profile the server and return exactly one JSON object matching the schema above.",
    )


CANDIDATE_PROFILERS: dict[ProfilerKind, Role] = {
    kind: _candidate_profiler_role(kind) for kind in PROFILER_DEFINITIONS
}

ALL_ROLES = tuple(CANDIDATE_PROFILERS.values())
