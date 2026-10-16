"""Whatever core answers and whenever the operator stops, a serial root keeps its promises.

A generated run is a list of steps (which open wait core answers, and how) and, maybe, a stop
before one of them. The fake core never lies; the flow must never run two attempts at once,
never start more rounds than its config allows, never start work after the stop, and end in
exactly one `Finish` with every attempt settled. A failing example prints the run as a timeline.
Both serial searches are held to the same promises, live and with a restart after every step.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import MultiHost
from tests.vibesys.orchestration.multi._host import config as multi_config
from tests.vibesys.orchestration.single._host import SingleHost
from tests.vibesys.orchestration.single._host import config as single_config
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.hypothesis.readmodel import project_hypothesis_state
from vibesys.run.contracts import IterationTree
from vs_core.api import RequestTurn, StartAttempt, Wait
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from tests.vibesys.serial_search._host import SerialHost

MODES = across_restarts
SEARCHES = pytest.mark.parametrize("search", ["single", "multi"])


def begin(search: str, *, rounds: int, restarting: bool) -> SerialHost:
    if search == "single":
        return SingleHost.start(single_config(max_rounds=rounds), restarting=restarting)
    return MultiHost.start(multi_config(max_rounds=rounds), restarting=restarting)


def _starts(host: SerialHost) -> int:
    return sum(
        isinstance(item, Wait) and isinstance(item.request, StartAttempt)
        for item in host.run.decisions
    )


def _check_always(host: SerialHost, rounds: int) -> None:
    """What holds after every step."""
    where = host.run.timeline()
    assert len(host.unsettled) <= 1, f"two attempts open: {where}"
    assert len(host.opened) <= rounds, f"more starts than rounds: {where}"
    designers = [item for item in host.open(RequestTurn) if item.label == "orchestrator"]
    assert len(designers) <= 1, f"two designer turns: {where}"
    # Every published record projects to a sound iteration tree: the run projects each one.
    record = host.record
    if record is not None:
        tree = project_hypothesis_state(record).iterations
        assert isinstance(tree, IterationTree), f"unprojectable record: {where}"


@SEARCHES
@MODES
@settings(max_examples=examples(pr=10, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    story=steps(),
    stop=stops(),
    rounds=st.integers(1, 3),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    search: str,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    rounds: int,
) -> None:
    host = begin(search, rounds=rounds, restarting=restarting)
    try:
        play(host, story, stop, check=lambda: _check_always(host, rounds))
    finally:
        note(host.run.timeline())
