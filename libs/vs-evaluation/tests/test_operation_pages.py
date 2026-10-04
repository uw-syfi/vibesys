"""Generated trusted observations exercise public operation paging contracts."""

from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationOperationObservation,
    EvaluationStageOutcome,
    EvaluationState,
    EvidenceKind,
    EvidenceMetric,
    EvidenceOutcome,
    OperationCursorError,
    OperationPages,
    ProfilerOperationState,
    ProfilerRunObservation,
    ProfilerWorkKey,
    ProfilerWorkPurpose,
    RunOperationsCall,
    RunOperationsReply,
)

_OWNER = "owner"
_OTHER = "other"


def _row(
    index: int, state: EvaluationState, *, failed: bool = False, revision: str = "rev"
) -> EvaluationOperationObservation:
    return EvaluationOperationObservation(
        handle_id=f"eval_{index}",
        principal_ids=("implementer",),
        candidate_content_digest="a" * 64,
        candidate_revision=revision,
        submission_index=index,
        evidence_kinds=(EvidenceKind.BENCHMARK,),
        state=state,
        evidence_recorded=True,
        stage_outcomes=(
            EvaluationStageOutcome(
                kind=EvidenceKind.BENCHMARK,
                outcome=EvidenceOutcome.FAILED if failed else EvidenceOutcome.PASSED,
                metrics=(
                    EvidenceMetric(name="throughput", value=10, unit="tokens/s", direction="max"),
                ),
            ),
        ),
    )


def _all_pages(
    pages: OperationPages, source: RunOperationsReply, call: RunOperationsCall
) -> tuple[list[str], str]:
    seen: list[str] = []
    snapshot = ""
    while True:
        reply = pages.read(call, source)
        assert len(reply.model_dump_json()) <= 4000
        snapshot = reply.snapshot_cursor or ""
        seen.extend(row.handle_id for row in reply.evaluations)
        for reference in reply.detail_required:
            detail = pages.read(RunOperationsCall(token=call.token, reference_id=reference), source)
            seen.extend(row.handle_id for row in detail.evaluations)
        if reply.next_cursor is None:
            return seen, snapshot
        call = RunOperationsCall(token=call.token, cursor=reply.next_cursor)


@given(
    st.lists(
        st.tuples(st.sampled_from(tuple(EvaluationState)), st.booleans()), min_size=0, max_size=90
    )
)
def test_pages_and_delta_return_every_row_once(values: list[tuple[EvaluationState, bool]]) -> None:
    rows = tuple(_row(index, state, failed=failed) for index, (state, failed) in enumerate(values))
    source = RunOperationsReply(evaluations=rows)
    pages = OperationPages()
    seen, snapshot = _all_pages(pages, source, RunOperationsCall(token=_OWNER))
    assert Counter(seen) == Counter(row.handle_id for row in rows)
    changed = tuple(
        row.model_copy(update={"candidate_revision": "next"}) if index % 2 else row
        for index, row in enumerate(rows)
    )
    source = RunOperationsReply(evaluations=(*changed, _row(len(rows), EvaluationState.RUNNING)))
    seen, _snapshot = _all_pages(pages, source, RunOperationsCall(token=_OWNER, since=snapshot))
    assert Counter(seen) == Counter(
        row.handle_id
        for index, row in enumerate(source.evaluations)
        if index % 2 or index == len(rows)
    )


@given(st.integers(min_value=10, max_value=100))
def test_omitted_counts_include_active_and_failed_verdicts(count: int) -> None:
    rows = tuple(
        _row(
            index,
            EvaluationState.RUNNING if index % 2 else EvaluationState.SUCCEEDED,
            failed=index % 3 == 0,
        )
        for index in range(count)
    )
    reply = OperationPages().read(
        RunOperationsCall(token=_OWNER), RunOperationsReply(evaluations=rows)
    )
    visible = {row.handle_id for row in reply.evaluations}
    omitted = tuple(row for row in rows if row.handle_id not in visible)
    assert reply.omitted_by_state == Counter(row.state for row in omitted)
    assert reply.omitted_failed_verdicts == sum(
        any(stage.outcome is EvidenceOutcome.FAILED for stage in row.stage_outcomes)
        for row in omitted
    )
    if any(row.state is EvaluationState.RUNNING for row in omitted):
        assert all(row.state is EvaluationState.RUNNING for row in reply.evaluations)


def test_oversized_row_has_bounded_detail_reference() -> None:
    row = _row(1, EvaluationState.FAILED, failed=True, revision="x" * 20000)
    source = RunOperationsReply(evaluations=(row,))
    pages = OperationPages()
    reply = pages.read(RunOperationsCall(token=_OWNER), source)
    assert len(reply.model_dump_json()) <= 4000
    assert reply.omitted_by_state == {EvaluationState.FAILED: 1}
    assert reply.omitted_failed_verdicts == 1
    detail = pages.read(
        RunOperationsCall(token=_OWNER, reference_id=reply.detail_required[0]), source
    )
    assert detail.evaluations == (row,)


def test_foreign_and_evicted_cursors_fail_explicitly() -> None:
    pages = OperationPages()
    source = RunOperationsReply(
        evaluations=tuple(_row(index, EvaluationState.RUNNING) for index in range(20))
    )
    first = pages.read(RunOperationsCall(token=_OWNER), source)
    with pytest.raises(OperationCursorError, match="cursor"):
        pages.read(RunOperationsCall(token=_OTHER, cursor=first.next_cursor), source)
    for _index in range(128):
        pages.read(RunOperationsCall(token=_OWNER), source)
    with pytest.raises(OperationCursorError, match="since"):
        pages.read(RunOperationsCall(token=_OWNER, since=first.snapshot_cursor), source)


@given(st.integers(min_value=1, max_value=30))
def test_mixed_operation_kinds_have_distinct_references(count: int) -> None:
    evaluations = tuple(_row(index, EvaluationState.RUNNING) for index in range(count))
    profilers = tuple(
        ProfilerRunObservation(
            operation_id=row.handle_id,
            session_id="session",
            principal_id="profiler",
            request="measure workload " * 500,
            work=ProfilerWorkKey(
                purpose=ProfilerWorkPurpose.TARGETED_DIAGNOSTIC, focus="attention"
            ),
            candidate_snapshot_id="revision",
            state=ProfilerOperationState.RUNNING,
            evidence_recorded=False,
        )
        for row in evaluations
    )
    source = RunOperationsReply(evaluations=evaluations, profiler_operations=profilers)
    pages = OperationPages()
    call = RunOperationsCall(token=_OWNER)
    references = []
    while True:
        reply = pages.read(call, source)
        assert len(reply.model_dump_json()) <= 4000
        references.extend((*reply.references, *reply.detail_required))
        if reply.next_cursor is None:
            break
        call = RunOperationsCall(token=_OWNER, cursor=reply.next_cursor)
    assert len(set(references)) == count * 2
    for reference in references:
        detail = pages.read(RunOperationsCall(token=_OWNER, reference_id=reference), source)
        assert len(detail.evaluations) + len(detail.profiler_operations) == 1
    seen, _snapshot = _all_pages(
        pages, source, RunOperationsCall(token=_OWNER, since=reply.snapshot_cursor)
    )
    assert not seen
