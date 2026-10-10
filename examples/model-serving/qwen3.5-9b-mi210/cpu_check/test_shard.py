"""The shard assignment partitions the tests deterministically, whatever the record says."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cpu_check import shard

_functions = st.sampled_from(["test_a", "test_b", "test_c", "test_d", "test_new"])
_tests = st.lists(
    st.tuples(st.text(min_size=1, max_size=6), _functions),
    unique_by=lambda test: test[0],
    max_size=20,
)
_durations = st.dictionaries(
    st.sampled_from(["test_a", "test_b", "test_c", "test_d"]),
    st.floats(min_value=0, max_value=100, allow_nan=False),
)


@given(tests=_tests, durations=_durations, shards=st.integers(1, 8))
def test_every_test_lands_in_exactly_one_valid_shard(
    tests: list[tuple[str, str]], durations: dict[str, float], shards: int
) -> None:
    placed = shard.assign(tests, durations, shards)

    assert set(placed) == {node_id for node_id, _ in tests}
    assert all(0 <= index < shards for index in placed.values())


@given(tests=_tests, durations=_durations, shards=st.integers(1, 8), data=st.data())
def test_the_assignment_does_not_depend_on_collection_order(
    tests: list[tuple[str, str]], durations: dict[str, float], shards: int, data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(tests))

    assert shard.assign(shuffled, durations, shards) == shard.assign(tests, durations, shards)


@given(shards=st.integers(1, 8), per_shard=st.integers(1, 4))
def test_equal_weights_spread_evenly(shards: int, per_shard: int) -> None:
    tests = [(f"id{i}", "test_a") for i in range(shards * per_shard)]

    counts = [0] * shards
    for index in shard.assign(tests, {"test_a": 5.0}, shards).values():
        counts[index] += 1

    assert counts == [per_shard] * shards


@given(index=st.integers(1, 9), total=st.integers(1, 9))
def test_a_valid_spec_round_trips(index: int, total: int) -> None:
    if index > total:
        with pytest.raises(ValueError, match=shard.SHARD_ENV):
            shard.parse_shard(f"{index}/{total}")
    else:
        assert shard.parse_shard(f"{index}/{total}") == (index, total)


@pytest.mark.parametrize("spec", ["", "3", "0/4", "a/b", "1/2/3", "-1/2"])
def test_a_malformed_spec_names_the_variable(spec: str) -> None:
    with pytest.raises(ValueError, match=shard.SHARD_ENV):
        shard.parse_shard(spec)


def test_the_checked_in_record_is_readable() -> None:
    assert all(seconds >= 0 for seconds in shard.load_durations().values())
