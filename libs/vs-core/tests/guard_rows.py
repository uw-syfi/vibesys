"""Rows for a guard that grants only when every condition holds.

The cross product of a guard's conditions grows multiplicatively, but a defect in a guard
shows in one condition, or in an interaction of two. ``guard_rows`` keeps what finds both: the
positive row, every pair of values of any two conditions (``pairwise_rows``), and, for
conditions whose rule reads them together, the full cross of just those (``joint``).
"""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING, cast

import pytest

from vs_sim.api.testing import pairwise_rows

if TYPE_CHECKING:
    from collections.abc import Sequence

type Row = tuple[object, ...]


def guard_rows(*factors: Sequence[object], positive: Row, joint: Sequence[int] = ()) -> list[Row]:
    """The rows to test: the positive row, all pairs, and every combination of ``joint``."""
    rows: dict[Row, None] = dict.fromkeys(pairwise_rows(*factors))
    rows[positive] = None
    for combo in product(*(factors[index] for index in joint)):
        row = list(positive)
        for index, value in zip(joint, combo, strict=True):
            row[index] = value
        rows[tuple(row)] = None
    return list(rows)


def by_pairs(*factors: tuple[str, Sequence[object]]) -> pytest.MarkDecorator:
    """``parametrize`` over the pairs of values of independent factors, not their product.

    Each factor is ``(argument name, values)``. The first value of every factor is in the first row.
    """
    names = tuple(name for name, _ in factors)
    rows = pairwise_rows(*(range(len(values)) for _, values in factors))
    return pytest.mark.parametrize(
        names,
        [
            tuple(
                values[cast("int", index)] for (_, values), index in zip(factors, row, strict=True)
            )
            for row in rows
        ],
    )
