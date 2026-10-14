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
from tests.support.vibesys_flows import across_restarts
from tests.vibesys.orchestration.multi._host import MultiHost
from tests.vibesys.orchestration.multi._host import config as multi_config
from tests.vibesys.orchestration.single._host import SingleHost
from tests.vibesys.orchestration.single._host import config as single_config
from tests.vibesys.serial_search._host import assert_flow_closed

from vs_core.api import Finish, RequestTurn, StartAttempt, Wait
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


@SEARCHES
@MODES
@settings(max_examples=examples(pr=10, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    steps=st.lists(st.tuples(st.integers(0, 7), st.integers(0, 13)), max_size=50),
    stop_before=st.none() | st.integers(0, 50),
    rounds=st.integers(1, 3),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    search: str,
    restarting: bool,
    steps: list[tuple[int, int]],
    stop_before: int | None,
    rounds: int,
) -> None:
    host = begin(search, rounds=rounds, restarting=restarting)
    stopped_at: int | None = None
    try:
        for index, (pick, variant) in enumerate(steps):
            if index == stop_before:
                host.run.stop()
                stopped_at = _starts(host)
            _check_always(host, rounds)
            if not host.step(pick, variant):
                break
        if stop_before is not None and stopped_at is None:
            host.run.stop()
            stopped_at = _starts(host)
        for _ in range(500):
            _check_always(host, rounds)
            if not host.step(0):
                break
        assert_flow_closed(host)
        finishes = [item for item in host.run.decisions if isinstance(item, Finish)]
        assert len(finishes) == 1, f"{len(finishes)} finishes: {host.run.timeline()}"
        if stopped_at is not None:
            assert _starts(host) == stopped_at, f"work began after the stop: {host.run.timeline()}"
    finally:
        note(host.run.timeline())
