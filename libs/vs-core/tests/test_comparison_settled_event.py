"""Core tells a flow when a comparison settles, at the end of the last run that feeds it (#3185).

A flow that waits for a comparison to settle (``ctx.evaluation.measure_compared``) waits for
``ComparisonSettled``, so core must report it whenever a run's end leaves a comparison with no
run feeding it, and never while one still does. These tests drive the kernel through its public
events (`Lab`): agents' calls in two attempts that share a base, and the end of any running run
in any order and in any way (#3208 review: a run core blocks before it is observed never ended),
under a declared floor and without one.
"""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_comparison_liveness import Bench, base_runs, died_steps, running, seeded
from .test_measurement_comparison import FLOOR, STALENESS, Lab, ended_steps
from .test_measurement_reuse import reloaded
from .test_measurements import transition

type Subject = tuple[core.Scope, str, str]
type Step = tuple[Literal["call", "end"], int, int]


def stated(lab: Lab) -> dict[Subject, bool]:
    """Each comparison the run's view states now, by its subject, with whether it is settling."""
    view = core.run_view(lab.state)
    return {
        (s.comparison.scope, s.comparison.candidate_input, s.comparison.base_input): s.settling
        for s in view.comparisons
    }


type End = Literal[
    "sample",
    "no-metric",
    "workload",
    "cancelled",
    "infrastructure",
    "refused",
    "blocked",
    "resumed",
]
"""How a run ends: `Outcome`; blocked at its limit before any observation; or a sample that
reaches a host restarted from its stored state (the run ended while the host was down)."""
ENDS: tuple[End, ...] = (
    "sample",
    "no-metric",
    "workload",
    "cancelled",
    "infrastructure",
    "refused",
    "blocked",
    "resumed",
)


def blocked_steps(
    state: core.CoreState, run: core.SubmitMeasurement
) -> tuple[core.Transition, ...]:
    """Core ends ``run`` at its limit while it is still unobserved (its submission hung)."""
    assert run.request_id is not None
    record = state.records.record(run.request_id)
    assert record is not None
    assert record.observation is None
    step = transition(
        state, core.ReconciliationDeadline(request_id=run.request_id, now_at=record.until.at + 1.0)
    )
    ended = step.state.records.record(run.request_id)
    assert ended is not None
    assert ended.phase == core.RequestPhase.BLOCKED
    return (step,)


def ending(
    lab: Lab, run: core.SubmitMeasurement, how: End, bench: Bench
) -> tuple[core.Transition, ...]:
    """Every transition that ends ``run`` ``how``, from the lab's state."""
    assert run.request_id is not None
    assert run.plan.measured_input is not None
    lab.ended.add(run.request_id.root)
    match how:
        case "sample":
            return ended_steps(lab.state, run, bench.read(run.plan.measured_input))
        case "resumed":
            return ended_steps(reloaded(lab.state), run, bench.read(run.plan.measured_input))
        case "blocked":
            return blocked_steps(lab.state, run)
        case _:
            return died_steps(lab.state, run, how)


def told_in(transition: core.Transition) -> list[Subject]:
    """The comparisons one transition tells the search have settled, repeats kept."""
    return [
        (event.scope, event.candidate_input, event.base_input)
        for event in core.search_events(transition.state, transition.events)
        if isinstance(event, core.ComparisonSettled)
    ]


def check_told(
    lab: Lab, steps: tuple[core.Transition, ...], waiting: set[Subject] | None = None
) -> None:
    """Each transition tells exactly the comparisons a flow may wait on that it took to settled.

    ``waiting`` carries, from one call to the next, every comparison the view stated settling
    and nobody has been told of since: a flow that read it settling waits for the report. A
    comparison can leave the view while it settles (every base sample went stale) and come back
    when the run that feeds it lands; its flow is still waiting then, so it is told all the
    same. One the view did not state before the step (left out, or never stated) may be told
    when its last run ends, since the view cannot say whether a run fed it.
    """
    waiting = set() if waiting is None else waiting
    for step in steps:
        before = stated(lab)
        waiting |= {subject for subject, settling in before.items() if settling}
        lab.state = step.state
        after = stated(lab)
        said = told_in(step)
        assert len(said) == len(set(said)), "a comparison told twice in one step"
        settled = {subject for subject, settling in after.items() if not settling}
        still = {subject for subject, settling in after.items() if settling}
        assert not set(said) & still, "told while a run still feeds it"
        assert set(said) <= waiting | (set(said) - set(before)), (
            "told again, though nothing fed it since it was told"
        )
        assert waiting & settled <= set(said), "a comparison stopped settling and nobody was told"
        waiting -= set(said)
        waiting |= still


