"""Agent role declarations: data only.

Each module here is one role FAMILY: a group of :class:`vibesys.runtime.Role`
instances that share a conceptual job (planning, implementing, judging,
profiling, ...) across strategies, naming a template, a pydantic reply type,
a fallback, workspace-access policy, and session policy. No turn execution,
sequencing, or state transitions live here -- those stay in ``ctx.agents.turn``
(``vibesys.orchestration.runtime``) and in each strategy's ``loops/<strategy>/``
folder.

Different prompts or reply types are always different roles, even when two
strategies name the "same" conceptual step (e.g. ``multi`` and ``single`` each
have their own plan role): never a role branching on which strategy called it.

Families:
  - ``designer``:        round-planning roles (multi, single, profile_single)
  - ``pre_round``:       pre-round profiling decision (multi)
  - ``implementer``:     hypothesis/issue implementer roles (multi, issue_queue)
  - ``judge``:           hypothesis/issue judge roles (multi, issue_queue)
  - ``profiler``:        per-profiler-kind roles (multi)
  - ``single_agent``:    combined implement+judge+profile roles (single,
                         profile_single)
  - ``perf_eval``:       performance-evaluator role (issue_queue)
  - ``mutator``:         evolve's mutation-operator role (candidate implementer)
  - ``candidate_judge``: evolve's offspring judge role
  - ``evolve_profiler``: evolve's per-profiler-kind candidate profiler roles
  - ``common``:          reply-schema pieces shared by more than one family
                         (``Verdict``, ``SkillResourceSelection``)

``profile_multi`` is not yet catalogued separately: it reuses ``multi``'s
roles (implementer, judge, profiler, designer, pre_round) via
``search/profile_focus`` composition.

The catalog is complete: every role any strategy uses lives in one of these
modules; ``tests/vibesys/roles/test_catalog.py`` asserts every role here is
used by at least one registered strategy.
"""

from __future__ import annotations

from vibesys.roles import (
    candidate_judge,
    common,
    designer,
    evolve_profiler,
    implementer,
    judge,
    mutator,
    perf_eval,
    pre_round,
    profiler,
    single_agent,
)

ALL_ROLES = (
    *designer.ALL_ROLES,
    *pre_round.ALL_ROLES,
    *implementer.ALL_ROLES,
    *judge.ALL_ROLES,
    *profiler.ALL_ROLES,
    *single_agent.ALL_ROLES,
    *perf_eval.ALL_ROLES,
    *mutator.ALL_ROLES,
    *candidate_judge.ALL_ROLES,
    *evolve_profiler.ALL_ROLES,
)

__all__ = [
    "ALL_ROLES",
    "candidate_judge",
    "common",
    "designer",
    "evolve_profiler",
    "implementer",
    "judge",
    "mutator",
    "perf_eval",
    "pre_round",
    "profiler",
    "single_agent",
]
