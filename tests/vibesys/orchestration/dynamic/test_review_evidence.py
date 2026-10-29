"""The judge is shown each trusted evaluation of the candidate as typed data, cut to a bound.

Which evaluations exist is core's (``ctx.evaluation.taken``); what the judge's prompt says of
each is the search's: its identity, kind, revision and verdict, the decoded metrics, and the end
of a failure text (where the cause usually is). A record the owner could not decode is listed
with no verdict, never dropped.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._values import (
    evidence_ref,
    reading_of_ref,
    revision,
)

from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.search import api as search
from vibesys.orchestration.dynamic.search.api import evaluations
from vs_core.api import EvidenceKind, ObservationStatus
from vs_sim.api.testing import examples

CANDIDATE = revision("candidate")
LIMIT = 1500
KINDS = st.sampled_from([EvidenceKind.CORRECTNESS, EvidenceKind.BENCHMARK])


@settings(max_examples=examples(pr=8, nightly=100))
@given(records=st.lists(st.tuples(KINDS, st.booleans(), st.booleans()), max_size=6))
def test_each_record_is_listed_in_order_with_the_verdict_of_its_own_reading_or_none(
    records: list[tuple[EvidenceKind, bool, bool]],
) -> None:
    refs = tuple(evidence_ref(f"e{n}", kind, CANDIDATE) for n, (kind, _, _) in enumerate(records))
    readings = tuple(
        reading_of_ref(
            ref,
            passed=passed,
            metrics=(MetricRow(name="throughput", value=float(n), direction="max"),),
        )
        for n, (ref, (_, decoded, passed)) in enumerate(zip(refs, records, strict=True))
        if decoded
    )
    listed = evaluations(refs, readings)

    assert [item.evidence_id for item in listed] == [ref.evidence_id for ref in refs]
    by_key = {reading.key: reading for reading in readings}
    for item, ref in zip(listed, refs, strict=True):
        assert (item.kind, item.revision, item.status) == (ref.kind, ref.candidate, ref.status)
        reading = by_key.get(ref.key)
        assert item.passed == (None if reading is None else reading.passed)
        assert item.metrics == (() if reading is None else reading.metrics)


@settings(max_examples=examples(pr=8, nightly=100))
@given(text=st.text(max_size=4 * LIMIT))
def test_a_failure_text_is_bounded_to_its_end_and_marked_when_cut(text: str) -> None:
    ref = evidence_ref("only", EvidenceKind.BENCHMARK, CANDIDATE)
    (item,) = evaluations((ref,), (reading_of_ref(ref, passed=False, feedback=text),))
    assert len(item.feedback) <= LIMIT
    assert item.feedback_cut == (len(text) > LIMIT)
    assert text.endswith(item.feedback)


@settings(max_examples=examples(pr=8, nightly=100))
@given(status=st.sampled_from(ObservationStatus))
def test_an_undecoded_record_keeps_core_s_own_status(status: ObservationStatus) -> None:
    ref = evidence_ref("lost", EvidenceKind.BENCHMARK, CANDIDATE, status=status)
    (item,) = evaluations((ref,), ())
    assert (item.passed, item.status, item.feedback, item.feedback_cut) == (None, status, "", False)


@settings(max_examples=examples(pr=8, nightly=100))
@given(text=st.text(alphabet="`ab\n ", max_size=60))
def test_a_fence_is_longer_than_any_backtick_run_the_text_holds_and_never_shorter_than_three(
    text: str,
) -> None:
    fence = search.fence(text)
    assert set(fence) == {"`"}
    assert len(fence) >= 3
    runs = [len(run) for run in text.replace("\n", " ").split(" ") if run and set(run) == {"`"}]
    assert all(len(fence) > size for size in runs)
    assert fence not in text


@settings(max_examples=examples(pr=8, nightly=100))
@given(text=st.text(max_size=300), limit=st.integers(1, 100))
def test_free_text_is_cut_to_its_limit_and_a_cut_is_visible(text: str, limit: int) -> None:
    head, end = search.bounded(text, limit), search.tail(text, limit)
    if len(text) <= limit:
        assert head == end == text
    else:
        assert head.startswith(text[:limit])
        assert head.endswith(search.CUT)
        assert end.endswith(text[-limit:])
        assert end.startswith(search.CUT.strip())
