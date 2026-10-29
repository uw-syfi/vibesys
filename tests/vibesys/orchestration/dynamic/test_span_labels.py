"""Whatever core answers and whenever the operator stops, the dynamic search labels its spans.

The run is played from generated schedules, as `test_root_properties.py` plays it. After every
step and at the end: every workstream span says what it works on (its kind, title, hypothesis
and parent revision), an passed one names the revision it kept (as does one turned down
though its code is kept), a profile is a probe that
reaches no verdict, a workstream links to the latest workstream of the hypothesis it descends
from, every agent turn names its role, a gate that reached a verdict carries the value it
measured (and the ratio to the input's, when the input was measured), and a gate that passed
belongs to an passed workstream. Live and with a restart after every step, the labels agree.
"""

from __future__ import annotations

import json
from typing import Literal

from hypothesis import event, given, note, settings
from hypothesis import strategies as st
from tests.vibesys.agent_spawns import spawns
from tests.vibesys.orchestration.dynamic._host import (
    Host,
    begin,
    config,
    plan_config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    step,
)
from tests.vibesys.search_faults import Step, Stop, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.candidates.api import Measured
from vs_core.api import Span, SpanType
from vs_sim.api.testing import examples


def _labelled(host: Host, kind: str) -> list[Span]:
    return [s for s in host.run.trace().spans if s.label is not None and s.label.kind == kind]


def _attrs(span: Span) -> dict[str, object]:
    assert span.label is not None
    assert span.label.attrs_json is not None, f"{span.id} has no attributes"
    return json.loads(span.label.attrs_json)


def _parents_by_hypothesis(host: Host) -> dict[str, set[str | None]]:
    """The hypotheses each work id's plans build on, from what the search published."""
    record = host.record
    found: dict[str, set[str | None]] = {}
    if record is None:
        return found
    plans = [f.plan for f in record.finished] + [r.plan for r in record.running]
    for plan in plans:
        found.setdefault(plan.work_id, set()).add(plan.parent_hypothesis_id)
    for finished in record.finished:  # the hypothesis its parent revision was resolved from
        found.setdefault(finished.plan.work_id, set()).add(finished.parent_hypothesis_id)
    return found


def _check_workstreams(host: Host) -> None:
    workstreams = [s for s in host.run.trace().spans if s.name == "workstream"]
    latest: dict[str, str] = {}
    parents = _parents_by_hypothesis(host)
    for span in workstreams:
        assert span.label is not None, f"{span.id} is not labelled"
        assert span.label.kind == "workstream"
        assert span.label.title, f"{span.id} has no title"
        attrs = _attrs(span)
        hypothesis = str(attrs["hypothesis_id"])
        assert attrs["parent_revision"]
        if attrs["probe"]:
            assert span.label.verdict is None, f"{span.id} only looks, yet reached a verdict"
        kept = attrs["revision"]
        # so does one its own checks turned down, whose code is kept all the same
        keeps = span.label.verdict == "passed" or span.label.reason == "turned_down"
        assert (kept is not None) == keeps, f"{span.id} kept {kept}"
        links = [link.to for link in span.label.links if link.rel == "builds_on"]
        allowed = {latest[p] for p in parents.get(hypothesis, set()) if p in latest}
        assert set(links) <= allowed, f"{span.id} builds on {links}, not one of {allowed}"
        assert len(links) <= 1
        latest[hypothesis] = span.id


def _check_turns_and_gates(host: Host) -> None:
    turns = [s for s in host.run.trace().spans if s.invocation is not None]
    assert all(s.label is not None and s.label.kind == "agent_turn" for s in turns)
    assert all(_attrs(s)["role"] for s in turns)
    measured = host.record is not None and isinstance(host.record.baseline, Measured)
    spans = {s.id: s for s in host.run.trace().spans}
    for gate in _labelled(host, "gate"):
        assert gate.type is SpanType.OPERATION
        assert _attrs(gate)["stage"]
        assert gate.label is not None
        if gate.label.verdict == "slower":
            ratio = _attrs(gate)["vs_baseline"]
            assert ratio is None or float(str(ratio)) < 1, f"{gate.id} is slower at {ratio}"
        if gate.label.verdict in {"passed", "slower"}:
            assert _attrs(gate)["metric_value"] is not None, f"{gate.id} measured nothing"
            if measured:
                assert _attrs(gate)["vs_baseline"] is not None, f"{gate.id} has no ratio"
        if gate.label.verdict == "passed":
            assert gate.parent is not None
            owner = spans[gate.parent]
            assert owner.label is not None
            assert owner.label.verdict == "passed", f"{gate.id} passed in a failed workstream"


