"""Profiler role family: run one profiler kind and summarize it.

Used by ``multi`` (one role per :class:`~vibesys.profilers.ProfilerKind`,
shared with ``profile_multi`` via ``search/profile_focus`` composition) and
by ``evolve`` (the candidate profiler, one role per kind, reusing the same
shared per-kind templates behind its own wrapper templates and context).

Deviation from the design brief: ``ProfilerSummary``'s canonical definition
lives in ``vibesys.evaluators.perf_reply`` (re-exported here), not defined
here, because ``search/population`` also reads it (Pareto-frontier
selection) and ``search`` must never depend on ``vibesys.roles`` (roles
depends on search and evaluation data, never the reverse). ``evaluators``
sits below both, so both can depend on it without a layering inversion.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.profilers import PROFILER_DEFINITIONS, ProfilerKind
from vibesys.runtime import Fresh, ReadOnly, Reuse, Role

__all__ = [
    "ALL_ROLES",
    "CANDIDATE_PROFILERS",
    "MULTI_PROFILERS",
    "CandidateProfilerContext",
    "ProfilerContext",
    "ProfilerResponse",
    "ProfilerSummary",
]


class ProfilerContext(BaseModel):
    """Shared context for every profiler-kind role.

    One model serves all of ``MULTI_PROFILERS`` (a per-kind template each):
    templates that don't reference a given field (e.g. ``macos_cpu.j2``
    dropping ``modality``) mark that omission deliberate with a
    ``vs-prompts:unused`` comment rather than getting their own narrower
    model -- see ``prompts/shared/profilers/*.j2``.
    """

    model_config = ConfigDict(frozen=True)

    profile_focus: str
    benchmark_command: str | None
    modality: str | None
    domain_profiler: str
    runtime_notes: str
    profile_execution: str
    objective: str | None
    profiler_support_name: str
    profiler_mcp_name: str
    profiler_campaign_context: str


class CandidateProfilerContext(BaseModel):
    """Shared context for every evolve candidate-profiler-kind role.

    Mirrors :class:`ProfilerContext`, but evolve's per-round addendum is the
    Pareto-frontier objectives list (``pareto_objectives_addendum``) rather
    than multi's campaign-context recap, so it gets its own context model
    rather than reusing ``ProfilerContext``.
    """

    model_config = ConfigDict(frozen=True)

    benchmark_command: str | None
    domain_profiler: str
    modality: str | None
    objective: str | None
    pareto_objectives_addendum: str
    profile_execution: str
    profile_focus: str
    profiler_mcp_name: str
    profiler_support_name: str
    runtime_notes: str


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
        context=ProfilerContext,
        access=ReadOnly(),  # allow-list (bounded evidence dir) resolved per call by the caller
        session=Fresh(),
        message="Profile the server and return exactly one JSON object matching the schema above.",
    )


MULTI_PROFILERS: dict[ProfilerKind, Role] = {
    kind: _profiler_role(kind) for kind in PROFILER_DEFINITIONS
}


def _fallback_candidate_profiler() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Profiler produced no structured response.",
        bottlenecks="n/a",
        suggestions="n/a",
        perf_metric=None,
        perf_unit=None,
    )


def _candidate_profiler_role(kind: ProfilerKind) -> Role:
    """One role per profiler kind for evolve's candidate profiler.

    Renders from evolve's own ``loops/evolve/profilers/<kind>.j2`` wrapper
    (which includes the same ``prompts/shared/profilers/<kind>.j2`` body
    multi's role renders, then appends the Pareto-objectives addendum), so
    it is a distinct ``Role`` from ``MULTI_PROFILERS[kind]`` with its own
    fallback shape and context model.
    """
    return Role(
        id="profiler",
        template=f"loops/evolve/profilers/{kind.value}.j2",
        reply=ProfilerSummary,
        fallback=_fallback_candidate_profiler,
        context=CandidateProfilerContext,
        access=ReadOnly(),
        session=Reuse(),
        message="Profile the server and return exactly one JSON object matching the schema above.",
    )


CANDIDATE_PROFILERS: dict[ProfilerKind, Role] = {
    kind: _candidate_profiler_role(kind) for kind in PROFILER_DEFINITIONS
}

ALL_ROLES = (*MULTI_PROFILERS.values(), *CANDIDATE_PROFILERS.values())
