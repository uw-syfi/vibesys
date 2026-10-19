"""Plan validation splits a portfolio into what may be scheduled and typed violations by field.

Validation reads only the run's record (hypotheses, what runs, profile IDs, the parents on offer)
and never raises. The properties: every rule has a scenario that breaks exactly it, at the
field the planner's correction will quote; and over any generated record and portfolio, what is
accepted fits the record, and the accepted entries alone always make a valid plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.vibesys.orchestration.dynamic._host import TERMS
from tests.vibesys.orchestration.dynamic._values import (
    BASE,
    finished_merge,
    finished_profile,
    hypothesis_record,
    implement_entry,
    merge_entry,
    portfolio,
    portfolios,
    profile_entry,
    run_record,
    snapshot,
    update_entry,
)

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.metrics import MetricRow
from vibesys.orchestration.dynamic.models import (
    PlannerHypothesisUpdate,
    PortfolioPlan,
    ProfileField,
    ProfileOffer,
)
from vibesys.orchestration.dynamic.search.api import (
    HypothesisRecord,
    MainMeasurement,
    ParentSnapshot,
    PlanCheck,
    ViolationCode,
    WorkKind,
    WorkPlan,
    validate,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from vs_core.api.search import TaskTerms

CAPACITY = 4


@dataclass
class Record:
    """What a scenario sets up in the run's record before the plan is validated."""

    hypotheses: list[HypothesisRecord] = field(default_factory=list)
    running: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    parents: list[ParentSnapshot] = field(default_factory=list)
    offer: ProfileOffer | None = None
    repairable: frozenset[str] | None = None
    """The failed plan steps a reply may continue, in a run the task's plan decides."""
    ranks: bool = False
    """Whether the run lets the reply order its evaluations."""
    merges: list[str] = field(default_factory=list)
    """The IDs of finished merges; a parent named by one is a merged main."""
    merged: list[tuple[str, str]] = field(default_factory=list)
    """The (hypothesis, revision) candidates a merge brought into main."""
    main_value: float | None = None
    """Main's measured headline throughput; None leaves main at the input, which has none."""
    pending: bool = False
    """Whether the input's measurement is still pending (a record restored before it landed)."""
    terms: TaskTerms = TERMS


def check(plan: PortfolioPlan, record: Record, capacity: int = CAPACITY) -> PlanCheck:
    """Validate ``plan`` against a run record set up as ``record`` says."""
    run = run_record(terms=record.terms)
    if record.pending:
        run.baseline = run.main.baseline = None
    for item in record.hypotheses:
        run.hypotheses[item.hypothesis_id] = item
    run.accepted.extend(
        WorkPlan(kind=WorkKind.IMPLEMENT, work_id=identifier) for identifier in record.running
    )
    run.ready_to_start(len(record.running))
    run.finished_work.extend(
        finished_profile(identifier, index) for index, identifier in enumerate(record.profiles, 90)
    )
    run.finished_work.extend(
        finished_merge(identifier, index) for index, identifier in enumerate(record.merges, 80)
    )
    run.main.merged = set(record.merged)
    if record.main_value is not None:
        row = MetricRow(name="throughput", value=record.main_value, direction="max")
        run.main.measurement = MainMeasurement(revision=BASE, merge_id="m0", metrics=(row,))
    run.parents = tuple(record.parents)
    ledger = replace(run.world().ledger, repairable=record.repairable, ranks=record.ranks)
    return validate(plan, ledger, run.offered(), capacity=capacity, offer=record.offer)


def codes(result: PlanCheck) -> list[ViolationCode]:
    return [item.code for item in result.violations]


SUPPORTED = HypothesisOutcome.SUPPORTED
FINISHED_A = hypothesis_record("a", HypothesisOutcome.INCONCLUSIVE)
GOOD_PARENT = snapshot("b", 1, value=10.0)

type Scenario = tuple[PortfolioPlan, Record, str]
"""A plan, the record it is validated against, and the field the violation must name."""


