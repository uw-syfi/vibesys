"""Cross products thinned to a diagonal on a pull request, whole at nightly depth.

Two independent axes whose product is a grid of cases (a fault by a kind of evidence) are an
exhaustive check of a rule that does not couple the axes. A pull request runs one diagonal of
the grid, so every value of each axis is still seen; the nightly job runs all of it.
"""

from __future__ import annotations

from itertools import product
from typing import TYPE_CHECKING

from vs_sim.api.testing import Depth, depth

if TYPE_CHECKING:
    from collections.abc import Sequence


def diagonal[A, B](first: Sequence[A], second: Sequence[B], *, stride: int) -> list[tuple[A, B]]:
    """All pairs at nightly depth; at PR depth those whose indices sum to a multiple of ``stride``.

    With ``stride`` at most ``len(second)`` every element of ``first`` appears at least once.
    """
    pairs = list(product(enumerate(first), enumerate(second)))
    if depth() is Depth.NIGHTLY:
        return [(a, b) for (_, a), (_, b) in pairs]
    return [(a, b) for (i, a), (j, b) in pairs if (i + j) % stride == 0]
