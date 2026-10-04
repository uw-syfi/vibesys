"""Detect an evaluation failure that repeats the previous ones from the same workspace.

A failure's signature is a typed kind plus the fields that identify it: the
exception type and source line of a traceback, or the metric and value range a
stopped stage measured. Each stage keeps its own run of failures, and only a
pass of that stage ends it, so a passing accuracy stage does not hide a
benchmark that keeps stopping at the same rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_evaluation.agent_evidence import EvidenceKind, EvidenceOutcome
from vs_evaluation.agent_models import FailureKind, RepeatedFailure
from vs_evaluation.failure_signature import failure_signature
from vs_evaluation.models import EvaluationState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_evaluation.agent_evidence import PartialMeasurement
    from vs_evaluation.agent_models import EvaluationOperationSnapshot

_FIRST_REPEAT = 2

# Fixed per kind; the signature and count reach the agent as their own fields.
_INSTRUCTIONS = {
    FailureKind.TRACEBACK: (
        "This failure repeats the previous ones with the same error: your edits have not "
        "reached its cause. Before you edit or submit again, read the code at the cited "
        "file and line and the code that produces its failing values, and state the cause. "
        "Repeating an identical failure ends your attempt."
    ),
    FailureKind.MEASUREMENT: (
        "This stage stopped again with its measurement in the same range: your edits have "
        "not changed the bottleneck. Before you edit or submit again, profile or reason "
        "from the measured rate to the cost that bounds it, and state what your next edit "
        "changes about that cost."
    ),
}


def _rate_range(partial: PartialMeasurement) -> str:
    """Name the power-of-two range holding the measured value, e.g. ``[4, 8)``."""
    if partial.value <= 0:
        return "<= 0"
    low = 2.0 ** math.floor(math.log2(partial.value))
    return f"[{low:g}, {2 * low:g})"


@dataclass(frozen=True)
class FailureSignature:
    """The identity of one evaluation failure: its stage, typed kind, and key fields.

    ``stage`` is the last stage whose outcome failed, or ``None`` when the run
    failed before any stage recorded a verdict. ``kind`` is ``None`` for a
    failure with no recognizable identity, which repeats nothing.
    """

    stage: EvidenceKind | None
    kind: FailureKind | None
    key: str

    def repeats(self, other: FailureSignature) -> bool:
        """Return whether ``other`` is the same recognized failure of the same stage."""
        return self.kind is not None and self == other


def classify_failure(snapshot: EvaluationOperationSnapshot) -> FailureSignature | None:
    """Return the signature of a finished evaluation's failure, or ``None`` if it did not fail."""
    failed = [item for item in snapshot.stage_outcomes if item.outcome is EvidenceOutcome.FAILED]
    if not failed and snapshot.failure is None:
        return None
    stage = failed[-1] if failed else None
    kind = stage.kind if stage is not None else None
    if stage is not None and stage.partial_measurement is not None:
        partial = stage.partial_measurement
        unit = f" {partial.unit}" if partial.unit else ""
        return FailureSignature(
            kind, FailureKind.MEASUREMENT, f"{partial.name} in {_rate_range(partial)}{unit}"
        )
    text = snapshot.failure or (stage.summary_tail if stage is not None else None) or ""
    traceback = failure_signature(text)
    if traceback is None:
        return FailureSignature(kind, None, "")
    return FailureSignature(kind, FailureKind.TRACEBACK, traceback)


def detect_repeated_failure(
    snapshots: Sequence[EvaluationOperationSnapshot],
) -> RepeatedFailure | None:
    """Describe the last evaluation's failure when it repeats its stage's previous ones.

    ``snapshots`` are one workspace's evaluations in submission order, the
    awaited one last. An evaluation that neither failed nor succeeded (queued,
    running, canceled) is skipped. A passed stage ends that stage's run of
    failures; a run failure outside every stage ends only at an evaluation
    that finished without a failure.
    """
    runs: dict[EvidenceKind | None, list[FailureSignature]] = {}
    last: FailureSignature | None = None
    for snapshot in snapshots:
        last = None
        if snapshot.failure is None and snapshot.state is not EvaluationState.SUCCEEDED:
            continue
        for outcome in snapshot.stage_outcomes:
            if outcome.outcome is EvidenceOutcome.PASSED:
                runs.pop(outcome.kind, None)
        last = classify_failure(snapshot)
        if last is None:
            runs.pop(None, None)
            continue
        runs.setdefault(last.stage, []).append(last)
    if last is None or last.kind is None:
        return None
    count = 0
    for previous in reversed(runs[last.stage]):
        if not last.repeats(previous):
            break
        count += 1
    if count < _FIRST_REPEAT:
        return None
    return RepeatedFailure(
        kind=last.kind,
        stage=last.stage,
        signature=last.key,
        count=count,
        instruction=_INSTRUCTIONS[last.kind],
    )


__all__ = ["FailureSignature", "classify_failure", "detect_repeated_failure"]