def _scenarios() -> dict[ViolationCode, Scenario]:
    keep = Record(hypotheses=[FINISHED_A])
    abandoned = hypothesis_record(
        "a", HypothesisOutcome.INCONCLUSIVE, strategy=HypothesisStrategy.ABANDONED
    )
    parented = Record(parents=[GOOD_PARENT])
    unchecked = TERMS.model_copy(update={"accuracy_stage": None})
    unsupported = ProfileOffer(fields=())
    return {
        ViolationCode.OVER_CAPACITY: (
            portfolio(implement_entry("a"), implement_entry("b")),
            Record(),
            "workstreams",
        ),
        ViolationCode.DUPLICATE_ID: (
            portfolio(implement_entry("a"), implement_entry("a")),
            Record(),
            "workstreams.1",
        ),
        ViolationCode.HYPOTHESIS_ID_USED: (
            portfolio(implement_entry("a")),
            keep,
            "workstreams.0.hypothesis_id",
        ),
        ViolationCode.HYPOTHESIS_ABANDONED: (
            portfolio(implement_entry("a", continue_hypothesis=True)),
            Record(hypotheses=[abandoned]),
            "workstreams.0.hypothesis_id",
        ),
        ViolationCode.HYPOTHESIS_IN_FLIGHT: (
            portfolio(implement_entry("a", continue_hypothesis=True)),
            Record(running=["a"]),
            "workstreams.0.hypothesis_id",
        ),
        ViolationCode.HYPOTHESIS_UNKNOWN: (
            portfolio(implement_entry("z", continue_hypothesis=True)),
            keep,
            "workstreams.0.hypothesis_id",
        ),
        ViolationCode.HYPOTHESIS_TERMINAL: (
            portfolio(implement_entry("a", continue_hypothesis=True)),
            Record(hypotheses=[hypothesis_record("a", SUPPORTED)]),
            "workstreams.0.hypothesis_id",
        ),
        ViolationCode.HYPOTHESIS_BLOCKED: (
            portfolio(implement_entry("a", continue_hypothesis=True, task="same")),
            Record(hypotheses=[hypothesis_record("a", HypothesisOutcome.BLOCKED, task="same")]),
            "workstreams.0.task",
        ),
        ViolationCode.REVISION_WITHOUT_PARENT: (
            portfolio(implement_entry("n", parent_revision="b-r1")),
            parented,
            "workstreams.0.parent_revision",
        ),
        ViolationCode.CONTINUATION_HAS_PARENT: (
            portfolio(implement_entry("a", continue_hypothesis=True, parent_hypothesis_id="b")),
            Record(hypotheses=[FINISHED_A], parents=[GOOD_PARENT]),
            "workstreams.0.parent_hypothesis_id",
        ),
        ViolationCode.REVISION_NOT_OFFERED: (
            portfolio(implement_entry("n", parent_hypothesis_id="b", parent_revision="nope")),
            parented,
            "workstreams.0.parent_revision",
        ),
        ViolationCode.PARENT_NOT_BUILDABLE: (
            portfolio(implement_entry("n", parent_hypothesis_id="b")),
            Record(),
            "workstreams.0.parent_hypothesis_id",
        ),
        ViolationCode.PROFILE_NOT_OFFERED: (
            portfolio(profile_entry("p")),
            Record(),
            "workstreams.0.kind",
        ),
        ViolationCode.PROFILE_FIELDS_UNSUPPORTED: (
            portfolio(profile_entry("p", required_fields=[ProfileField.HIP_API_TIMING])),
            Record(offer=unsupported),
            "workstreams.0.required_fields",
        ),
        ViolationCode.PROFILE_ID_USED: (
            portfolio(profile_entry("a")),
            Record(hypotheses=[FINISHED_A], offer=ProfileOffer(fields=())),
            "workstreams.0.profile_id",
        ),
        ViolationCode.UPDATE_RUNNING: (
            portfolio(implement_entry("n"), updates=(update_entry("a"),)),
            Record(hypotheses=[FINISHED_A], running=["a"]),
            "hypothesis_updates.0.hypothesis_id",
        ),
        ViolationCode.UPDATE_DUPLICATE: (
            portfolio(implement_entry("n"), updates=(update_entry("a"), update_entry("a"))),
            keep,
            "hypothesis_updates.1.hypothesis_id",
        ),
        ViolationCode.UPDATE_UNKNOWN: (
            portfolio(implement_entry("n"), updates=(update_entry("z"),)),
            keep,
            "hypothesis_updates.0.hypothesis_id",
        ),
        ViolationCode.UPDATE_INCOMPLETE: (
            portfolio(implement_entry("n"), updates=(update_entry("a"),)),
            Record(hypotheses=[hypothesis_record("a")]),
            "hypothesis_updates.0.hypothesis_id",
        ),
        ViolationCode.OUTSIDE_PLAN: (
            portfolio(implement_entry("n")),
            Record(hypotheses=[FINISHED_A], repairable=frozenset({"a"})),
            "workstreams.0",
        ),
        ViolationCode.MERGE_ID_USED: (
            portfolio(merge_entry("a", "b")),
            Record(hypotheses=[FINISHED_A], parents=[GOOD_PARENT]),
            "workstreams.0.merge_id",
        ),
        ViolationCode.MERGE_IN_FLIGHT: (
            portfolio(merge_entry("m1", "b"), merge_entry("m2", "b")),
            parented,
            "workstreams.1.kind",
        ),
        ViolationCode.MERGE_CANDIDATE_REPEATED: (
            portfolio(merge_entry("m", "b", "b")),
            parented,
            "workstreams.0.candidates.1",
        ),
        ViolationCode.MERGE_CANDIDATE_IS_MAIN: (
            portfolio(merge_entry("m2", "m1")),
            Record(parents=[snapshot("m1", 1, value=10.0)], merges=["m1"]),
            "workstreams.0.candidates.0",
        ),
        ViolationCode.MERGE_CANDIDATE_MERGED: (
            portfolio(merge_entry("m", "b")),
            Record(parents=[GOOD_PARENT], merged=[("b", "b-r1")]),
            "workstreams.0.candidates.0",
        ),
        ViolationCode.MERGE_CANDIDATE_NOT_BETTER: (
            portfolio(merge_entry("m", "b")),
            Record(parents=[GOOD_PARENT], main_value=10.0),
            "workstreams.0.candidates.0",
        ),
        ViolationCode.MERGE_BASELINE_PENDING: (
            portfolio(merge_entry("m", "b")),
            Record(parents=[GOOD_PARENT], pending=True),
            "workstreams.0.candidates.0",
        ),
        ViolationCode.MERGE_UNCHECKABLE: (
            portfolio(merge_entry("m", "b")),
            Record(parents=[GOOD_PARENT], terms=unchecked),
            "workstreams.0.kind",
        ),
        ViolationCode.EXPECTATION_UNKNOWN: (
            portfolio(implement_entry("n"), expected=("n", "z")),
            Record(),
            "handoff.expectations.1.work_id",
        ),
        ViolationCode.RANKING_UNKNOWN: (
            portfolio(implement_entry("n"), order=("n", "z")),
            Record(hypotheses=[FINISHED_A], ranks=True),
            "evaluation_order.1",
        ),
        ViolationCode.RANKING_DUPLICATE: (
            portfolio(implement_entry("n"), order=("a", "n", "a")),
            Record(hypotheses=[FINISHED_A], ranks=True),
            "evaluation_order.2",
        ),
    }


