"""Credential-safe projections from typed executor progress."""

from __future__ import annotations

import pytest

from vs_evaluation.api import (
    EvaluationState,
    EvaluationStepResult,
    ExecutorObservation,
    ExecutorPoll,
    PollPhase,
    StageState,
)
from vs_runtime._measurement_progress import (  # test-isolation: pure projection detail; the public shell adds unrelated I/O.
    MeasurementProgress,
)
from vs_runtime.api.core import StageSettled, StageStarted


class _Recorder:
    def __init__(self) -> None:
        self.heard: list[StageStarted | StageSettled] = []

    def stage_started(self, event: StageStarted) -> None:
        self.heard.append(event)

    def stage_settled(self, event: StageSettled) -> None:
        self.heard.append(event)


@pytest.mark.parametrize(
    ("operator_failure", "expected"), [(None, "CLIENTSECRET=[REDACTED]"), ("", "")]
)
def test_failure_projection_distinguishes_omitted_from_explicitly_empty(
    operator_failure: str | None, expected: str
) -> None:
    observer = _Recorder()
    progress = MeasurementProgress(observer)
    raw_failure = "CLIENTSECRET=measurement-secret"
    terminal = ExecutorObservation(
        state=EvaluationState.FAILED,
        failure=raw_failure,
        operator_failure=operator_failure,
        stage_results=(
            EvaluationStepResult(
                name="benchmark",
                state=StageState.FAILED,
                failure=raw_failure,
                operator_failure=operator_failure,
            ),
        ),
    )

    progress.report(
        "handle",
        "measure candidate",
        ExecutorPoll(phase=PollPhase.ENDED, terminal=terminal),
    )

    settled = next(event for event in observer.heard if isinstance(event, StageSettled))
    assert settled.failure == expected