# who starts whom: a planning call's planner (or the outliner of a step) starts a workstream's
# agents (a step the task itself declares names none); the implementer starts the judge that
# reviews its candidate; the search starts the planner and the outliner
_PLANNED: set[tuple[str, ...]] = {("orchestrator",), ("outliner",), ()}
_STARTERS: dict[str, set[tuple[str, ...]]] = {
    "orchestrator": {()},
    "outliner": {()},
    "implementer": _PLANNED,
    "profiler": _PLANNED,
    "merger": _PLANNED,
    "judge": {("implementer",)},
}


def _check_spawns(host: Host) -> None:
    for role, starters in spawns(host.run.trace().spans).items():
        assert starters <= _STARTERS[role], f"a {role} was started by {starters}"


def _check(host: Host) -> None:
    _check_workstreams(host)
    _check_turns_and_gates(host)
    _check_spawns(host)


@across_restarts
@settings(max_examples=examples(pr=6, nightly=60), deadline=None, derandomize=True, database=None)
@given(
    story=steps(),
    stop=stops(),
    width=st.tuples(st.integers(1, 3), st.integers(1, 3)),
    world=st.tuples(st.sampled_from(["measured", "failed-workload", "no-evidence"]), st.booleans()),
)
def test_the_dynamic_search_labels_its_spans_whatever_core_answers(
    *,
    restarting: bool,
    story: list[Step],
    stop: Stop | None,
    width: tuple[int, int],
    world: tuple[Literal["measured", "failed-workload", "no-evidence"], bool],
) -> None:
    measured, slow = world
    rounds, in_flight = width
    host = begin(
        config(max_rounds=rounds, max_in_flight=in_flight),
        restarting=restarting,
        input_first=False,
        baseline=measured,
        # every candidate slower than the input (50), so gates reach "slower" too
        throughputs={f"c{n}": 10.0 for n in range(1, 40)} if slow else {},
    )
    try:
        play(host, story, stop, check=lambda: _check(host))
        _check(host)
        verdicts = sorted({str(g.label.verdict) for g in _labelled(host, "gate") if g.label})
        event(f"gate verdicts: {verdicts}")
    finally:
        note(host.run.timeline())


@across_restarts
def test_the_planner_starts_a_workstreams_implementer_which_starts_its_judge(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting)
    host.drain()

    started = spawns(host.run.trace().spans)
    assert started == {
        "orchestrator": {()},
        "implementer": {("orchestrator",)},
        "judge": {("implementer",)},
    }


@across_restarts
def test_the_outliner_starts_the_steps_it_outlined_and_a_declared_step_names_no_starter(
    *, restarting: bool
) -> None:
    outlined = begin(plan_config(), restarting=restarting)
    outlined.drain()
    declared = begin(plan_config(step("a")), restarting=restarting)
    declared.drain()

    assert spawns(outlined.run.trace().spans)["implementer"] == {("outliner",)}
    assert spawns(declared.run.trace().spans)["implementer"] == {()}


@across_restarts
def test_regression_a_kept_candidate_a_trusted_check_failed_is_a_rejected_workstream(
    *, restarting: bool
) -> None:
    # its accuracy passed, so the workspace keeps its code for later work to build on; yet its
    # benchmark check failed, and the workstream read as passed: kept work nobody vouched for
    host = begin(config(), restarting=restarting, failing_benchmarks={"c1"})
    host.drain()

    (workstream,) = [s for s in _labelled(host, "workstream") if s.label and s.label.verdict]
    assert workstream.label is not None
    assert (workstream.label.verdict, workstream.label.reason) == ("failed", "turned_down")
    assert _attrs(workstream)["revision"] == "c1", "it names the code the workspace kept"