SCENARIOS = _scenarios()


def _where(path: tuple[str | int, ...]) -> str:
    return ".".join(str(item) for item in path)


def test_every_violation_code_has_a_scenario() -> None:
    assert set(SCENARIOS) == set(ViolationCode)


@settings(max_examples=examples(pr=8, nightly=100))
@given(code=st.sampled_from(sorted(SCENARIOS, key=str)))
def test_a_broken_rule_is_named_by_its_code_at_the_field_the_planner_must_fix(
    code: ViolationCode,
) -> None:
    plan, record, where = SCENARIOS[code]
    result = check(plan, record, capacity=1 if code is ViolationCode.OVER_CAPACITY else CAPACITY)
    named = [item for item in result.violations if item.code is code]
    assert len(named) == 1, result.violations
    assert _where(named[0].path) == where
    assert not result.valid


def test_a_merge_is_never_a_repair_of_a_failed_plan() -> None:
    result = check(
        portfolio(merge_entry("m", "b")),
        Record(hypotheses=[FINISHED_A], parents=[GOOD_PARENT], repairable=frozenset({"a"})),
    )
    assert codes(result) == [ViolationCode.OUTSIDE_PLAN]
    assert _where(result.violations[0].path) == "workstreams.0"


def test_a_plan_that_breaks_no_rule_is_accepted_whole_in_order() -> None:
    plan = portfolio(
        implement_entry("n"),
        implement_entry("a", continue_hypothesis=True),
        implement_entry("m", parent_hypothesis_id="b", parent_revision="b-r1"),
        updates=(update_entry("c", "abandoned"),),
    )
    record = Record(
        hypotheses=[FINISHED_A, hypothesis_record("c", HypothesisOutcome.INCONCLUSIVE)],
        parents=[GOOD_PARENT],
    )
    result = check(plan, record)
    assert result.valid
    assert [item.work_id for item in result.accepted] == ["n", "a", "m"]
    assert [(item.hypothesis_id, item.disposition) for item in result.updates] == [
        ("c", HypothesisStrategy.ABANDONED)
    ]


