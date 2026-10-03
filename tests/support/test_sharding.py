"""Properties of the CI test sharding (tests/support/sharding.py)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.sharding import assign_shards, parse_shard

_NAMES = st.text(alphabet="abcdefgh/_.", min_size=1, max_size=12)
_DURATIONS = st.dictionaries(_NAMES, st.floats(min_value=0, max_value=500), max_size=30)
_FILES = st.lists(_NAMES, min_size=1, max_size=40)
_COUNTS = st.integers(min_value=1, max_value=6)


@given(files=_FILES, durations=_DURATIONS, count=_COUNTS)
def test_every_file_lands_in_exactly_one_valid_shard(
    files: list[str], durations: dict[str, float], count: int
) -> None:
    assignment = assign_shards(files, durations, count)

    assert set(assignment) == set(files)
    assert all(1 <= shard <= count for shard in assignment.values())


@given(durations=_DURATIONS, count=_COUNTS, data=st.data())
def test_the_assignment_does_not_depend_on_collection_order(
    durations: dict[str, float], count: int, data: st.DataObject
) -> None:
    files = data.draw(_FILES)
    shuffled = data.draw(st.permutations(files))

    assert assign_shards(shuffled, durations, count) == assign_shards(files, durations, count)


@given(files=_FILES, durations=_DURATIONS, count=_COUNTS)
def test_the_heaviest_shard_exceeds_the_mean_by_less_than_one_file(
    files: list[str], durations: dict[str, float], count: int
) -> None:
    assignment = assign_shards(files, durations, count)
    unique = set(files)
    known = [durations[name] for name in unique if name in durations]
    fallback = sum(known) / len(known) if known else 1.0
    weights = {name: durations.get(name, fallback) for name in unique}
    loads = [0.0] * count
    for name, shard in assignment.items():
        loads[shard - 1] += weights[name]

    assert max(loads) <= sum(weights.values()) / count + max(weights.values()) + 1e-6


@given(count=_COUNTS, data=st.data())
def test_parse_shard_round_trips_every_valid_spec(count: int, data: st.DataObject) -> None:
    index = data.draw(st.integers(min_value=1, max_value=count))

    assert parse_shard(f"{index}/{count}") == (index, count)


@pytest.mark.parametrize("spec", ["", "1", "0/2", "3/2", "a/b", "1/", "/2", "-1/2", "1/2/3"])
def test_parse_shard_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(ValueError, match="--shard"):
        parse_shard(spec)
