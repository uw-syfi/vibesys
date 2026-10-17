"""Public surface of the scripted agents of a dynamic run (agent-less runs)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.scripted._recipes import QUEUE_RECIPES, EditKind, Recipe
from vibesys.orchestration.dynamic.scripted._script import CHAT_REPLY, DynamicScript, wire
from vibesys.orchestration.dynamic.scripted._workload import QUEUE_RS, WORKLOADS, Workload
from vibesys.orchestration.dynamic.search import api as search

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_agent.api import AgentEventSink, ScriptedTurn

PLANNER_KIND = search.dynamic.ids.role_id(search.PLANNER.name).root
IMPLEMENTER_KIND = search.dynamic.ids.role_id(search.IMPLEMENTER.name).root
JUDGE_KIND = search.dynamic.ids.role_id(search.JUDGE.name).root


def dynamic_responders(
    *,
    seed: int,
    sink: AgentEventSink,
    workloads: tuple[Workload, ...] = WORKLOADS,
) -> Mapping[str, Callable[[ScriptedTurn], dict[str, object]]]:
    """The responder of each role a scripted dynamic run asks, keyed by the role's agent kind.

    The profiler, the merger and the outliner have none: a plan that asks for them, which the
    scripted planner never does, fails its turn as an agent whose reply the schema refuses.
    """
    script = DynamicScript(seed=seed, sink=sink, workloads=workloads)

    def answering(
        reply: Callable[[ScriptedTurn], dict[str, object]],
    ) -> Callable[[ScriptedTurn], dict[str, object]]:
        return lambda turn: wire(turn, reply(turn))

    return {
        PLANNER_KIND: answering(script.plan),
        IMPLEMENTER_KIND: answering(script.implement),
        JUDGE_KIND: answering(script.judge),
    }


__all__ = [
    "CHAT_REPLY",
    "QUEUE_RECIPES",
    "QUEUE_RS",
    "WORKLOADS",
    "EditKind",
    "Recipe",
    "Workload",
    "dynamic_responders",
]
