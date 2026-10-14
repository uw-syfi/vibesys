"""Whatever core answers and whenever the operator stops, the issue-queue root keeps its promises.

A generated run is a list of steps (which open wait core answers, and how), a few lost board
acknowledgements and, maybe, a stop before one of them. The fake core never lies; the flow must
never run two jobs at once, never exceed its job or attempt budgets, never start work after the
stop, never write the same change twice, and end in exactly one `Finish` with every attempt
settled. A failing example prints the run as a timeline.
"""

from __future__ import annotations

from hypothesis import example, given, note, settings
from hypothesis import strategies as st
from tests.support.vibesys_flows import across_restarts
from tests.vibesys.orchestration.issue_queue._host import (
    Host,
    assert_flow_closed,
    begin,
    config,
    suspending_view,
)
from tests.vibesys.orchestration.issue_queue._replies import evaluated, judged

from vs_core.api import Finish, StartAttempt, Wait
from vs_sim.api.testing import examples

MODES = across_restarts


def _starts(host: Host) -> int:
    return sum(
        isinstance(item, Wait) and isinstance(item.request, StartAttempt)
        for item in host.run.decisions
    )


def _check_always(host: Host) -> None:
    """What holds after every step."""
    rules, where = host.config, host.run.timeline()
    assert len(host.unsettled) <= 1, f"two jobs at once: {where}"
    assert len(host.opened) <= rules.max_jobs + host.refused_starts, (
        f"more jobs than allowed: {where}"
    )
    for issue in host.tracker.list():
        assert issue.attempts <= rules.max_attempts_per_issue, f"too many attempts: {where}"
    titles = host.titles()
    assert len(titles) == len(set(titles)), f"an issue was filed twice: {where}"


@MODES
@example(
    steps=[(0, 0)],
    stop_before=1,
    rules={"max_rounds": 1, "max_attempts_per_issue": 1},
    lost=frozenset(),
)
@settings(max_examples=examples(pr=10, nightly=30), deadline=None, derandomize=True, database=None)
@given(
    steps=st.lists(st.tuples(st.integers(0, 7), st.integers(0, 13)), max_size=60),
    stop_before=st.none() | st.integers(0, 60),
    rules=st.fixed_dictionaries(
        {"max_rounds": st.integers(1, 3), "max_attempts_per_issue": st.integers(1, 3)}
    ),
    lost=st.frozensets(st.integers(0, 12), max_size=4),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    restarting: bool,
    steps: list[tuple[int, int]],
    stop_before: int | None,
    rules: dict[str, int],
    lost: frozenset[int],
) -> None:
    host = begin(
        config(**rules),
        restarting=restarting,
        lost_acknowledgements=lost,
        suspending=True,
        view=suspending_view(),
        judge=[judged(bugs=("bug one",)), judged(passed=False)],
        perf=[evaluated("faster")],
    )
    stopped_at: int | None = None
    try:
        for index, (pick, variant) in enumerate(steps):
            if index == stop_before:
                host.run.stop()
                stopped_at = _starts(host)
            _check_always(host)
            if not host.step(pick, variant):
                break
        if stop_before is not None and stopped_at is None:
            host.run.stop()
            stopped_at = _starts(host)
        for _ in range(500):
            _check_always(host)
            if not host.step(0):
                break
        assert_flow_closed(host)
        finishes = [item for item in host.run.decisions if isinstance(item, Finish)]
        assert len(finishes) == 1, f"{len(finishes)} finishes: {host.run.timeline()}"
        if stopped_at is not None:
            assert _starts(host) == stopped_at, f"work began after the stop: {host.run.timeline()}"
    finally:
        note(host.run.timeline())