@settings(max_examples=examples(pr=60, nightly=600))
@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    steps=st.lists(
        st.tuples(
            st.sampled_from(("call", "end", "wait")),
            st.integers(0, 1),
            st.integers(0, 7),
            st.sampled_from(ENDS),
        ),
        max_size=16,
    ),
    seed=st.integers(0, 2**16),
)
@example(
    floor=FLOOR,
    spread=0.0,
    steps=[("call", 0, 0, "sample"), ("call", 1, 0, "sample"), ("end", 0, 1, "blocked")],
    seed=0,
)
@example(floor=0.0, spread=0.01, steps=[("call", 0, 0, "sample")], seed=0)
@example(
    # The close call's base run lands after the only fresh base sample went stale: the view
    # leaves the comparison out while it runs, and core told nobody when it landed.
    floor=FLOOR,
    spread=0.0,
    steps=[
        ("call", 0, 0, "sample"),
        ("end", 0, 0, "sample"),
        ("end", 0, 0, "sample"),
        ("wait", 0, 5, "sample"),
    ],
    seed=0,
)
@pytest.mark.regression
def test_every_end_of_a_run_tells_each_comparison_it_settles_exactly_once(
    floor: float, spread: float, steps: list[tuple[str, int, int, End]], seed: int
) -> None:
    """Whatever way each run ends, a comparison is told settled once, when its last run ends.

    Runs end with a sample, without one (`Outcome`), blocked by core at their limit before
    they were observed, or with a sample reaching a restarted host. Between them the run clock
    moves on (``wait``, a quarter of ``base_staleness`` per unit), so base samples go stale
    while runs that feed a comparison are under way. After the steps every run left ends with
    a sample, so every close call takes the runs it earns, and no flow is left waiting.
    """
    lab = Lab.of("a", "b", floor=floor, staleness=STALENESS)
    bench = Bench(
        {"input-1": 100.4, "input-2": 103.0, "base": 100.0}, spread, seeded(f"ends:{seed}")
    )
    waiting: set[Subject] = set()
    tail: list[tuple[str, int, int, End]] = [("end", 0, 0, "sample")] * 40
    for kind, who, which, how in [*steps, *tail]:
        name = "ab"[who]
        if kind == "call":
            lab.call(name, f"c-{name}-{which % 2}", f"input-{which % 2 + 1}")
            continue
        if kind == "wait":
            lab.advance(which * STALENESS / 4)
            continue
        live = running(lab)
        if not live:
            continue
        check_told(lab, ending(lab, live[which % len(live)], how, bench), waiting)
    assert not running(lab), "the runs a comparison takes are bounded"
    assert not any(stated(lab).values()), "every comparison settles once its runs have ended"
    assert not waiting, "every flow that waited for a comparison was told it settled"


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0], ids=["floor", "no-floor"])
def test_a_base_run_core_blocks_before_it_is_observed_still_lets_its_comparisons_settle(
    floor: float,
) -> None:
    """Two attempts share a base; a close call's extra base run hangs at submission.

    Core blocks its record at the limit, before any observation. Before, the evaluation area
    never heard of it: the run's budget entry stayed open and counted in flight for good, so
    both attempts' comparisons stayed settling with no event (every verdict waited the full
    bound) and no close call got another base run.
    """
    lab = Lab.of("a", "b", floor=floor, staleness=1e9)
    bench = Bench({"input-1": 100.4, "base": 100.0}, 0.0, seeded("blocked"))
    lab.call("a", "c-a", "input-1")
    lab.call("b", "c-b", "input-1")
    told: list[Subject] = []
    blocked = False
    for _ in range(40):
        live = running(lab)
        if not live:
            break
        extra = [r for r in live if r.plan.purpose == "baseline" and len(base_runs(lab)) > 1]
        how: End = "blocked" if extra and not blocked else "sample"
        blocked = blocked or how == "blocked"
        steps = ending(lab, extra[0] if how == "blocked" else live[0], how, bench)
        check_told(lab, steps)
        told.extend(subject for step in steps for subject in told_in(step))
    assert blocked, "a close call asked for one more base run"
    assert not running(lab)
    held = stated(lab)
    assert set(held) == {(lab.scopes[name], "input-1", "base") for name in "ab"}
    assert not any(held.values()), "no comparison is left settling with no run under way"
    assert set(told) == set(held), "each attempt's flow is told its comparison settled"


