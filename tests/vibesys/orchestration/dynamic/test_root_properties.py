"""Whatever core answers and whenever the operator stops, the dynamic root keeps its promises.

A generated run is a list of steps (which open wait core answers, and how) and, maybe, a stop
before one of them. The fake core never lies; the flow must never exceed its concurrency or its
budget, never plan twice at once, never start work after the stop, and end in exactly one
`Finish` with every attempt settled. A failing example prints the run as a timeline.
"""

from __future__ import annotations

from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import RequestTurn, StartAttempt, Wait
from vs_sim.api.testing import examples

MODES = across_restarts


def _starts(host: Host) -> int:
    return sum(
        isinstance(item, Wait) and isinstance(item.request, StartAttempt)
        for item in host.run.decisions
    )


def _planning_calls(host: Host) -> int:
    """Planning calls begun: each one's turns are numbered under its own ``plan.N/`` path."""
    return len(
        {
            item.request.decision_id.root.partition("/")[0]
            for item in host.run.decisions
            if isinstance(item, Wait)
            and isinstance(item.request, RequestTurn)
            and PLANNER_LABEL in item.request.decision_id.root
        }
    )


def _check_always(host: Host) -> None:
    """What holds after every step."""
    rules, record = host.config, host.record
    where = host.run.timeline()
    assert len(host.unsettled) <= rules.max_in_flight, f"too many attempts open: {where}"
    assert len(host.open(RequestTurn, label=PLANNER_LABEL)) <= 1, f"two planner turns: {where}"
    assert len(host.opened) <= rules.start_budget, f"more starts than the budget: {where}"
    if record is not None:
        assert len(record.running) <= rules.max_in_flight, f"too many running: {where}"


@MODES
@settings(max_examples=examples(pr=10, nightly=60), deadline=None, derandomize=True, database=None)
@given(
    story=steps(),
    stop=stops(),
    rounds=st.integers(1, 3),
    in_flight=st.integers(1, 3),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    rounds: int,
    in_flight: int,
) -> None:
    host = begin(config(max_rounds=rounds, max_in_flight=in_flight), restarting=restarting)
    try:
        play(
            host,
            story,
            stop,
            check=lambda: _check_always(host),
            work=lambda: (_starts(host), _planning_calls(host)),
        )
    finally:
        note(host.run.timeline())
