"""Over any plan of steps, a run that follows it keeps the plan's order and asks nobody what to do.

A generated plan is a graph of steps without cycles, declared in any order, each evaluated or
not, run one to three wide. When core answers the ordinary way, in
any order, a step starts only once every step it depends on is done, from the revision its latest
dependency settled; no planner turn is asked for while any step is not done, and no outline turn
at all; an unevaluated step is never eligible to win; and one wide, the steps start in the order a
reader of the plan would take them: the first declared step whose dependencies are done. When core
also refuses, loses and fails waits, and the operator may stop, the planner is asked only to repair
a failed step or once every step is done, and the run still ends with every workspace settled.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    OUTLINER_LABEL,
    PLANNER_LABEL,
    Host,
    begin,
    plan_config,
)
from tests.vibesys.orchestration.dynamic._replies import step
from tests.vibesys.search_faults import Step, Stop, assert_flow_closed, play, steps, stops
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.search.api import StepState
from vs_core.api import RequestTurn, RevisionRef, Settle, StartAttempt, Wait, Withdraw
from vs_sim.api.testing import examples

MODES = across_restarts
DONE = {StepState.CHECKED, StepState.EVALUATED}


@st.composite
def plan_steps(draw: st.DrawFn, *, min_size: int = 1) -> list[dict[str, Any]]:
    """Steps whose dependencies form a graph without cycles, declared in any order."""
    count = draw(st.integers(min_value=min_size, max_value=5))
    built = []
    for index in range(count):
        earlier = [f"s{n}" for n in range(index)]
        needs = draw(st.lists(st.sampled_from(earlier), unique=True, max_size=2)) if earlier else []
        evaluate = draw(st.sampled_from(["none", "trusted"]))
        built.append(step(f"s{index}", evaluate, depends_on=tuple(needs)))
    return list(draw(st.permutations(built)))


def _step_of(decision: str) -> str:
    """The work a workstream decision belongs to: ``000002.s1/start.1`` is of ``s1``."""
    return decision.split("/", 1)[0].split(".", 1)[1]


def _issued[R](host: Host, kind: type[R]) -> list[R]:
    return [
        item.request
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, kind)
    ]


def _states(host: Host) -> dict[str, StepState]:
    record = host.record
    return {} if record is None else dict(record.step_states)


def _check_order(host: Host, plan: list[dict[str, Any]]) -> None:
    """What holds after every step, whatever core answered."""
    where = host.run.timeline()
    states = _states(host)
    needs = {item["id"]: item["depends_on"] for item in plan}
    for name, state in states.items():
        if state is StepState.RUNNING and name in needs:
            undone = [dep for dep in needs[name] if states[dep] not in DONE]
            assert not undone, f"{name} runs before {undone} are done: {where}"
    if host.open(RequestTurn, label=PLANNER_LABEL):
        waiting = [name for name, state in states.items() if state not in DONE]
        failed = [name for name, state in states.items() if state is StepState.FAILED]
        assert not waiting or failed, f"planner asked while {waiting} wait: {where}"
    if plan:
        assert not host.open(RequestTurn, label=OUTLINER_LABEL), f"outlined a plan: {where}"


def _greedy_order(plan: list[dict[str, Any]]) -> list[str]:
    """The first declared step whose dependencies are done, one at a time.

    A trusted step is measured after its work ends, and a slot frees when the work ends, so a
    step that depends on the one just started waits behind any other step that can start.
    """
    order: list[str] = []
    while len(order) < len(plan):
        ready = [
            item
            for item in plan
            if item["id"] not in order and all(dep in order for dep in item["depends_on"])
        ]
        measuring = {item["id"] for item in plan if item["evaluate"] == "trusted"} & set(order[-1:])
        unblocked = [item for item in ready if not measuring.intersection(item["depends_on"])]
        order.append((unblocked or ready)[0]["id"])
    return order


@MODES
@settings(max_examples=examples(pr=12, nightly=100), deadline=None, derandomize=True, database=None)
@given(plan=plan_steps(), width=st.integers(1, 3), picks=st.lists(st.integers(0, 7), max_size=80))
def test_steps_start_once_their_dependencies_are_done_from_the_latest_one_without_a_planner(
    *, restarting: bool, plan: list[dict[str, Any]], width: int, picks: list[int]
) -> None:
    cfg = plan_config(*plan, max_rounds=len(plan), max_in_flight=width)
    host = begin(cfg, restarting=restarting)
    try:
        for pick in [*picks, *[0] * 1000]:
            _check_order(host, plan)
            if not host.step(pick):
                break
    finally:
        note(host.run.timeline())
    assert_flow_closed(host)

    starts = _issued(host, StartAttempt)
    started = [_step_of(item.decision_id.root) for item in starts]
    ids = [item["id"] for item in plan]
    assert started[: len(plan)] == [name for name in started if name in ids]
    assert sorted(started[: len(plan)]) == sorted(ids)
    assert all(state in DONE for state in _states(host).values())
    if width == 1:
        assert started[: len(plan)] == _greedy_order(plan)

    settled = {
        _step_of(item.decision_id.root): item.disposition
        for item in _issued(host, Withdraw)
        if isinstance(item.disposition, Settle)
    }
    the_input = _input_base(starts, plan)
    for start in starts[: len(plan)]:
        name = _step_of(start.decision_id.root)
        spec = next(item for item in plan if item["id"] == name)
        latest = max(spec["depends_on"], key=started.index, default=None)
        expected = the_input if latest is None else settled[latest].candidate
        assert start.workspace.base == expected, f"{name} built on the wrong revision"
        assert settled[name].eligible is (spec["evaluate"] == "trusted")
        assert settled[name].retention == ("candidate" if spec["evaluate"] == "trusted" else "wip")


def _input_base(starts: list[StartAttempt], plan: list[dict[str, Any]]) -> RevisionRef:
    """The revision a step with no dependencies starts from: the input."""
    roots = {item["id"] for item in plan if not item["depends_on"]}
    return next(
        start.workspace.base for start in starts if _step_of(start.decision_id.root) in roots
    )


@MODES
@settings(max_examples=examples(pr=12, nightly=100), deadline=None, derandomize=True, database=None)
@given(
    plan=plan_steps(min_size=0),
    width=st.integers(1, 3),
    story=steps(),
    stop=stops(),
)
def test_whatever_core_answers_the_planner_only_repairs_and_the_run_settles(
    *,
    restarting: bool,
    plan: list[dict[str, Any]],
    width: int,
    story: list[Step],
    stop: Stop | None,
) -> None:
    cfg = plan_config(*plan, max_rounds=max(1, len(plan)), max_in_flight=width)
    host = begin(cfg, restarting=restarting)
    try:
        play(host, story, stop, check=lambda: _check_order(host, plan))
    finally:
        note(host.run.timeline())
