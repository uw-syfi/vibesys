"""Public test doubles for the evaluation lifecycle API."""

from vs_evaluation.started_operation import wait_until_executor_started
from vs_evaluation.testing import (
    FakeDeadlineFactory,
    FakeDeadlineScope,
    FakeEvaluationBackend,
    FakeEvaluationExecutor,
    FakeSubmission,
    InMemoryEvaluationNamespace,
    InMemoryEvaluationStore,
)

__all__ = [
    "FakeDeadlineFactory",
    "FakeDeadlineScope",
    "FakeEvaluationBackend",
    "FakeEvaluationExecutor",
    "FakeSubmission",
    "InMemoryEvaluationNamespace",
    "InMemoryEvaluationStore",
    "wait_until_executor_started",
]
