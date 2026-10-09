"""Pairwise test-case selection: every pair of values of any two factors appears in some row.

A full cross product of independent factors grows multiplicatively, but a defect
caused by the interaction of two factors is found by any row that holds that pair.
``pairwise_rows`` returns a deterministic, small set of rows covering every such pair, so a
parametrized test stays exhaustive over pairs without paying for every combination.
"""

from __future__ import annotations

from itertools import combinations, product
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

type Row = tuple[object, ...]


def _pairs(row: Row) -> set[tuple[int, object, int, object]]:
    return {(i, row[i], j, row[j]) for i, j in combinations(range(len(row)), 2)}


def pairwise_rows(*factors: Sequence[object]) -> list[Row]:
    """Rows over *factors* that together contain every pair of values of every two factors.

    Greedy: repeatedly take the first row of the cross product that covers the most
    still-uncovered pairs. The result is a pure function of the factors' order.
    """
    if len(factors) < 2:
        return list(product(*factors))
    candidates = [(row, _pairs(row)) for row in product(*factors)]
    uncovered = set().union(*(pairs for _, pairs in candidates)) if candidates else set()
    rows: list[Row] = []
    while uncovered:
        row, pairs = max(candidates, key=lambda candidate: len(candidate[1] & uncovered))
        rows.append(row)
        uncovered -= pairs
    return rows
