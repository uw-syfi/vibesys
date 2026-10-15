"""Crashes that once broke recovery, kept as named cases.

Each case pins a crash point of the skeleton run (named by its boundary, target and ordinal) that
a bug was found at. They stay whatever `--depth` says: the sample of
`test_crash_convergence.py` need not reach them.
"""

from __future__ import annotations

import logging

import pytest
from tests.support.crash_harness import (
    BLOCKED_ENDINGS,
    all_crossings,
    assert_converges,
    converges_after,
    ends_as_it_must,
    following,
    name,
    rule,
    run,
)
from tests.support.skeleton_faults import RECEIPT_BEGUN

from vs_faults.api import Boundary, Crossing, FaultPlan
from vs_sim.api.testing import Depth, depth

HOLDING = "executor_request:close_session#1"
"""A crash while the skeleton's search is still alive: its flow's resources are open, the run
is closing. Named by request, not by a commit's ordinal, which moves when the run's writes do."""


def test_a_dead_host_leaves_no_search_generator_to_the_event_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The search runs inside the host's event loop but is not driven by it.

    A loop that heard of the search's async generators (a flow's ``@resource`` bodies) would
    close the ones still alive when it shuts down by running their release as a loop task, which
    cannot run a ``ctx`` wait and fails loudly. A dead host's search must start no more work: the
    next host settles the workspace from the journal. Here the host dies holding a workspace, and
    again right after the restart.
    """
    first = next(c for c in all_crossings() if name(c) == HOLDING)
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        for second in following(first, 1):
            converges_after(first, second)
    assert [r.getMessage() for r in caplog.records if r.name == "asyncio"] == []


def _commit(ordinal: int) -> Crossing:
    return Crossing(Boundary.DURABLE_WRITE, "commit", ordinal)


# The pairs the P5 review reproduced as stalls.
_REVIEWED = [
    (Crossing(Boundary.EXECUTOR_REQUEST, "ensure_session", 1), _commit(28)),
    *((_commit(37), _commit(n)) for n in (38, 39, 40)),
    (_commit(49), _commit(52)),
]
# The three pairs below are distinct stalls; the neighbours of (37, 38) differ only in how far
# into the same recovery the second death falls, which the nightly job runs.
_PULL_REQUEST = [_REVIEWED[0], _REVIEWED[1], _REVIEWED[4]]
_PAIRS = _REVIEWED if depth() is Depth.NIGHTLY else _PULL_REQUEST


@pytest.mark.parametrize(
    ("first", "second"), _PAIRS, ids=[f"{name(a)}+{name(b)}" for a, b in _PAIRS]
)
def test_the_pairs_the_review_reproduced_converge(first: Crossing, second: Crossing) -> None:
    converges_after(first, second)


def test_a_crash_recovery_cannot_tell_ends_the_run_blocked() -> None:
    """A request that began and never sealed reaches its limit while recovery still inspects it.

    The deadline's extra inspection answers "unknown" again, so the check stays blocked and
    recovery never opens. With nothing in flight, the run ends with a typed ``blocked`` result
    instead of sitting running until its deadline.
    """
    assert {"durable_write:receipt_begun#4"} == BLOCKED_ENDINGS
    for crossing in all_crossings():
        if name(crossing) in BLOCKED_ENDINGS:
            ends_as_it_must(crossing)


# Second deaths in the host that ends a blocked run: one while it inspects again after the
# block, one while it closes the attempt scope on the way out.
_AFTER_A_BLOCK = (
    Crossing(Boundary.EXECUTOR_REQUEST, "inspect_request", 2),
    Crossing(Boundary.EXECUTOR_REQUEST, "close_attempt_scope", 1),
)


@pytest.mark.slow
@pytest.mark.parametrize("second", _AFTER_A_BLOCK, ids=name)
def test_a_restart_after_recovery_blocked_keeps_the_run_blocked(second: Crossing) -> None:
    """A restart learns nothing about a record recovery blocked on, so recovery stays blocked.

    Before, the next host rebuilt its checks from the records alone. An unknown answer about a
    record core had already ended counted as settled, recovery opened, and the search resumed:
    it ended the run as a plain failure, or, when the dead host had already stopped the run as
    blocked, its finish was refused and the runtime halted.
    """
    (blocked,) = (c for c in all_crossings() if name(c) in BLOCKED_ENDINGS)
    assert second in following(blocked, depth=len(all_crossings()))
    plan = FaultPlan(seed=2, rules=(rule(blocked), rule(second)))
    summary = run(plan).summary
    replay = f"replay with {plan.model_dump_json()}"
    assert summary.stalled is None, f"{summary.stalled}; {replay}"
    assert summary.outcome == "blocked", replay


@pytest.mark.slow
def test_a_turn_whose_limit_passed_while_no_host_was_alive_still_runs() -> None:
    """The walk that found it: the turn's dispatch began and never reached the agent, then three
    more hosts died within their first commits, until the turn's limit had passed in downtime.

    A limit counts only time a host was alive to wait, so the restart gives the turn its time
    back: Sessions learns the turn never started and sends it again, and the run ends as the
    straight run does.
    """
    plan = FaultPlan(
        seed=4,
        rules=(
            rule(Crossing(Boundary.DURABLE_WRITE, RECEIPT_BEGUN, 6)),
            *(rule(_commit(n)) for n in (41, 42, 45)),
        ),
    )
    assert_converges(run(plan).summary, f"replay with {plan.model_dump_json()}", crashes=4)


def test_an_ensure_checked_before_its_session_is_known_resolves_when_the_session_is() -> None:
    """The walk #2428 reports: the host dies as a turn's receipt begins, then as the turn goes out.

    Recovery inspects the session's ensure while the session is still unknown, so the answer
    cannot show the session holds the ensured resource. The check waits on the session; the
    turn's reply makes the session known a step later (it rides the session's owner event), and
    the check is resolved again then. Before, the check named only its answered inspection and
    the run halted with an orphaned recovery-check wait.
    """
    plan = FaultPlan(
        seed=2,
        rules=(
            rule(Crossing(Boundary.DURABLE_WRITE, RECEIPT_BEGUN, 6)),
            rule(Crossing(Boundary.EXECUTOR_REQUEST, "dispatch_turn", 2)),
        ),
    )
    assert_converges(run(plan).summary, f"replay with {plan.model_dump_json()}", crashes=2)
