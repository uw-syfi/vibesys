"""Dashboard and seek contracts retain presence and enforce bounded metadata."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_proto.api as p


def head() -> p.RunHead:
    return p.RunHead(
        lifecycle=p.Lifecycle(state=p.Oneof("running", p.Running())), strategy="example"
    )


@given(seq=st.one_of(st.none(), st.integers(min_value=0, max_value=2**64 - 1)))
def test_committed_sequence_preserves_absent_zero_and_positive(seq: int | None) -> None:
    value = head()
    if seq is not None:
        value.committed_seq = seq
    p.validate(value)
    decoded = p.RunHead.from_binary(value.to_binary())
    assert decoded.has_field("committed_seq") == (seq is not None)
    if seq is not None:
        assert decoded.committed_seq == seq


@given(count=st.integers(min_value=0, max_value=300))
def test_head_chart_is_bounded(count: int) -> None:
    value = head()
    value.chart_points = [p.MetricPoint(series="objective", value=1, clock=0)] * count
    if count <= 256:
        p.validate(value)
        assert p.RunHead.from_binary(value.to_binary()) == value
    else:
        with pytest.raises(p.ValidationError):
            p.validate(value)


@given(
    seq=st.integers(min_value=1, max_value=2**64 - 1),
    offset=st.integers(min_value=8, max_value=2**63 - 1),
)
def test_keyframe_seek_header_round_trips(seq: int, offset: int) -> None:
    value = p.Keyframe(through_seq=seq, log_offset=offset)
    p.validate(value)
    assert p.Keyframe.from_binary(value.to_binary()) == value


@pytest.mark.parametrize("value", [p.Keyframe(), p.Keyframe(through_seq=1, log_offset=7)])
def test_keyframe_rejects_invalid_seek_header(value: p.Keyframe) -> None:
    with pytest.raises(p.ValidationError):
        p.validate(value)


@given(
    has_lifecycle=st.booleans(),
    awaiting=st.booleans(),
    epoch=st.integers(0, 5),
    committed=st.one_of(st.none(), st.integers(0, 5)),
)
def test_epoch_awaiting_its_lifecycle_is_an_explicit_exclusive_state(
    *,
    has_lifecycle: bool,
    awaiting: bool,
    epoch: int,
    committed: int | None,
) -> None:
    value = p.RunHead(strategy="example", epoch=epoch, awaiting_lifecycle=awaiting)
    if has_lifecycle:
        value.lifecycle = head().lifecycle
    if committed is not None:
        value.committed_seq = committed
    valid = has_lifecycle != awaiting and (not awaiting or (epoch > 0 and committed is not None))
    if valid:
        p.validate(value)
        assert p.RunHead.from_binary(value.to_binary()) == value
    else:
        with pytest.raises(p.ValidationError):
            p.validate(value)
