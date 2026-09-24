"""The advertised loop-to-agent-roles contract.

Single authoritative mapping from ``--outer-loop`` kind to the agent roles
that loop can run as per-round executions: the ``kind=`` values its call
sites pass to ``LoopContext.invoke``. ``run_started`` advertises this
sequence to frontends, which seed one pending placeholder per role for each
round. Conditional roles are included (``profiler`` runs only when profiling
is enabled and requested) because a pending placeholder is the correct
display for a role that may still run. ``entrypoints.cli`` drops
``profiler`` from the advertised tuple when the resolved profiler kind is
``ProfilerKind.NONE``, since it is then guaranteed not to run.

Kept free of loop imports: ``entrypoints.cli`` reads it while emitting
``run_started``, before any loop module is loaded, and the loop packages
intentionally avoid heavy package-level imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

EXPECTED_AGENT_ROLES: Mapping[str, tuple[str, ...]] = {
    "agent": ("orchestrator", "implementer", "judge", "profiler"),
    "profile-guided": ("orchestrator", "implementer", "judge", "profiler"),
    "plain": ("implementer", "judge", "perf_eval"),
    "evolve": ("implementer", "judge", "profiler"),
}


def expected_agent_roles(outer_loop: str) -> tuple[str, ...]:
    """Return the per-round agent roles ``outer_loop`` can invoke."""
    try:
        return EXPECTED_AGENT_ROLES[outer_loop]
    except KeyError as exc:
        raise ValueError(f"Unknown outer loop {outer_loop!r}.") from exc
