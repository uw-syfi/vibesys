"""No merge is offered, scheduled or measured, and main does not move, before the input lands.

What a merge must beat is main's measurement, which is the input's until a merge moves main. The
search plans and starts workstreams while the input's measurement is still pending, so these
tests hold the input open for part of a generated story, then land it measured, failed or lost.
They check, while it is pending, that the planner is offered nothing to merge and that no merge
runs or is measured; once it lands, that every move of main measured no worse than main then;
and that a run restarted along the way ends with the record a live run ends with. A record
restored with verified candidates before the input lands (as a resumed run's may be) offers none
of them for a merge either.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from hypothesis import event, given, note, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    FACTS,
    INPUT_PATH,
    INPUT_THROUGHPUT,
    PLANNER_LABEL,
    TERMS,
    Host,
    World,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._main_watch import MainWatch
from tests.vibesys.orchestration.dynamic._values import BASE, merge_entry, portfolio, snapshot
from tests.vibesys.search_faults import MAX_STEPS, Step, play, steps

from vibesys.candidates.api import InputReading, Measured
from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.search.api import (
    PlannerCorrection,
    PlannerPrompt,
    RunRecord,
    RunSnapshot,
    ViolationCode,
    WorkKind,
    validate,
)
from vs_core.api import Measure, ProposeWinner
from vs_core.api.testing import RestartPolicy
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.search.api import DynamicConfig
    from vs_core.api.testing import Asked

type InputEnd = Literal["measured", "failed-workload", "lost"]


def _pending_checks(host: Host) -> None:
    """While the input is pending: nothing mergeable, no merge, no candidate measured, main put."""
    for asked in host.run.open:
        if asked.label == PLANNER_LABEL:
            context = asked.context
            assert isinstance(context, PlannerPrompt | PlannerCorrection)
            assert context.mergeable == (), context.mergeable
    measured = [item for item in host.open(Measure) if item.path != INPUT_PATH]
    assert measured == [], measured
    record = host.record
    if record is None:
        return
    assert record.baseline is None
    assert record.main in {None, FACTS.baseline}, record.main
    assert record.main_measurement is None
    assert record.merges == ()
    assert not [item for item in record.running if item.plan.kind is WorkKind.MERGE]


def _hold_input(host: Host, story: list[Step]) -> int:
    """Play ``story`` answering everything but the input's measurement; the steps it played."""
    played = 0
    for chosen in story:
        answerable = [
            item for item in host.run.open if item.until is not None and item.path != INPUT_PATH
        ]
        if not answerable:
            break  # only the input is left to answer: the clock must not run it out
        asked: Asked = answerable[chosen.pick % len(answerable)]
        if isinstance(asked.request, ProposeWinner):
            host.run.adopt(asked)
        else:
            host.answer(asked, chosen.variant)
        played += 1
        _pending_checks(host)
    return played


@dataclass(frozen=True)
class Case:
    """A story whose input is held open for its first ``land_at`` steps, then ends as ``ending``."""

    cfg: DynamicConfig
    world: World
    story: list[Step]
    land_at: int
    ending: InputEnd


@st.composite
def cases(draw: st.DrawFn) -> Case:
    """A merging world, a story, and where in it the input lands and how."""
    world: World = {
        "merging": True,
        "throughputs": draw(
            st.dictionaries(
                st.sampled_from([f"c{n}" for n in range(1, 12)]), st.floats(10, 200), max_size=11
            )
        ),
        "merger_answers": draw(st.lists(st.integers(0, 3), max_size=4)),
        # A merge the planner asks for while the input is pending names whatever is buildable.
        "merge_picks": draw(st.lists(st.integers(0, 5), max_size=4)),
    }
    return Case(
        cfg=config(max_rounds=draw(st.integers(2, 4)), max_in_flight=draw(st.integers(1, 2))),
        world=world,
        story=draw(steps()),
        land_at=draw(st.integers(0, MAX_STEPS)),
        ending=draw(st.sampled_from(["measured", "failed-workload", "lost"])),
    )


def _run(case: Case, *, restarting: bool | RestartPolicy) -> tuple[RunSnapshot | None, int]:
    """Play ``case``, the input held open until it lands; the final record and the steps held."""
    world: World = {**copy.deepcopy(case.world), "baseline": case.ending}  # the host pops answers
    host = begin(case.cfg, restarting=restarting, input_first=False, **world)
    watch = MainWatch(
        throughputs=dict(case.world.get("throughputs", {})),
        input_throughput=INPUT_THROUGHPUT if case.ending == "measured" else None,
    )
    try:
        held = _hold_input(host, case.story[: case.land_at])
        host.measure_input()
        assert host.record is not None
        assert host.record.baseline is not None
        play(host, case.story[case.land_at :], check=lambda: watch.check(host.record))
    finally:
        note(host.run.timeline())
    watch.check(host.record)
    return host.record, held


@settings(max_examples=examples(pr=10, nightly=100), deadline=None, derandomize=True, database=None)
@given(case=cases(), restarts=st.frozensets(st.integers(0, MAX_STEPS), min_size=1, max_size=4))
def test_no_merge_runs_and_main_stays_until_the_input_lands_and_a_restart_changes_nothing(
    case: Case, restarts: frozenset[int]
) -> None:
    live, held = _run(case, restarting=False)
    restarted, _ = _run(case, restarting=RestartPolicy(steps=restarts, after_stop=True))
    assert restarted == live
    merges = () if live is None else live.merges
    event(f"input {case.ending}, held for {min(held, 10) // 5 * 5}+ steps")
    event(f"merges: {sorted({item.outcome.value for item in merges})}")


def _restored(baseline: Measured | None) -> RunRecord:
    """A record restored with a verified candidate of ``h1`` that beats any input."""
    run = RunRecord(config(max_rounds=4), BASE, baseline, TERMS)
    run.parents = (snapshot("h1", 1, value=500.0),)
    return run


def test_a_restored_record_with_verified_candidates_offers_no_merge_until_the_input_lands() -> None:
    run = _restored(None)
    world = run.world()
    assert world.merge_options.checkable
    assert world.merge_options.rows == ()
    assert world.merge_options.bar("h1") is ViolationCode.MERGE_BASELINE_PENDING
    result = validate(
        portfolio(merge_entry("m1", "h1")), world.ledger, run.offered(), capacity=4, offer=None
    )
    assert not result.valid
    assert [item.code for item in result.violations] == [ViolationCode.MERGE_BASELINE_PENDING]
    row = MetricRow(name="throughput", value=INPUT_THROUGHPUT, direction="max")
    run.measured(Measured(reading=InputReading(accuracy_passed=True, metrics=(row,))))
    assert [item.hypothesis_id for item in run.world().merge_options.rows] == ["h1"]
