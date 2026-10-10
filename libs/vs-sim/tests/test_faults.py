"""Ordinal fault schedules: rules match the n-th call of a boundary, and streams are reproducible."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api.testing import CallCounter, fault_stream, match_rule


@dataclass(frozen=True)
class _Rule:
    boundary: str
    target: str | None
    at: int


NAMES = st.sampled_from(["a", "b", "c"])
RULES = st.builds(_Rule, NAMES, st.none() | NAMES, st.integers(1, 5))


@given(rules=st.lists(RULES, max_size=6), boundary=NAMES, target=NAMES, ordinal=st.integers(1, 6))
def test_a_rule_is_found_exactly_when_one_names_the_call(
    rules: list[_Rule], boundary: str, target: str, ordinal: int
) -> None:
    found = match_rule(rules, boundary, target, ordinal)
    expected = [
        rule
        for rule in rules
        if rule.boundary == boundary and rule.at == ordinal and rule.target in (None, target)
    ]
    assert found == (expected[0] if expected else None)


@given(calls=st.lists(st.tuples(NAMES, NAMES), max_size=30))
def test_ordinals_count_each_boundary_and_target_from_one(calls: list[tuple[str, str]]) -> None:
    counter = CallCounter()
    seen: dict[tuple[str, str], int] = {}
    for boundary, target in calls:
        seen[boundary, target] = seen.get((boundary, target), 0) + 1
        assert counter.next(boundary, target) == seen[boundary, target]


@given(seed=st.integers(0, 2**32), scope=st.lists(st.integers(), max_size=3))
def test_a_stream_is_reproducible_and_scopes_are_independent(seed: int, scope: list[int]) -> None:
    first = [fault_stream(seed, *scope).randint(0, 10**9) for _ in range(3)]
    assert first == [fault_stream(seed, *scope).randint(0, 10**9) for _ in range(3)]
    other = fault_stream(seed, *scope, "other")
    assert [other.randint(0, 10**9) for _ in range(3)] != first