def test_a_run_that_ranks_evaluations_takes_the_replys_order_of_known_and_planned_ids() -> None:
    plan = portfolio(implement_entry("n"), order=("n", "a"))
    result = check(plan, Record(hypotheses=[FINISHED_A], ranks=True))
    assert result.valid
    assert result.ranking == ("n", "a")
    assert check(portfolio(implement_entry("n")), Record(ranks=True)).ranking is None


def test_a_ranking_may_name_a_merge_whose_commit_waits_for_its_measurement() -> None:
    """A merge's commit is queued for its trusted measurement like a candidate, under its ID."""
    plan = portfolio(implement_entry("n"), order=("m1", "n"))
    result = check(plan, Record(merges=["m1"], ranks=True))
    assert result.valid, result.violations
    assert result.ranking == ("m1", "n")


@settings(max_examples=examples(pr=8, nightly=100))
@given(order=st.lists(st.sampled_from(["a", "n", "z"]), max_size=4).map(tuple))
def test_a_run_that_picks_the_newest_ignores_any_order_the_reply_gives(
    order: tuple[str, ...],
) -> None:
    """The order is read only when the run lets the orchestrator rank; a reply that gives one
    anyway, known IDs or not, is not sent back for it."""
    result = check(portfolio(implement_entry("n"), order=order), Record(hypotheses=[FINISHED_A]))
    assert result.valid, result.violations
    assert result.ranking is None
    assert [item.work_id for item in result.accepted] == ["n"]


@st.composite
def records(draw: st.DrawFn) -> Record:
    """A run record: some hypotheses with rounds, some running, some profiles, some parents."""
    ids = draw(st.lists(st.sampled_from("abcdefgh"), unique=True, max_size=6))
    hypotheses = [
        hypothesis_record(
            identifier,
            *draw(st.lists(st.sampled_from(list(HypothesisOutcome)), max_size=2)),
            task=draw(st.sampled_from(["same", "other"])),
            strategy=draw(st.sampled_from(list(HypothesisStrategy))),
        )
        for identifier in ids
    ]
    running = draw(st.lists(st.sampled_from(ids), unique=True, max_size=2)) if ids else []
    spare = [item for item in "abcdefgh" if item not in ids]
    profiles = draw(st.lists(st.sampled_from(spare), unique=True, max_size=2)) if spare else []
    parents = [
        snapshot(draw(st.sampled_from("abcdefgh")), n, value=draw(st.none() | st.just(5.0)))
        for n in range(1, draw(st.integers(0, 3)) + 1)
    ]
    offer = draw(st.none() | st.just(ProfileOffer(fields=tuple(ProfileField))))
    left = [item for item in spare if item not in profiles]
    merges = draw(st.lists(st.sampled_from(left), unique=True, max_size=2)) if left else []
    revisions = [(item.hypothesis_id, item.revision.revision_id.root) for item in parents]
    merged = draw(st.lists(st.sampled_from(revisions), unique=True)) if revisions else []
    return Record(
        hypotheses,
        running,
        profiles,
        parents,
        offer,
        merges=merges,
        merged=merged,
        main_value=draw(st.none() | st.sampled_from([1.0, 5.0, 9.0])),
        terms=draw(st.sampled_from([TERMS, TERMS.model_copy(update={"accuracy_stage": None})])),
    )


def _mergeable(record: Record) -> set[str]:
    """The hypotheses a merge may name, by the rule restated independently of the search.

    A candidate is a hypothesis's latest offered revision; it is mergeable when the task has an
    accuracy check, it is no merged main, it was not merged, and it beats main's throughput.
    """
    if record.terms.accuracy_stage is None:
        return set()
    latest: dict[str, ParentSnapshot] = {}
    for item in record.parents:
        known = latest.get(item.hypothesis_id)
        if known is None or item.submission_index > known.submission_index:
            latest[item.hypothesis_id] = item
    found = set()
    for identifier, item in latest.items():
        value = item.benchmark.metrics[0].value if item.benchmark else None
        better = record.main_value is None or (value is not None and value > record.main_value)
        merged = (identifier, item.revision.revision_id.root) in record.merged
        if identifier not in record.merges and not merged and better:
            found.add(identifier)
    return found