@pytest.mark.regression
def test_a_base_run_a_restarted_host_cannot_find_ends_when_recovery_blocks_it() -> None:
    """The host restarts before core's base run was dispatched; recovery blocks its record.

    Before, the run stayed in flight for good, as for a block at the limit while the host ran.
    """
    lab = Lab.of("a", staleness=1e9)
    bench = Bench({"input-1": 130.0, "base": 100.0}, 0.0, seeded("restart"))
    candidate, base = lab.call("a", "c-a", "input-1")
    check_told(lab, ending(lab, candidate, "sample", bench))
    assert base.request_id is not None
    record = lab.state.records.record(base.request_id)
    assert record is not None
    restart = record.until.at + 1.0
    lab.state = transition(reloaded(lab.state), core.RecoveryStarted(epoch=2, now_at=restart)).state
    check_told(
        lab,
        (
            transition(
                lab.state, core.ReconciliationDeadline(request_id=base.request_id, now_at=restart)
            ),
        ),
    )
    ended = lab.state.records.record(base.request_id)
    assert ended is not None
    assert ended.phase == core.RequestPhase.BLOCKED
    lab.ended.add(base.request_id.root)
    (again,) = running(lab)
    check_told(lab, ending(lab, again, "sample", bench))
    assert stated(lab) == {(lab.scopes["a"], "input-1", "base"): False}


@pytest.mark.regression
def test_a_registered_measurement_job_does_not_keep_a_comparison_settling() -> None:
    """A registered operation's job measures the candidate's input in its scope (#3208 LOW-2).

    It gives no sample, so it feeds no comparison. Before, it counted as a run under way: the
    comparison stayed settling until the job ended, and that end told nobody.
    """
    lab = Lab.of("a", staleness=1e9)
    bench = Bench({"input-1": 130.0, "base": 100.0}, 0.0, seeded("registered"))
    candidate, _ = lab.call("a", "c-a", "input-1")
    while live := running(lab):
        check_told(lab, ending(lab, live[0], "sample", bench))
    assert isinstance(candidate.plan.candidate, core.RevisionRef)
    identity = core.MeasurementIdentity.from_plan(candidate.plan, candidate.plan.candidate)
    request = core.RequestId(root="operation:registered")
    evaluation = lab.state.evaluation
    registered = evaluation.model_copy(
        update={
            "submission_budgets": (
                *evaluation.submission_budgets,
                core.SubmissionBudget(
                    scope=lab.scopes["a"],
                    identity=identity.model_copy(update={"purpose": "profile"}),
                    limit=1,
                    receipts=(core.PreparedSubmissionReceipt(request_id=request, ordinal=1),),
                ),
            ),
            "registered_jobs": (
                core.RegisteredOwnedJob(
                    operation_id=core.OperationId(root="operation:registered"),
                    expected_measurement=identity,
                    request_id=request,
                    scope=lab.scopes["a"],
                    resource_pool=core.PoolId(root="jobs"),
                    wait=core.WaitOn(requests=(request,)),
                ),
            ),
        }
    )
    (comparison,) = lab.comparisons()
    assert not core.comparison_settling(registered, comparison)


@pytest.mark.regression
@pytest.mark.parametrize("floor", [FLOOR, 0.0], ids=["floor", "no-floor"])
def test_a_close_call_whose_only_fresh_base_sample_expires_mid_run_still_settles(
    floor: float,
) -> None:
    """A close call's base run lands after the one base sample it was to join went stale.

    The view stops stating the comparison while the run is under way (no fresh base sample),
    and states it again over the new sample once the run lands. Before, core read whether the
    comparison was settling from the view alone, so the step that ended the run saw nothing
    settling and told nobody: the flow waiting in ``measure_compared`` slept to its bound
    (``SETTLE_RUNS`` measurement limits, hours on a real task).
    """
    lab = Lab.of("a", floor=floor, staleness=STALENESS)
    bench = Bench({"input-1": 100.4, "base": 100.0}, 0.0, seeded("expires"))
    subject = (lab.scopes["a"], "input-1", "base")
    for run in lab.call("a", "c-a", "input-1"):
        check_told(lab, ending(lab, run, "sample", bench))
    assert stated(lab) == {subject: True}, "the close call earned a base run"
    lab.advance(STALENESS + 1.0)
    waiting: set[Subject] = set()
    told: list[Subject] = []
    while live := running(lab):
        steps = ending(lab, live[0], "sample", bench)
        check_told(lab, steps, waiting)
        told.extend(subject for step in steps for subject in told_in(step))
    assert stated(lab) == {subject: False}
    assert told[-1:] == [subject], "the flow waiting for the comparison is told it settled"
    assert not waiting
