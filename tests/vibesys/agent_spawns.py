"""Read the agent spawn relation from a run's agent-turn spans, checking what holds for every search.

Every agent turn names its agent, its role and the agents that started it. Whatever the search:
all turns of one agent agree on its role and its starters; an agent never starts itself; and a
starter is an agent whose first turn began before this agent's first turn. ``spawns`` returns,
for each role, the roles of the agents that started its agents, for a search's own test to hold
to what that search wires.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import Span


def spawns(spans: Sequence[Span]) -> dict[str, set[tuple[str, ...]]]:
    """For each role, the sorted roles of its agents' starters (empty: the search started it)."""
    first: dict[str, int] = {}
    agents: dict[str, tuple[str, tuple[str, ...]]] = {}
    for at, span in enumerate(spans):
        if span.label is None or span.label.kind != "agent_turn":
            continue
        assert span.label.attrs_json is not None, f"{span.id} has no attributes"
        attrs = json.loads(span.label.attrs_json)
        agent, role = str(attrs["agent"]), str(attrs["role"])
        started_by = tuple(str(item) for item in attrs["started_by"])
        assert agent, f"{span.id} names no agent"
        known = agents.setdefault(agent, (role, started_by))
        assert known == (role, started_by), f"the turns of {agent} disagree: {known}"
        first.setdefault(agent, at)
    found: dict[str, set[tuple[str, ...]]] = {}
    for agent, (role, started_by) in agents.items():
        assert agent not in started_by, f"{agent} starts itself"
        for starter in started_by:
            assert starter in agents, f"{agent} was started by {starter}, which took no turn"
            assert first[starter] < first[agent], f"{agent} began before {starter} started it"
        roles = tuple(sorted(agents[starter][0] for starter in started_by))
        found.setdefault(role, set()).add(roles)
    return found
