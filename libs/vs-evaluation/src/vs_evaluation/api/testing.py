"""Public test doubles for the evaluation lifecycle API."""

from vs_evaluation.profiler_testing import FakeProfilerTurn, FakeProfilerTurnProvision
from vs_evaluation.started_operation import wait_until_executor_started
from vs_evaluation.testing import (
    FakeDeadlineFactory,
    FakeDeadlineScope,
    FakeEvaluationBackend,
    FakeEvaluationExecutor,
    FakeEvaluationSettlements,
    FakeSubmission,
    InMemoryEvaluationNamespace,
    InMemoryEvaluationStore,
)

__all__ = [
    "FakeDeadlineFactory",
    "FakeDeadlineScope",
    "FakeEvaluationBackend",
    "FakeEvaluationExecutor",
    "FakeEvaluationSettlements",
    "FakeProfilerTurn",
    "FakeProfilerTurnProvision",
    "FakeSubmission",
    "InMemoryEvaluationNamespace",
    "InMemoryEvaluationStore",
    "wait_until_executor_started",
]