def _entry_positions(result: PlanCheck) -> set[int]:
    return {
        item.path[1]
        for item in result.violations
        if item.path[0] == "workstreams" and len(item.path) > 1 and isinstance(item.path[1], int)
    }


@settings(max_examples=examples(pr=10, nightly=150), deadline=None, derandomize=True, database=None)
@given(plan=portfolios, record=records(), capacity=st.integers(0, 4))
def test_what_is_accepted_fits_the_record_and_alone_makes_a_valid_plan(
    plan: PortfolioPlan, record: Record, capacity: int
) -> None:
    result = check(plan, record, capacity)

    assert check(plan, record, capacity) == result  # a pure function of its inputs
    accepted = [item.work_id for item in result.accepted]
    assert len(accepted) == len(set(accepted)) <= capacity
    known = {item.hypothesis_id: item for item in record.hypotheses}
    mergeable = _mergeable(record)
    for item in result.accepted:
        if item.kind is WorkKind.MERGE:
            assert set(item.candidates) <= mergeable, (item.candidates, mergeable)
        if item.kind is WorkKind.PROFILE:
            assert record.offer is not None
            assert item.work_id not in known
            assert item.work_id not in record.profiles
            continue
        assert item.work_id not in record.profiles
        assert item.work_id not in record.running
        if item.continue_hypothesis:
            prior = known[item.work_id]
            assert prior.strategy is not HypothesisStrategy.ABANDONED
            assert not prior.rounds or prior.rounds[-1].outcome not in {
                HypothesisOutcome.SUPPORTED,
                HypothesisOutcome.NOMINATED,
                HypothesisOutcome.DISPROVEN,
            }
        else:
            assert item.work_id not in known
    for update in result.updates:
        assert known[update.hypothesis_id].rounds
        assert update.hypothesis_id not in record.running
    updated = [item.hypothesis_id for item in result.updates]
    assert len(updated) == len(set(updated))
    assert result.valid == (not result.violations)
    if result.valid:
        assert len(accepted) == len(plan.workstreams)

    dropped = _entry_positions(result)
    kept = [entry for index, entry in enumerate(plan.workstreams) if index not in dropped]
    if kept:
        retry = check(portfolio(*kept, updates=_kept_updates(plan, result)), record, capacity)
        assert [item.work_id for item in retry.accepted] == accepted
        assert all(item.code is ViolationCode.OVER_CAPACITY for item in retry.violations)


def _kept_updates(plan: PortfolioPlan, result: PlanCheck) -> tuple[PlannerHypothesisUpdate, ...]:
    kept = {item.hypothesis_id for item in result.updates}
    seen: set[str] = set()
    chosen = []
    for update in plan.hypothesis_updates:
        if update.hypothesis_id in kept and update.hypothesis_id not in seen:
            seen.add(update.hypothesis_id)
            chosen.append(update)
    return tuple(chosen)


# -- combining retained candidates ---------------------------------------------------------------

COMBINE_ID = "k"
MAX_PARTS = 8
"""The most parts one combine may name."""
_PART_VALUES = st.none() | st.sampled_from([1.0, 5.0, 9.0])
"""Each part's own headline: often below main's, since a part that lost alone may be combined."""


@dataclass(frozen=True)
class CombineCase:
    """A combine entry as the planner wrote it, the record it is checked against, and the rule."""

    entry: dict[str, object]
    record: Record
    valid: bool
    """Whether the entry breaks no rule: 2 to 8 distinct offered exact parts, no unknown key, a
    new ID. Restated here independently of the search; how the parts measure never matters."""


