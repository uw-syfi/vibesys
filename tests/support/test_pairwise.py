"""``pairwise_rows`` covers every pair of values of every two factors, and no more rows than needed."""

from __future__ import annotations

from itertools import combinations, product

from hypothesis import given
from hypothesis import strategies as st
from tests.support.pairwise import pairwise_rows

FACTORS = st.lists(
    st.lists(st.integers(0, 5), min_size=1, max_size=5, unique=True), min_size=2, max_size=5
)


@given(factors=FACTORS)
def test_every_pair_of_values_of_any_two_factors_appears_in_a_row(
    factors: list[list[int]],
) -> None:
    rows = pairwise_rows(*factors)
    for i, j in combinations(range(len(factors)), 2):
        wanted = set(product(factors[i], factors[j]))
        assert {(row[i], row[j]) for row in rows} == wanted


@given(factors=FACTORS)
def test_rows_come_from_the_cross_product_and_never_exceed_it(factors: list[list[int]]) -> None:
    rows = pairwise_rows(*factors)
    assert set(rows) <= set(product(*factors))
    assert len(rows) == len(set(rows))
    assert len(rows) <= len(list(product(*factors)))


def test_the_selection_is_a_pure_function_of_the_factors() -> None:
    assert pairwise_rows("ab", "cd", "ef") == pairwise_rows("ab", "cd", "ef")


def test_a_single_factor_keeps_every_value() -> None:
    assert pairwise_rows("abc") == [("a",), ("b",), ("c",)]
