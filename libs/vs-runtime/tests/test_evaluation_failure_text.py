"""The evaluation failure text a submitting agent reads, rendered from templates.

Each property states the text contract the Python string building had before
the templates replaced it, so the migration is byte-identical for every input.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import EvidenceKind
from vs_runtime.api import (
    render_evaluation_failure,
    render_rejected_evidence,
    render_stage_failure,
)

_TEXT = st.text(max_size=40)
_FAILURE = st.none() | _TEXT
_CHECKS = st.lists(st.tuples(_FAILURE, st.sampled_from(EvidenceKind)), max_size=4)


@given(_CHECKS)
def test_rejected_evidence_is_one_line_per_check(
    rejected: list[tuple[str | None, EvidenceKind]],
) -> None:
    expected = "\n".join(summary or f"{kind.value} failed" for summary, kind in rejected)

    assert render_rejected_evidence(rejected) == expected


@given(_FAILURE, _FAILURE)
def test_evaluation_failure_prefers_its_own_message(
    record_failure: str | None, stage_failure: str | None
) -> None:
    expected = record_failure or stage_failure or "evaluation failed without a message"

    assert render_evaluation_failure(record_failure, stage_failure) == expected


@given(_CHECKS, _FAILURE)
def test_stage_failure_lists_failed_checks_before_the_executor_failure(
    rejected: list[tuple[str | None, EvidenceKind]], observed_failure: str | None
) -> None:
    lines = "\n".join(summary or f"{kind.value} check failed" for summary, kind in rejected)
    expected = lines or observed_failure or "a stage failed"

    assert render_stage_failure(rejected, observed_failure) == expected
