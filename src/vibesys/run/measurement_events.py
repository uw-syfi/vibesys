"""Map measurement stage progress to the gate and subprocess events a frontend renders.

``vs_runtime`` reports what a measurement job did as data (a stage began, a stage ended with
metrics or a failure). This module is the only place that turns those facts into
``GATE_STARTED``, ``GATE_FINISHED`` and ``SUBPROCESS_OUTPUT`` core events. Frontends decide how
they look. The job handle is the event's ``execution_id``, so each event names a job that the
committed core state holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import (
    CoreEventType,
    CoreEventWriter,
    EventStatus,
    GateFinishedData,
    GateKind,
    GateStartedData,
    SubprocessOutputData,
)
from vs_core.api import DecisionId, SpanId, operation_span
from vs_redaction.api import redact_diagnostic_tail

if TYPE_CHECKING:
    from vs_runtime.api.core import MeasurementDecisions, StageSettled, StageStarted

GATE_TAIL_CHARS = 1000
_LOCAL_VALIDATION = "local-validation"
_GATES = {
    "build": GateKind.BUILD,
    "accuracy": GateKind.ACCURACY,
    "benchmark": GateKind.BENCHMARK,
}
_PROCESS_KINDS = {
    GateKind.BUILD: "candidate_build",
    GateKind.ACCURACY: "accuracy_checker",
    GateKind.BENCHMARK: "benchmark",
}


def gate_of(purpose: str | None, stage_id: str) -> GateKind | None:
    """The framework gate a stage of a job measured for ``purpose`` is, or None (profiling).

    The one mapping from stages to gates: the gate events and the gate stages' log
    streams both name a stage by it.
    """
    gate = _GATES.get(stage_id)
    if gate is GateKind.ACCURACY and purpose == _LOCAL_VALIDATION:
        return GateKind.VALIDATION
    return gate


class CoreMeasurementEvents:
    """A ``MeasurementObserver`` that writes gate events to one run's event stream."""

    def __init__(self, events: CoreEventWriter, decisions: MeasurementDecisions) -> None:
        """Write every translated event to *events*."""
        self._events = events
        self._decisions = decisions

    def _span(self, handle_id: str) -> SpanId | None:
        decision = self._decisions.decision(handle_id)
        return None if decision is None else operation_span(DecisionId(root=decision))

    def stage_started(self, event: StageStarted) -> None:
        """One ``GATE_STARTED`` for a gate stage."""
        gate = gate_of(event.purpose, event.stage_id)
        if gate is None:
            return
        self._events.emit(
            CoreEventType.GATE_STARTED,
            data=GateStartedData(gate=gate, span_id=self._span(event.handle_id)),
            status=EventStatus.ACTIVE,
            execution_id=event.handle_id,
        )

    def stage_settled(self, event: StageSettled) -> None:
        """The stage's output, then one ``GATE_FINISHED`` whose status says pass or fail."""
        gate = gate_of(event.purpose, event.stage_id)
        if gate is None:
            return
        passed = event.passed
        self._publish_output(event, gate)
        reason = event.failure or ("evaluation cancelled" if event.cancelled else None)
        metric = event.metrics[0] if passed and event.metrics else None
        self._events.emit(
            CoreEventType.GATE_FINISHED,
            data=GateFinishedData(
                gate=gate,
                span_id=self._span(event.handle_id),
                reused=event.reused,
                metric=None if metric is None or gate is not GateKind.BENCHMARK else metric.name,
                value=None if metric is None or gate is not GateKind.BENCHMARK else metric.value,
                unit=None
                if metric is None or gate is not GateKind.BENCHMARK
                else (metric.unit or metric.name),
                output_tail=(
                    None
                    if passed or reason is None
                    else redact_diagnostic_tail(reason, max_chars=GATE_TAIL_CHARS)
                ),
            ),
            status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
            execution_id=event.handle_id,
        )

    def _publish_output(self, event: StageSettled, gate: GateKind) -> None:
        kind = _PROCESS_KINDS.get(gate, "validation")
        for stream, content in (("stdout", event.stdout), ("stderr", event.stderr)):
            if content:
                self._events.emit(
                    CoreEventType.SUBPROCESS_OUTPUT,
                    data=SubprocessOutputData(
                        process_id=f"{event.handle_id}-{event.stage_id}",
                        process_kind=kind,
                        span_id=self._span(event.handle_id),
                        stream=stream,
                        content=content,
                    ),
                    execution_id=event.handle_id,
                )
