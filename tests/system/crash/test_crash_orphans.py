"""After a host death at any point, no wait is left stuck without the run saying so.

A request a dead host dispatched is live only through a command of the new epoch (its
inspection, or its re-issue), so a wait on it either moves or becomes an orphan the shell
halts on by name. The property: wherever the host dies (once, or again in the run that
restarted), the run ends with no open wait, or the report it ends with names every wait still
open. A silent stall, where a wait claims a producer that will never answer, fails it.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.crash_harness import after_crash, all_crossings, crash_plan, rule, run

from vs_core.api import RunStatus, orphan_waits, waits
from vs_faults.api import FaultPlan
from vs_sim.api.testing import examples


@settings(deadline=None, database=None, max_examples=examples(pr=3, nightly=40))
@given(draw=st.data())
def test_a_wait_left_open_after_a_host_death_anywhere_is_reported(draw: st.DataObject) -> None:
    crossings = all_crossings()
    first = draw.draw(st.sampled_from(crossings[:-1]), label="first death")
    rules = [rule(first)]
    offered = after_crash(run(crash_plan(first)).gate.calls, first)[:-1]
    if offered and draw.draw(st.booleans(), label="dies again"):
        rules.append(rule(draw.draw(st.sampled_from(offered), label="second death")))
    plan = FaultPlan(seed=len(rules), rules=tuple(rules))
    simulated = run(plan)
    replay = f"replay with {plan.model_dump_json()}"

    report = simulated.summary.stalled or ""
    still_open = [f"{wait.waiter.value} {wait.subject}" for wait in waits(simulated.core)]
    if simulated.summary.status == RunStatus.TERMINAL:
        assert not report, f"{report}; {replay}"
    else:
        assert report, replay
        assert all(wait in report for wait in still_open), f"{still_open} vs {report}; {replay}"
    assert all(
        f"{wait.waiter.value} {wait.subject}" in report for wait in orphan_waits(simulated.core)
    ), replay
