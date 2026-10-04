"""Explicit declarations of the multi-agent orchestration presets."""

from functools import partial

from pydantic import BaseModel

from vibesys.hypothesis.readmodel import project_hypothesis_state
from vibesys.orchestration.memory import declared_memory_paths
from vibesys.orchestration.multi.agents import AGENTS
from vibesys.orchestration.multi.models import MultiOptions, MultiState, ProfileGuidedMultiOptions
from vibesys.orchestration.multi.orchestration import orchestrate, orchestrate_profile_guided
from vibesys.orchestration.resume import compare_round_budget, project_round_budget
from vibesys.plugin_registration import OrchestrationRegistration
from vibesys.run.contracts import PluginProjection
from vs_runtime.api import BenchmarkObjective, MetricDirection, OrchestrationPlugin


def _input_objectives(options: BaseModel) -> tuple[BenchmarkObjective, ...]:
    """Benchmark the input tree on the run's configured objective axes."""
    if not isinstance(options, MultiOptions | ProfileGuidedMultiOptions):
        message = f"unexpected multi-agent options type {type(options).__name__}"
        raise TypeError(message)
    return tuple(
        BenchmarkObjective(name=item.name, direction=MetricDirection(item.direction))
        for item in options.metric_space.objectives
    )


def _project(raw_state: BaseModel) -> PluginProjection:
    """Project the multi-agent aggregate into its public policy view."""
    return project_hypothesis_state(MultiState.model_validate(raw_state).search)


PLUGIN = OrchestrationPlugin(
    id="multi-agent",
    agents=AGENTS,
    options=MultiOptions,
    state=MultiState,
    orchestrate=orchestrate,
    memory_paths=declared_memory_paths(),
    input_objectives=_input_objectives,
)
REGISTRATION = OrchestrationRegistration(
    plugin=PLUGIN,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="multi-agent",
        options_type=MultiOptions,
    ),
    project_max_rounds=partial(project_round_budget, options_type=MultiOptions),
)

PROFILE_GUIDED_PLUGIN = OrchestrationPlugin(
    id="profile-guided-multi-agent",
    agents=AGENTS,
    options=ProfileGuidedMultiOptions,
    state=MultiState,
    orchestrate=orchestrate_profile_guided,
    memory_paths=declared_memory_paths(),
    input_objectives=_input_objectives,
)
PROFILE_GUIDED_REGISTRATION = OrchestrationRegistration(
    plugin=PROFILE_GUIDED_PLUGIN,
    project=_project,
    resume_policy=partial(
        compare_round_budget,
        plugin_id="profile-guided-multi-agent",
        options_type=ProfileGuidedMultiOptions,
    ),
    project_max_rounds=partial(
        project_round_budget,
        options_type=ProfileGuidedMultiOptions,
    ),
)

__all__ = [
    "PLUGIN",
    "PROFILE_GUIDED_PLUGIN",
    "PROFILE_GUIDED_REGISTRATION",
    "REGISTRATION",
]
