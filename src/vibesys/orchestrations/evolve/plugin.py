"""Evolutionary-search plugin declaration."""

from pydantic import BaseModel

from vibesys.orchestrations.evolve.agents import AGENTS
from vibesys.orchestrations.evolve.models import EvolveOptions, EvolveState
from vibesys.orchestrations.evolve.orchestration import orchestrate
from vs_runtime.api import OrchestrationPlugin, PluginProjection


def _project(raw_state: BaseModel) -> PluginProjection:
    state = EvolveState.model_validate(raw_state)
    return PluginProjection(
        payload={
            "population": state.population.model_dump(mode="json"),
            "metric_space": state.metric_space.model_dump(mode="json"),
            "generation": state.population.generation,
        }
    )


PLUGIN = OrchestrationPlugin(
    id="evolve",
    agents=AGENTS,
    options=EvolveOptions,
    state=EvolveState,
    orchestrate=orchestrate,
    project=_project,
)

__all__ = ["PLUGIN"]
