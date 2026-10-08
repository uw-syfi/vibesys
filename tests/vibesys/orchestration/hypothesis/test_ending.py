"""A hypothesis-search run's ending is derived from the work its records show."""

from __future__ import annotations

from typing import Literal

from hypothesis import given
from hypothesis import strategies as st

from vibesys.hypothesis import RoundRecord, RunEnding, derive_ending
from vibesys.metrics import MetricSpace

Kind = Literal["failed", "unmeasured", "measured_rejected", "winner"]


def _record(number: int, kind: Kind) -> RoundRecord:
    commit = f"{number:040x}"
    if kind == "failed":
        return RoundRecord(
            round_number=number, commit=None, perf_metric=None, perf_unit=None, passed=False
        )
    if kind == "unmeasured":
        # The implementer changed nothing measurable: it passed but nothing was measured.
        return RoundRecord(
            round_number=number, commit=commit, perf_metric=None, perf_unit=None, passed=True
        )
    return RoundRecord(
        round_number=number,
        commit=commit,
        perf_metric=100.0 + number,
        perf_unit="tok/s",
        perf_direction="max",
        passed=True,
        judge_verdict="pass",
        official_evaluation=True,
        perf_provenance="framework",
        candidate_retained=kind == "winner",
    )


histories = st.lists(
    st.sampled_from(("failed", "unmeasured", "measured_rejected", "winner")), max_size=8
)


@given(histories)
def test_ending_follows_the_work_the_records_show(kinds: list[Kind]) -> None:
    records = [_record(number, kind) for number, kind in enumerate(kinds, start=1)]

    ending = derive_ending(records, MetricSpace())

    if "winner" in kinds:
        assert ending is RunEnding.ADOPTED
    elif "measured_rejected" in kinds:
        assert ending is RunEnding.NO_IMPROVEMENT
    else:
        assert ending is RunEnding.NO_TRUSTED_RESULT


@given(histories)
def test_only_a_run_with_something_to_keep_succeeds(kinds: list[Kind]) -> None:
    records = [_record(number, kind) for number, kind in enumerate(kinds, start=1)]

    succeeded = derive_ending(records, MetricSpace()).succeeded

    assert succeeded == any(kind in {"winner", "measured_rejected"} for kind in kinds)
