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
from tests.vibesys.orchestration.issue_queue._host import Host, begin, config, suspending_view
from tests.vibesys.orchestration.issue_queue._replies import evaluated, judged
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.issue_queue.core_policy.api import project_run_snapshot
from vibesys.run.contracts import IterationTree
from vs_core.api import StartAttempt, StopMode, Wait
from vs_sim.api.testing import examples

MODES = across_restarts


def _unopened_starts(host: Host) -> int:
    """Starts core ended without a workspace: refused, lost or timed out."""
    asked = sum(
        isinstance(item, Wait) and isinstance(item.request, StartAttempt)
        for item in host.run.decisions
    )
    return asked - len(host.opened) - len(host.open(StartAttempt))


def _check_always(host: Host) -> None:
    """What holds after every step."""
    rules, where = host.config, host.run.timeline()
    assert len(host.unsettled) <= 1, f"two jobs at once: {where}"
    assert len(host.opened) <= rules.max_jobs + _unopened_starts(host), (
        f"more jobs than allowed: {where}"
    )
    for issue in host.tracker.list():
        assert issue.attempts <= rules.max_attempts_per_issue, f"too many attempts: {where}"
    titles = host.titles()
    assert len(titles) == len(set(titles)), f"an issue was filed twice: {where}"
    # Every published record projects to a sound iteration tree: the run projects each one.
    record = host.record
    if record is not None:
        tree = project_run_snapshot(record, experiment_revision=0).iterations
        assert isinstance(tree, IterationTree), f"unprojectable record: {where}"


@MODES
@example(
    story=[Step(0, 0, None)],
    stop=Stop(1, StopMode.DRAIN),
    rules={"max_rounds": 1, "max_attempts_per_issue": 1},
    lost=frozenset(),
)
@settings(max_examples=examples(pr=10, nightly=30), deadline=None, derandomize=True, database=None)
@given(
    story=steps(),
    stop=stops(),
    rules=st.fixed_dictionaries(
        {"max_rounds": st.integers(1, 3), "max_attempts_per_issue": st.integers(1, 3)}
    ),
    lost=st.frozensets(st.integers(0, 12), max_size=4),
)
def test_the_root_keeps_its_promises_whatever_core_answers_and_whenever_the_operator_stops(
    *,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
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
    try:
        play(host, story, stop, check=lambda: _check_always(host))
    finally:
        note(host.run.timeline())
