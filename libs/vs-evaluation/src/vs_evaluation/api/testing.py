"""Public test doubles for the evaluation lifecycle API."""

from vs_evaluation.profiler_testing import FakeProfilerTurn, FakeProfilerTurnProvision
from vs_evaluation.testing import (
    FakeClock,
    FakeDeadlineFactory,
    FakeDeadlineScope,
    FakeEvaluationBackend,
    FakeEvaluationExecutor,
    FakeSubmission,
    InMemoryEvaluationStore,
)

__all__ = [
    "FakeClock",
    "FakeDeadlineFactory",
    "FakeDeadlineScope",
    "FakeEvaluationBackend",
    "FakeEvaluationExecutor",
    "FakeProfilerTurn",
    "FakeProfilerTurnProvision",
    "FakeSubmission",
    "InMemoryEvaluationStore",
]
