"""A host that crashes again right after a restart still converges (first crash at a run-store write).

For each crossing of the run listed here the host crashes there, restarts, and crashes at each
of the next ``depth`` crossings of the recovery run. CI runs depth 1; the slow sweep runs
depth 3. Whatever the sequence, the run ends exactly as the straight run does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import all_crossings, converges_after, following, name

if TYPE_CHECKING:
    from vs_faults.api import Crossing


CI_DEPTH = 1
SLOW_DEPTH = 3


def _firsts() -> list[object]:
    return [pytest.param(c, id=name(c)) for c in all_crossings() if c.target == "commit"]


@pytest.mark.parametrize("first", _firsts())
def test_a_second_crash_at_the_next_crossing_converges(first: Crossing) -> None:
    for second in following(first, CI_DEPTH):
        converges_after(first, second)


@pytest.mark.slow
@pytest.mark.parametrize("first", _firsts())
def test_a_second_crash_at_any_of_the_next_three_crossings_converges(first: Crossing) -> None:
    for second in following(first, SLOW_DEPTH):
        converges_after(first, second)
