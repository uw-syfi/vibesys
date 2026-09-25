"""Profiler role family: run one profiler kind and summarize it.

Used by ``multi`` (one role per :class:`~vibesys.profilers.ProfilerKind`,
shared with ``profile_multi`` via ``search/profile_focus`` composition).

Deviation from the design brief: ``ProfilerSummary``'s canonical definition
lives in ``vibesys.evaluators.perf_reply`` (re-exported here), not defined
here, because ``search/population`` also reads it (Pareto-frontier
selection) and ``search`` must never depend on ``vibesys.roles`` (roles
depends on search and evaluation data, never the reverse). ``evaluators``
sits below both, so both can depend on it without a layering inversion.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.profilers import PROFILER_DEFINITIONS, ProfilerKind
from vibesys.runtime import Fresh, ReadOnly, Role

__all__ = ["ALL_ROLES", "MULTI_PROFILERS", "ProfilerResponse", "ProfilerSummary"]


class ProfilerResponse(BaseModel):
    """Structured response from a profiler agent."""

    analysis: str = Field(
        description=(
            "Detailed interpretation of collected profiling data, including "
            "visibility limits and observed CPU, accelerator, or runtime evidence."
        )
    )
    bottlenecks: str = Field(
        description=(
            "Measured bottlenecks ordered by impact, or an explicit attribution gap "
            "when the capture cannot observe the production mechanism."
        )
    )
    suggestions: str = Field(
        description=(
            "Advisory optimization or measurement suggestions tied to measured "
            "evidence and estimated end-to-end impact; they do not block planning."
        )
    )


def _fallback_profiler_summary() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Profiler produced no structured response.",
        bottlenecks="n/a",
        suggestions="Re-run profiling on the next round.",
    )


def _profiler_role(kind: ProfilerKind) -> Role:
    """One role per profiler kind: each renders a different template.

    ``ProfilerDefinition.prompt_template`` resolves ``profilers/<kind>.j2``
    against multi's own folder, which falls back to
    ``prompts/shared/profilers/<kind>.j2`` (no per-strategy profiler prompts
    exist today); the role's template path keeps that same two shared-root
    resolution.
    """
    return Role(
        id="profiler",
        template=f"loops/multi/profilers/{kind.value}.j2",
        reply=ProfilerSummary,
        fallback=_fallback_profiler_summary,
        access=ReadOnly(),  # allow-list (bounded evidence dir) resolved per call by the caller
        session=Fresh(),
        message="Profile the server and return exactly one JSON object matching the schema above.",
    )


MULTI_PROFILERS: dict[ProfilerKind, Role] = {
    kind: _profiler_role(kind) for kind in PROFILER_DEFINITIONS
}

ALL_ROLES = tuple(MULTI_PROFILERS.values())
