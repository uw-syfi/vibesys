"""A keyframe carries the latest error notice of each epoch, wherever the notices sit in the log."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_proto.api import (
    Keyframe,
    MetricPoint,
    Notice,
    NoticeCode,
    Oneof,
    Record,
    Severity,
    Timestamp,
    ValidationError,
    validate,
)
from vs_runlog.api import fold_skeleton


def _record(seq: int, epoch: int, body: Oneof) -> Record:
    return Record(seq=seq, epoch=epoch, at=Timestamp(seconds=seq), body=body)


def _notice(severity: Severity) -> Oneof:
    return Oneof(
        "notice",
        Notice(severity=severity, code=NoticeCode.INPUT_MEASUREMENT_FAILED, message="notice"),
    )


@given(
    kinds=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=3), st.sampled_from(["error", "warning", "point"])
        ),
        max_size=400,
    ).map(lambda items: sorted(items, key=lambda item: item[0])),
)
def test_the_keyframe_holds_the_latest_error_notice_of_each_epoch(
    kinds: list[tuple[int, str]],
) -> None:
    """For any notice positions and any number of records after them, nothing is windowed away."""
    records = []
    for seq, (epoch, kind) in enumerate(kinds, start=1):
        if kind == "point":
            body = Oneof("metric", MetricPoint(series="progress", value=float(seq)))
        else:
            body = _notice(Severity.ERROR if kind == "error" else Severity.WARNING)
        records.append(_record(seq, epoch, body))
    keyframe = fold_skeleton(records, log_offset=8)
    expected = {
        epoch: max(
            record.seq
            for record in records
            if record.epoch == epoch
            and record.body is not None
            and record.body.field == "notice"
            and record.body.value.severity >= Severity.ERROR
        )
        for epoch in {
            record.epoch
            for record in records
            if record.body is not None
            and record.body.field == "notice"
            and record.body.value.severity >= Severity.ERROR
        }
    }
    assert {record.epoch: record.seq for record in keyframe.failures} == expected
    if records:
        validate(keyframe)


def _keyframe_with(failures: list[Record], *, tracked: bool = True, through: int = 305) -> Keyframe:
    return Keyframe(through_seq=through, log_offset=8, failures=failures, failures_tracked=tracked)


def test_a_fresh_fold_tracks_failures_and_a_seeded_one_inherits_the_flag() -> None:
    fresh = fold_skeleton([_record(1, 1, _notice(Severity.WARNING))], log_offset=8)
    assert fresh.failures_tracked
    old = Keyframe(through_seq=1, log_offset=8)  # written before the field: untracked
    resumed = fold_skeleton([_record(2, 1, _notice(Severity.ERROR))], log_offset=8, seed=old)
    assert not resumed.failures_tracked


@pytest.mark.parametrize(
    "keyframe",
    [
        _keyframe_with([_record(9999, 1, _notice(Severity.ERROR))]),
        _keyframe_with([_record(4, 1, _notice(Severity.WARNING))]),
        _keyframe_with([_record(4, 1, Oneof("metric", MetricPoint(series="a", value=1.0)))]),
        _keyframe_with(
            [_record(4, 1, _notice(Severity.ERROR)), _record(5, 1, _notice(Severity.FATAL))]
        ),
        _keyframe_with([_record(4, 1, _notice(Severity.ERROR))], tracked=False),
    ],
)
def test_a_keyframe_whose_failures_are_not_error_notices_of_its_prefix_is_invalid(
    keyframe: Keyframe,
) -> None:
    with pytest.raises(ValidationError):
        validate(keyframe)


def test_failures_of_the_prefix_are_valid() -> None:
    validate(
        _keyframe_with(
            [_record(4, 1, _notice(Severity.ERROR)), _record(300, 2, _notice(Severity.FATAL))]
        )
    )
    validate(_keyframe_with([], tracked=False))
