"""Agent role declarations: data only.

Each module here is one role FAMILY: a group of :class:`vibesys.runtime.Role`
instances that share a conceptual job (planning, implementing, judging,
profiling, ...) across strategies, naming a template, a pydantic reply type,
a fallback, workspace-access policy, and session policy. No turn execution,
sequencing, or state transitions live here -- those stay in ``ctx.agents.turn``
(``vibesys.orchestration.runtime``) and in each strategy's ``orchestrations/<strategy>/``
folder.

Different prompts or reply types are always different roles, even when two
strategies name the "same" conceptual step (e.g. ``multi`` and ``single`` each
have their own plan role): never a role branching on which strategy called it.

Families:
  - ``designer``:        round-planning role (multi)
  - ``pre_round``:       pre-round profiling decision (multi)
  - ``implementer``:     hypothesis implementer roles (multi)
  - ``judge``:           hypothesis judge role (multi)
  - ``profiler``:        per-profiler-kind roles (multi)
  - ``common``:          reply-schema pieces shared by more than one family
                         (``Verdict``, ``SkillResourceSelection``)

``profile_multi`` is not yet catalogued separately: it reuses ``multi``'s
roles (implementer, judge, profiler, designer, pre_round) via
``search/profile_focus`` composition.

The catalog is complete for policies that still use :class:`vibesys.runtime.Role`.
Explicit orchestration plugins own their :class:`vs_runtime.api.AgentRole`
declarations and response contracts within their policy packages.
"""

from __future__ import annotations

from vibesys.roles import (
    common,
    designer,
    implementer,
    judge,
    pre_round,
    profiler,
)

ALL_ROLES = (
    *designer.ALL_ROLES,
    *pre_round.ALL_ROLES,
    *implementer.ALL_ROLES,
    *judge.ALL_ROLES,
    *profiler.ALL_ROLES,
)

__all__ = [
    "ALL_ROLES",
    "common",
    "designer",
    "implementer",
    "judge",
    "pre_round",
    "profiler",
]
