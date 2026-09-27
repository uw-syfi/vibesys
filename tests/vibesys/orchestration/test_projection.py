"""Public projections shared by the migrated hypothesis-search plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.api import PluginProjection
from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.hypothesis.readmodel import AgentRunProjection
from vibesys.orchestration.hypothesis.state import Hypothesis, HypothesisState
from vibesys.orchestration.metrics import MetricSpace, Objective
from vibesys.orchestration.multi import (
    PLUGIN as MULTI_PLUGIN,
)
from vibesys.orchestration.multi import (
    PROFILE_GUIDED_REGISTRATION as PROFILE_MULTI_REGISTRATION,
)
from vibesys.orchestration.multi import REGISTRATION as MULTI_REGISTRATION
from vibesys.orchestration.profile_focus import ProfileFocusState
from vibesys.orchestration.single import (
    PLUGIN as SINGLE_PLUGIN,
)
from vibesys.orchestration.single import (
    PROFILE_GUIDED_PLUGIN as PROFILE_SINGLE_PLUGIN,
)
from vibesys.orchestration.single import (
    PROFILE_GUIDED_REGISTRATION as PROFILE_SINGLE_REGISTRATION,
)
from vibesys.orchestration.single import REGISTRATION as SINGLE_REGISTRATION
from vs_loop_state.api import RoundRecord

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.plugin_catalog import OrchestrationRegistration
    from vs_runtime.api import OrchestrationPlugin


REGISTRATIONS = (
    SINGLE_REGISTRATION,
    PROFILE_SINGLE_REGISTRATION,
    MULTI_REGISTRATION,
    PROFILE_MULTI_REGISTRATION,
)


def _search_state() -> HypothesisState:
    return HypothesisState(
        experiment_revision=7,
        metrics=MetricSpace(
            objectives=(
                Objective(name="throughput", direction="max"),
                Objective(name="latency", direction="min"),
            )
        ),
        hypotheses=[
            Hypothesis(
                hypothesis_id="H-01",
                plan=OrchestratorPlan(
                    hypothesis_id="H-01",
                    title="Batch prefill",
                    hypothesis="Batching removes per-request overhead.",
                    task="Batch prefill requests.",
                    pass_criteria="",
                    reasoning="The trace shows repeated launch overhead.",
                ),
                started_round=1,
                rounds=[
                    RoundRecord(
                        round_number=1,
                        hypothesis_id="H-01",
                        commit="candidate-1",
                        passed=False,
                        attempts=2,
                        judge_verdict="fail",
                        perf_metric=42.0,
                        perf_unit="tokens/s",
                        perf_provenance="implementer",
                    )
                ],
                last_experiment_revision=7,
            )
        ],
    )


def _plugin_state(
    plugin: OrchestrationPlugin,
    *,
    search: HypothesisState | None = None,
) -> BaseModel:
    state_model = plugin.state
    assert state_model is not None
    return state_model.model_validate({"search": search or _search_state()})


@pytest.mark.parametrize(
    "registration", REGISTRATIONS, ids=lambda registration: registration.plugin.id
)
def test_plugin_projects_aggregate_state_without_mutating_it(
    registration: OrchestrationRegistration,
) -> None:
    plugin = registration.plugin
    state = _plugin_state(plugin)
    before = state.model_dump_json()
    project = registration.project
    assert project is not None

    projection = project(state)

    assert state.model_dump_json() == before
    assert isinstance(projection, PluginProjection)
    assert projection.experiment_revision == 7
    assert projection.payload is not None
    payload = AgentRunProjection.model_validate(projection.payload)
    assert payload.kind == "agent"
    assert payload.current_round == 1
    assert payload.objectives == ("throughput:max", "latency:min")
    assert payload.experiment_revision == 7
    assert payload.active_hypothesis_id is None
    assert payload.hypotheses[0].title == "Batch prefill"
    assert payload.rounds[0].commit == "candidate-1"
    assert len(projection.rounds) == 1
    assert projection.rounds[0].model_dump() == {
        "number": 1,
        "status": "failed",
        "attempts": 2,
        "judge_verdict": "fail",
        "perf_metric": 42.0,
        "perf_unit": "tokens/s",
        "profile_skipped": False,
    }


def test_profile_guidance_does_not_change_the_public_run_projection() -> None:
    plain_state = _plugin_state(SINGLE_PLUGIN)
    search = _search_state()
    search.profile_guidance = ProfileFocusState()
    profile_state = _plugin_state(PROFILE_SINGLE_PLUGIN, search=search)
    plain_project = SINGLE_REGISTRATION.project
    profile_project = PROFILE_SINGLE_REGISTRATION.project
    assert plain_project is not None
    assert profile_project is not None

    assert profile_project(profile_state) == plain_project(plain_state)


@pytest.mark.parametrize(
    ("registration", "wrong_state_plugin"),
    [
        (SINGLE_REGISTRATION, MULTI_PLUGIN),
        (MULTI_REGISTRATION, SINGLE_PLUGIN),
    ],
    ids=("single-rejects-multi", "multi-rejects-single"),
)
def test_plugin_projection_rejects_another_plugins_state_model(
    registration: OrchestrationRegistration,
    wrong_state_plugin: OrchestrationPlugin,
) -> None:
    project = registration.project
    assert project is not None

    with pytest.raises(ValidationError, match="Input should be a valid dictionary"):
        project(_plugin_state(wrong_state_plugin))
