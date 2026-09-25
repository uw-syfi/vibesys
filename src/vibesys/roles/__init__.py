"""Agent role declarations: data only.

Each module here is one strategy's (or ``issue_queue``'s) role family:
a set of :class:`vibesys.runtime.Role` instances naming a template, a
pydantic reply type, a fallback, workspace-access policy, and session
policy. No turn execution, sequencing, or state transitions live here --
those stay in ``ctx.agents.turn`` (``vibesys.orchestration.runtime``) and in
each strategy's ``loops/<strategy>/`` folder.

Different prompts or reply types are always different roles, even when two
strategies name the "same" conceptual step (e.g. ``multi`` and ``single``
each have their own ``*_ORCHESTRATOR_PLAN``): never a role branching on
which strategy called it.

profile_multi and profile_single are not yet catalogued here (deferred to
the phase that migrates them onto ``ctx.agents.turn``; see the phase-3a
report).
"""

from __future__ import annotations

from vibesys.roles import issue_queue, multi, single

ALL_ROLES = (*multi.ALL_ROLES, *single.ALL_ROLES, *issue_queue.ALL_ROLES)

__all__ = ["ALL_ROLES", "issue_queue", "multi", "single"]
