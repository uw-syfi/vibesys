"""Core tells a flow when a comparison settles, at the end of the last run that feeds it (#3185).

A flow that waits for a comparison to settle (``ctx.evaluation.measure_compared``) waits for
``ComparisonSettled``, so core must report it whenever a run's end leaves a comparison with no
run feeding it, and never while one still does. These tests drive the kernel through its public
events (`Lab`): agents' calls in two attempts that share a base, and the end of any running run
in any order, under a declared floor and without one.
"""

from __future__ import annotations

from typing import Literal

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core
from vs_sim.api.testing import examples

from .test_comparison_liveness import Bench, running, seeded
from .test_measurement_comparison import FLOOR, Lab, ended_transition

type Subject = tuple[core.Scope, str, str]
type Step = tuple[Literal["call", "end"], int, int]


def stated(lab: Lab) -> dict[Subject, bool]:
    """Each comparison the run's view states now, by its subject, with whether it is settling."""
    view = core.run_view(lab.state)
    return {
        (s.comparison.scope, s.comparison.candidate_input, s.comparison.base_input): s.settling
        for s in view.comparisons
    }


def told(transition: core.Transition) -> set[Subject]:
    """The comparisons a step's events tell the search have settled."""
    return {
        (event.scope, event.candidate_input, event.base_input)
        for event in core.search_events(transition.state, transition.events)
        if isinstance(event, core.ComparisonSettled)
    }


@settings(max_examples=examples(pr=40, nightly=400))
@given(
    floor=st.sampled_from((FLOOR, 0.0)),
    spread=st.sampled_from((0.0, 0.01, 0.05)),
    steps=st.lists(
        st.tuples(st.sampled_from(("call", "end")), st.integers(0, 1), st.integers(0, 7)),
        max_size=16,
    ),
    seed=st.integers(0, 2**16),
)
@example(floor=0.0, spread=0.01, steps=[("call", 0, 0)], seed=0)
def test_a_comparison_is_told_settled_exactly_when_the_last_run_feeding_it_ends(
    floor: float, spread: float, steps: list[Step], seed: int
) -> None:
    """Every comparison a run's end leaves without a feeding run is told, and no other is.

    After the steps, every run is ended in turn, so every close call takes the runs it earns.
    """
    lab = Lab.of("a", "b", floor=floor)
    bench = Bench(
        {"input-1": 100.4, "input-2": 103.0, "base": 100.0}, spread, seeded(f"told:{seed}")
    )
    tail: list[Step] = [("end", 0, 0)] * 40
    for kind, who, which in [*steps, *tail]:
        name = "ab"[who]
        if kind == "call":
            lab.call(name, f"c-{name}-{which % 2}", f"input-{which % 2 + 1}")
            continue
        live = running(lab)
        if not live:
            continue
        run = live[which % len(live)]
        assert run.request_id is not None
        assert run.plan.measured_input is not None
        before = stated(lab)
        lab.ended.add(run.request_id.root)
        step = ended_transition(lab.state, run, bench.read(run.plan.measured_input))
        lab.state = step.state
        after = stated(lab)
        settled = {subject for subject, settling in after.items() if not settling}
        assert told(step) <= settled, "told while a run still feeds it"
        stopped = {subject for subject, settling in before.items() if settling} & settled
        assert stopped <= told(step), "a comparison stopped settling and nobody was told"
    assert not running(lab), "the runs a comparison takes are bounded"
