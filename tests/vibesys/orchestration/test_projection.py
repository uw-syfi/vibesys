"""The hypothesis search state every serial strategy projects, and the multi presets' registration."""

from __future__ import annotations

import pytest

from vibesys.hypothesis import OrchestratorPlan, RoundRecord
from vibesys.hypothesis.state import Hypothesis, HypothesisState
from vibesys.metrics import MetricSpace, Objective
from vibesys.orchestration.multi import PROFILE_GUIDED_REGISTRATION, REGISTRATION

REGISTRATIONS = (REGISTRATION, PROFILE_GUIDED_REGISTRATION)


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


@pytest.mark.parametrize(
    "registration", REGISTRATIONS, ids=lambda registration: registration.plugin.id
)
def test_each_multi_preset_reads_its_state_through_a_runtime_record_projector(
    registration: object,
) -> None:
    projector = getattr(registration, "projector", None)
    assert projector is not None
    assert getattr(registration, "project", None) is None
