"""Public surface of the scripted agents of a dynamic run (agent-less runs).

The decisions are pure (`plan`, `implement`, `judge`): each takes the facts of a turn and returns the
reply, what the agent says, and the edit it makes. The shell that reads the workspace, applies the
edit and answers the client is `launch.scripted_agents`.
"""

from __future__ import annotations

from vibesys.orchestration.dynamic.scripted._agents import (
    CHAT_REPLY,
    Turn,
    implement,
    judge,
    named_in,
    plan,
)
from vibesys.orchestration.dynamic.scripted._recipes import QUEUE_RECIPES, EditKind, Recipe
from vibesys.orchestration.dynamic.scripted._scenario import Fault
from vibesys.orchestration.dynamic.scripted._workload import (
    QUEUE_RS,
    WORKLOADS,
    Reader,
    Workload,
    recognized,
)
from vibesys.orchestration.dynamic.search import api as search

PLANNER_KIND = search.dynamic.ids.role_id(search.PLANNER.name).root
IMPLEMENTER_KIND = search.dynamic.ids.role_id(search.IMPLEMENTER.name).root
JUDGE_KIND = search.dynamic.ids.role_id(search.JUDGE.name).root

__all__ = [
    "CHAT_REPLY",
    "IMPLEMENTER_KIND",
    "JUDGE_KIND",
    "PLANNER_KIND",
    "QUEUE_RECIPES",
    "QUEUE_RS",
    "WORKLOADS",
    "EditKind",
    "Fault",
    "Reader",
    "Recipe",
    "Turn",
    "Workload",
    "implement",
    "judge",
    "named_in",
    "plan",
    "recognized",
]
