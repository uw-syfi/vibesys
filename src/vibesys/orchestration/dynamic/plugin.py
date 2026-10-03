"""Plugin registration for dynamic portfolio hypothesis search."""

from functools import partial

from pydantic import BaseModel

from vibesys.hypothesis.readmodel import project_hypothesis_state
from vibesys.orchestration.dynamic.agents import AGENTS
from vibesys.orchestration.dynamic.models import DynamicOptions, DynamicState
from vibesys.orchestration.dynamic.orchestration import orchestrate
from vibesys.orchestration.resume import compare_round_budget
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import PluginProjection
from vs_runtime.api import OrchestrationPlugin


def _project(raw_state: BaseModel) -> PluginProjection:
    """Project the authoritative shared hypothesis state."""
    return project_hypothesis_state(DynamicState.model_validate(raw_state).search)


def _project_max_rounds(raw_options: BaseModel) -> int:
    """Project the round budget: every workstream records one round."""
    options = DynamicOptions.model_validate(raw_options)
    return options.max_rounds * options.max_in_flight


PLUGIN = OrchestrationPlugin(
    id="dynamic",
    agents=AGENTS,
    options=DynamicOptions,
    state=DynamicState,
    orchestrate=orchestrate,
)
REGISTRATION = OrchestrationRegistration(
    plugin=PLUGIN,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="dynamic",
        options_type=DynamicOptions,
    ),
    project_max_rounds=_project_max_rounds,
)

__all__ = ["PLUGIN", "REGISTRATION"]
