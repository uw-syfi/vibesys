"""Whatever core answers and whenever the operator stops, the dynamic root keeps its promises.

A generated run is a list of steps (which open wait core answers, and how) and, maybe, a stop
before one of them. The fake core never lies; the flow must never exceed its concurrency or its
budget, never plan twice at once, never start work after the stop, never start a workstream
before the input's measurement lands (planning does not wait for it, and core may answer it at
any step or not before the stop), and end in exactly one `Finish` with every attempt settled. A
failing example prints the run as a timeline.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import Measure, ProposeWinner, RequestTurn, StartAttempt, Wait
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


def _before_the_input(host: Host) -> list[str]:
    """What the run asked for that needs the input's measurement: a workstream's start, a
    candidate's measurement, a candidate named the winner. (A stop before the measurement lands
    leaves the input standing, which compares nothing with it.)"""
    return [
        type(request).__name__
        for item in host.run.decisions
        if isinstance(item, Wait)
        and (
            isinstance(request := item.request, StartAttempt)
            or (isinstance(request, Measure) and request.plan.purpose != "baseline")
            or (
                isinstance(request, ProposeWinner)
                and request.selection.kind == "retained_candidate"
            )
        )
    ]


def _check_always(host: Host) -> None:
    """What holds after every step."""
    rules, record = host.config, host.record
    where = host.run.timeline()
    if record is None or record.baseline is None:
        assert not _before_the_input(host), f"work before the input was measured: {where}"
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
    width=st.tuples(st.integers(1, 3), st.integers(1, 3)),
    measured=st.sampled_from(["measured", "failed-workload", "no-evidence", "lost"]),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    width: tuple[int, int],
    measured: Literal["measured", "failed-workload", "no-evidence", "lost"],
) -> None:
    rounds, in_flight = width
    host = begin(
        config(max_rounds=rounds, max_in_flight=in_flight),
        restarting=restarting,
        input_first=False,
        baseline=measured,
    )
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
