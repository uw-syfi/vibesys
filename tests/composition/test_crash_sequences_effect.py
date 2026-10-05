"""A host that crashes again right after a restart still converges (first crash at an executor request or a receipt write).

For each crossing of the run listed here the host crashes there, restarts, and crashes at each
of the next ``depth`` crossings of the recovery run. CI runs depth 1; the slow sweep runs
depth 3. Whatever the sequence, the run ends exactly as the straight run does.
"""

from __future__ import annotations

import pytest
from tests.support.crash_harness import all_crossings, converges_after, following, name

from vs_faults.api import Boundary, Crossing

CI_DEPTH = 1
SLOW_DEPTH = 3


# A turn that crashed after its begun record is inspected as a turn, and core rejects that
# inspection (no canonical invocation owner exists before the turn's first observation). What
# an interrupted turn becomes is turn policy (LIVE-ROBUST-B, #1364); flip when it lands.
_TURN_GAP = frozenset({"durable_write:receipt_begun#5"})


def _marks(crossing: Crossing) -> list[pytest.MarkDecorator]:
    if name(crossing) not in _TURN_GAP:
        return []
    return [
        pytest.mark.xfail(
            strict=True,
            reason="core rejects the inspection of a turn that never produced an observation",
        )
    ]


def _firsts() -> list[object]:
    return [
        pytest.param(c, id=name(c), marks=_marks(c))
        for c in all_crossings()
        if c.target != "commit"
    ]


@pytest.mark.parametrize("first", _firsts())
def test_a_second_crash_at_the_next_crossing_converges(first: Crossing) -> None:
    for second in following(first, CI_DEPTH):
        converges_after(first, second)


@pytest.mark.slow
@pytest.mark.parametrize("first", _firsts())
def test_a_second_crash_at_any_of_the_next_three_crossings_converges(first: Crossing) -> None:
    for second in following(first, SLOW_DEPTH):
        converges_after(first, second)


def _commit(ordinal: int) -> Crossing:
    return Crossing(Boundary.DURABLE_WRITE, "commit", ordinal)


# The pairs the P5 review reproduced as stalls.
_REVIEWED = [
    (Crossing(Boundary.EXECUTOR_REQUEST, "ensure_session", 1), _commit(28)),
    *((_commit(37), _commit(n)) for n in (38, 39, 40)),
    (_commit(49), _commit(52)),
]


@pytest.mark.parametrize(
    ("first", "second"), _REVIEWED, ids=[f"{name(a)}+{name(b)}" for a, b in _REVIEWED]
)
def test_the_pairs_the_review_reproduced_converge(first: Crossing, second: Crossing) -> None:
    converges_after(first, second)
