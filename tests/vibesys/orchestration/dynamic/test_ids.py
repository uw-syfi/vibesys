"""The keys of dynamic's concurrent flows are unique, orderable and one path component."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.orchestration.dynamic.search.api import planning_key, workstream_key
from vs_sim.api.testing import examples

WORK_IDS = st.text(max_size=20).filter(bool)
SEQUENCES = st.integers(1, 999_999)


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    first=st.tuples(SEQUENCES, WORK_IDS),
    second=st.tuples(SEQUENCES, WORK_IDS),
)
def test_workstream_keys_are_injective_and_sort_in_schedule_order(
    first: tuple[int, str], second: tuple[int, str]
) -> None:
    one, other = workstream_key(*first), workstream_key(*second)
    assert (one == other) == (first == second)
    if first[0] != second[0]:
        assert (one < other) == (first[0] < second[0])


@settings(max_examples=examples(pr=8, nightly=100))
@given(sequence=SEQUENCES, work_id=WORK_IDS, call=st.integers(1, 10_000))
def test_a_key_is_one_path_component_and_a_planning_key_never_equals_a_workstream_key(
    sequence: int, work_id: str, call: int
) -> None:
    for key in (workstream_key(sequence, work_id), planning_key(call)):
        assert "/" not in key
        assert key.split() == [key]
    assert planning_key(call) != workstream_key(sequence, work_id)


@settings(max_examples=examples(pr=8, nightly=100))
@given(calls=st.sets(st.integers(1, 10_000), min_size=2))
def test_each_planning_call_has_its_own_key(calls: set[int]) -> None:
    assert len({planning_key(call) for call in calls}) == len(calls)
