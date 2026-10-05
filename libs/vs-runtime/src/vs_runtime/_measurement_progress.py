"""Semantic progress of measurement jobs, for hosts that show what an evaluation is doing.

``MeasurementRequests`` polls a job again and again, so the same stage is seen many times.
``MeasurementProgress`` turns those polls into one ``stage_started`` and one ``stage_settled``
per stage of a job, in this process. A restarted host may repeat a report; the reports carry
data only, and the host that formats them decides what a repeat looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_evaluation.api import EvidenceOutcome, PollPhase, StageState, TrustedEvidence

if TYPE_CHECKING:
    from vs_evaluation.api import ExecutorPoll


@dataclass(frozen=True, slots=True)
class StageStarted:
    """One stage of a measurement job began to run."""

    handle_id: str
    purpose: str
    stage_id: str


@dataclass(frozen=True, slots=True)
class StageMetric:
    """One named measurement a stage reported."""

    name: str
    value: float
    unit: str | None


@dataclass(frozen=True, slots=True)
class StageSettled:
    """One stage of a measurement job ended, with what it measured or why it failed.

    ``passed`` is true when the stage ran to completion and its evidence did not fail.
    ``cancelled`` is true when the executor cancelled the stage instead. ``stdout`` and
    ``stderr`` are the tails of what the stage's command printed, when the executor kept them.
    """

    handle_id: str
    purpose: str
    stage_id: str
    passed: bool
    cancelled: bool
    metrics: tuple[StageMetric, ...]
    summary: str | None
    failure: str | None
    stdout: str | None = None
    stderr: str | None = None


class MeasurementObserver(Protocol):
    """Receives stage progress. It runs on the executor's thread and must not raise."""

    def stage_started(self, event: StageStarted) -> object:
        """A stage began."""
        ...

    def stage_settled(self, event: StageSettled) -> object:
        """A stage ended."""
        ...


class IgnoreMeasurement:
    """The observer of a host that shows no evaluation progress."""

    def stage_started(self, event: StageStarted) -> None:
        """Drop the report."""
        del event

    def stage_settled(self, event: StageSettled) -> None:
        """Drop the report."""
        del event


class MeasurementProgress:
    """Report each stage of each job once, from successive polls of that job."""

    def __init__(self, observer: MeasurementObserver) -> None:
        """Report to *observer*."""
        self._observer = observer
        self._started: set[tuple[str, str]] = set()
        self._settled: set[tuple[str, str]] = set()

    def report(self, handle_id: str, purpose: str, polled: ExecutorPoll) -> None:
        """Report the stages this poll shows that no earlier poll showed."""
        if polled.phase is PollPhase.RUNNING and polled.current_stage is not None:
            self._start(handle_id, purpose, polled.current_stage)
        terminal = polled.terminal
        if polled.phase is not PollPhase.ENDED or terminal is None:
            return
        for step in terminal.stage_results:
            if step.state is StageState.SKIPPED:
                continue
            self._start(handle_id, purpose, step.name)
            if (handle_id, step.name) in self._settled:
                continue
            self._settled.add((handle_id, step.name))
            evidence = None if step.result is None else TrustedEvidence.model_validate(step.result)
            self._observer.stage_settled(
                StageSettled(
                    handle_id=handle_id,
                    purpose=purpose,
                    stage_id=step.name,
                    passed=step.state is StageState.SUCCEEDED
                    and (evidence is None or evidence.outcome is not EvidenceOutcome.FAILED),
                    cancelled=step.state is StageState.CANCELED,
                    metrics=()
                    if evidence is None
                    else tuple(StageMetric(m.name, m.value, m.unit) for m in evidence.metrics),
                    summary=None if evidence is None else evidence.semantic_summary,
                    failure=step.failure,
                    stdout=step.stdout_tail,
                    stderr=step.stderr_tail,
                )
            )

    def _start(self, handle_id: str, purpose: str, stage_id: str) -> None:
        if (handle_id, stage_id) in self._started:
            return
        self._started.add((handle_id, stage_id))
        self._observer.stage_started(StageStarted(handle_id, purpose, stage_id))


__all__ = [
    "IgnoreMeasurement",
    "MeasurementObserver",
    "MeasurementProgress",
    "StageMetric",
    "StageSettled",
    "StageStarted",
]