@st.composite
def combine_cases(draw: st.DrawFn) -> CombineCase:
    """A combine of picks drawn from offered pairs, unoffered revisions, unknown hypotheses and
    repeats, sometimes with an unknown key, over a record whose parts beat main or not."""
    names = draw(st.lists(st.sampled_from("abcdef"), unique=True, max_size=5))
    parents = [
        snapshot(name, ordinal, value=draw(_PART_VALUES))
        for name in names
        for ordinal in range(1, draw(st.integers(1, 2)) + 1)
    ]
    offered = [(item.hypothesis_id, item.revision.revision_id.root) for item in parents]
    picks: list[tuple[str, str]] = []
    for _ in range(draw(st.integers(0, 10))):
        kind = draw(st.sampled_from(["offered", "offered", "unoffered", "unknown", "repeat"]))
        if kind == "offered" and offered:
            picks.append(draw(st.sampled_from(offered)))
        elif kind == "unoffered" and names:
            picks.append((draw(st.sampled_from(names)), "nope"))
        elif kind == "repeat" and picks:
            picks.append(draw(st.sampled_from(picks)))
        else:
            picks.append(("zz", "zz-r1"))
    identifier = draw(st.sampled_from([COMBINE_ID, COMBINE_ID, *names]))
    parts: list[dict[str, object]] = [
        {"hypothesis_id": name, "revision": revision} for name, revision in picks
    ]
    unknown = draw(st.sampled_from([None, None, "entry", "part"]))
    if unknown == "part" and parts:
        parts[draw(st.integers(0, len(parts) - 1))]["weight"] = 1
    entry: dict[str, object] = {
        "kind": "combine",
        "hypothesis_id": identifier,
        "title": "both halves",
        "hypothesis": "the halves were designed to work together",
        "parents": parts,
        "instructions": "apply in order",
        "watch_for": "both touch the scheduler",
        "pass_criteria": "faster than the best part",
    }
    if unknown == "entry":
        entry["weight"] = 1
    hypotheses = [hypothesis_record(name, HypothesisOutcome.INCONCLUSIVE) for name in names]
    stray = unknown == "entry" or (unknown == "part" and bool(parts))
    valid = (
        not stray
        and 2 <= len(picks) <= MAX_PARTS
        and all(pick in offered for pick in picks)
        and len({name for name, _ in picks}) == len(picks)
        and identifier not in names
    )
    main_value = draw(st.none() | st.sampled_from([1.0, 5.0, 9.0]))
    return CombineCase(
        entry, Record(hypotheses=hypotheses, parents=parents, main_value=main_value), valid
    )


def _parsed(reply: dict[str, object]) -> PortfolioPlan | list[tuple[str | int, ...]]:
    """The reply as the planning call parses it, or the paths of what the schema rejected."""
    try:
        return PortfolioPlan.model_validate(reply)
    except ValidationError as error:
        return [tuple(item["loc"]) for item in error.errors()]


def _rejected_at(paths: list[tuple[str | int, ...]]) -> bool:
    """Whether a schema rejection names the unknown key or the parts list it is about."""
    named = {"weight", "parents"}
    return all(path[:2] == ("workstreams", 0) for path in paths) and any(
        named & {str(step) for step in path} for path in paths
    )


@settings(max_examples=examples(pr=40, nightly=400), deadline=None, derandomize=True, database=None)
@given(case=combine_cases())
@pytest.mark.xfail(strict=True, reason="S182: the combine workstream kind does not exist yet")
def test_a_combine_is_scheduled_exactly_when_its_parts_are_distinct_offered_exact_revisions(
    case: CombineCase,
) -> None:
    """Parse and validate as the planning call does: accepted iff the restated rule holds.

    Main's measurement and each part's value vary, and never decide: a combine has no
    beats-main bar, since parts that lost or tied alone are what it is for.
    """
    reply = {
        "reasoning": "combine",
        "workstreams": [case.entry],
        "handoff": {"expectations": [], "doubts": []},
    }
    plan = _parsed(reply)
    if not isinstance(plan, PortfolioPlan):
        assert not case.valid, plan
        assert _rejected_at(plan), plan
        return
    result = check(plan, case.record)
    assert result.valid == case.valid, result.violations
    if case.valid:
        (accepted,) = result.accepted
        assert accepted.work_id == case.entry["hypothesis_id"]
        assert accepted.kind is WorkKind.IMPLEMENT  # a combine is an implement of a new hypothesis
        assert accepted.parent_hypothesis_id is None  # it starts from main
        parts = [(item.hypothesis_id, item.revision) for item in accepted.combine]
        expected = [(str(p["hypothesis_id"]), str(p["revision"])) for p in case.entry["parents"]]
        assert parts == expected
        return
    assert result.accepted == ()
    for item in result.violations:
        assert item.path[:2] == ("workstreams", 0), item
        assert len(item.path) >= 3, item  # names the field to fix, not just the entry
