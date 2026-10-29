"""Semantic progress of measurement jobs, for hosts that show what an evaluation is doing.

``MeasurementRequests`` polls a job again and again, so the same stage is seen many times.
``MeasurementProgress`` turns those polls into one ``stage_started`` and one ``stage_settled``
per stage of a job, in this process. A restarted host may repeat a report; the reports carry
data only, and the host that formats them decides what a repeat looks like.

The raw output of a stage's command is not a report: an executor that runs stages in this
process writes it, while the stage runs, to a stream a ``StageOutputOpener`` opens for it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from vs_evaluation.api import (
    MAX_EVIDENCE_SUMMARY_CHARS,
    EvidenceOutcome,
    PollPhase,
    StageState,
    TrustedEvidence,
)
from vs_redaction.api import redact_diagnostic_tail

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from vs_evaluation.api import ExecutorPoll
    from vs_sim.api import OutputSink


@dataclass(frozen=True, slots=True)
class StageStarted:
    """One stage of a measurement job began to run."""

    handle_id: str
    purpose: str
    stage_id: str


@dataclass(frozen=True, slots=True)
class StageRun:
    """One run of one stage of a measurement job.

    ``handle_id``, ``purpose`` and ``stage_id`` are the ones :class:`StageStarted` reports
    for the same stage. ``purpose`` is ``None`` when the stage's request does not name one.
    ``attempt`` counts the tries of the measurement, from 1: a
    ``RetryingEvaluationExecutor`` measures again under a new inner handle, and its
    :class:`TryHandles` names that run by the measurement's handle and try number.
    ``decision_id`` is the core decision the measurement answers, when
    :class:`MeasurementDecisions` knows it: its run-trace span is ``op:<decision_id>``.
    """

    handle_id: str
    stage_id: str
    purpose: str | None = None
    attempt: int = 1
    decision_id: str | None = None


type StageOutputOpener = Callable[[StageRun], AbstractContextManager[OutputSink]]
"""Opens the stream one stage's command writes its raw stdout and stderr to.

Called once each time an executor starts running a stage, before its command starts.
The executor writes the command's output chunks to the entered sink while the stage
runs, and exits the context when the stage has ended (passed, failed, or been
cancelled) on every path; after exit nothing more is written. The stream carries output
only: what the stage measured is reported through :class:`StageSettled`.
"""


class MeasurementDecisions:
    """Which core decision each measurement handle answers, as its submission named it.

    ``MeasurementRequests`` records every handle it submits here, so a stage's output can
    be named by the decision behind it (the gate's run-trace span). Share one with the
    requests and the stage output opener; it is used on the executors' event loop only.
    A job submitted by an earlier host is unknown here, and its stages carry no decision.
    """

    def __init__(self) -> None:
        """Know no measurement yet."""
        self._decisions: dict[str, str] = {}

    def record(self, handle_id: str, decision_id: str) -> None:
        """Remember that the measurement ``handle_id`` answers ``decision_id``."""
        self._decisions[handle_id] = decision_id

    def decision(self, handle_id: str) -> str | None:
        """Return the core decision that submitted a handle, or explicit unknown."""
        return self._decisions.get(handle_id)

    def stage_output(self, opener: StageOutputOpener) -> StageOutputOpener:
        """``opener``, called with each stage's run naming the decision its measurement answers."""

        def open_stage_output(run: StageRun) -> AbstractContextManager[OutputSink]:
            decision = self._decisions.get(run.handle_id)
            return opener(run if decision is None else replace(run, decision_id=decision))

        return open_stage_output


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
    ``reused`` is true when the job ran nothing and re-read an earlier run's stage instead.
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
    reused: bool = False


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

    def report(
        self, handle_id: str, purpose: str, polled: ExecutorPoll, *, reused: bool = False
    ) -> None:
        """Report the stages this poll shows that no earlier poll showed.

        *reused*: the job ran nothing and the poll is of the earlier run it reuses.
        """
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
                    failure=(
                        step.operator_failure
                        if step.operator_failure is not None
                        else step.operator_summary
                        if evidence is not None
                        and evidence.outcome is EvidenceOutcome.FAILED
                        and step.operator_summary is not None
                        else redact_diagnostic_tail(
                            step.failure, max_chars=MAX_EVIDENCE_SUMMARY_CHARS
                        )
                        if step.failure is not None
                        else None
                    ),
                    stdout=step.stdout_tail,
                    stderr=step.stderr_tail,
                    reused=reused,
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
    "StageOutputOpener",
    "StageRun",
    "StageSettled",
    "StageStarted",
]
