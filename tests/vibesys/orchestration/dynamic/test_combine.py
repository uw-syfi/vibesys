"""A combine builds one candidate from retained candidates that may each have lost alone.

The planner names two or more exact retained revisions; the search schedules an implement
workstream of a new hypothesis that starts from main, and its implementer is told each part's own
change (the diff from the revision the part was built on to the part). The composite is judged
like any candidate, and the planner's history row shows its trusted result beside each part's,
with a verdict against the best part. These stories play the search under a fake host, live and
with restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Unpack

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    INPUT_THROUGHPUT,
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    combine,
    implement,
    implemented,
    merge,
    merged,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic._slots import (
    implement_and_judge,
    open_next_slot,
    planner_context,
    turn_about,
    verify_parents,
)
from tests.vibesys.orchestration.dynamic._values import run_record, snapshot
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.candidates.api import measurement
from vibesys.dynamic_handoff import render_handoff
from vibesys.hypothesis import HypothesisStrategy
from vibesys.metrics import MetricRow, MetricSpace, Objective
from vibesys.orchestration.dynamic.models import ImplementerResult, MergeResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    CombineSource,
    ImplementPrompt,
    MergePrompt,
    PartsVerdict,
    PlannerPrompt,
    ProfileHistoryRow,
    PromptTemplate,
    RoundRecord,
    WorkKind,
    WorkPlan,
    best_part,
    implement_prompt,
    versus_parts,
)
from vs_core.api import (
    Measure,
    Operation,
    RequestTurn,
    Retained,
    RevisionRef,
    StartAttempt,
    Withdraw,
)
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    import random

    from tests.vibesys.orchestration.dynamic._host import World

    from vibesys.metrics import Direction
    from vibesys.orchestration.dynamic.search.api import ParentSnapshot

MODES = across_restarts


def _start(host: Host) -> None:
    """Verify the parent when it is not the input, then admit the one workstream waiting."""
    for _ in range(3):
        if host.open(StartAttempt):
            break
        verify_parents(host)
    assert host.open(StartAttempt), host.run.timeline()
    open_next_slot(host)


def _settle(host: Host) -> None:
    """Answer the measurement, the reading of its evidence and the close, as the world would."""
    for _ in range(10):
        pending = [*host.open(Measure), *host.open(Operation), *host.open(Withdraw)]
        if not pending:
            return
        host.answer(pending[0])


def _plan(host: Host, *entries: dict[str, object]) -> PlannerPrompt:
    host.drain_until_turn(PLANNER_LABEL)
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt), context
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(*entries))
    return context


def _implemented(
    host: Host, identifier: str, candidate: str, outcome: str = "supported"
) -> ImplementPrompt:
    """Play one implement workstream through its settlement; what its implementer was asked."""
    _start(host)
    asked = turn_about(host, "implementer", identifier).context
    assert isinstance(asked, ImplementPrompt)
    implement_and_judge(host, identifier, candidate, outcome=outcome)
    _settle(host)
    return asked


def _merged(
    host: Host, merge_id: str, hypothesis: str | tuple[str, ...], commit: str
) -> MergePrompt:
    """Plan a merge of ``hypothesis`` (one or several) and have its merger commit ``commit``.

    What the merger was asked. The commit may hold edits of the merger's own (a fix-up): the
    record cannot tell, so no diff may take a merge commit as a tree's starting point.
    """
    names = (hypothesis,) if isinstance(hypothesis, str) else hypothesis
    _plan(host, merge(merge_id, *names))
    _start(host)
    (asked,) = host.open(RequestTurn, label="merger")
    context = asked.context
    assert isinstance(context, MergePrompt)
    checkpoint = Retained(revision=RevisionRef.of_git_commit(commit))
    host.run.reply(asked, MergeResult.model_validate_json(merged()), checkpoint=checkpoint)
    _settle(host)
    return context


def _flat(text: str) -> str:
    return " ".join(text.split())


def _row(context: PlannerPrompt, identifier: str) -> object:
    """The history row of hypothesis ``identifier``."""
    (row,) = (
        item
        for item in context.history
        if not isinstance(item, ProfileHistoryRow) and item.hypothesis_id == identifier
    )
    return row


def _two_parts_that_lost(*, restarting: bool, **world: Unpack[World]) -> tuple[Host, PlannerPrompt]:
    """A run whose ``h1`` (``c1``) and ``h2`` (``c2``) each measured below the input."""
    host = begin(config(max_rounds=6), restarting=restarting, **world)
    _plan(host, implement("h1"))
    _implemented(host, "h1", "c1")
    _plan(host, implement("h2"))
    _implemented(host, "h2", "c2")
    host.drain_until_turn(PLANNER_LABEL)
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    return host, context


LOST = {"c1": INPUT_THROUGHPUT - 10, "c2": INPUT_THROUGHPUT - 5}


@MODES
def test_a_combine_of_two_candidates_that_lost_alone_is_built_from_main_and_reported_by_part(
    *, restarting: bool
) -> None:
    host, offer = _two_parts_that_lost(restarting=restarting, throughputs={**LOST, "c3": 300.0})
    # Neither part beats main, so neither may be merged; both are offered to combine.
    assert offer.mergeable == ()
    revisions = {row.hypothesis_id: row.revision for row in offer.buildable}
    assert set(revisions) == {"h1", "h2"}
    pair = [(name, revisions[name].revision_id.root) for name in ("h1", "h2")]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(combine("k1", *pair)))
    # The next planner reads the parts in this call's handoff file.
    written = max(host.handoffs.values(), key=lambda item: item.call)
    handoff = _flat(render_handoff(written))
    parts = ", ".join(f"`{name}` at `{revision}`" for name, revision in pair)
    assert f'combine `k1` "title k1" of {parts}, applied in that order onto main' in handoff

    _start(host)
    asked = turn_about(host, "implementer", "k1").context
    assert isinstance(asked, ImplementPrompt)
    assert asked.parent_revision == offer.main_revision  # a combine starts from main
    assert [(part.hypothesis_id, part.revision) for part in asked.combine] == [
        ("h1", revisions["h1"]),
        ("h2", revisions["h2"]),
    ]
    # Each part's own change: both were built on the input.
    assert {part.built_on for part in asked.combine} == {offer.base_revision}
    text = render(PromptTemplate.IMPLEMENT, asked)
    for part in asked.combine:
        assert f"git diff {part.built_on.revision_id.root} {part.revision.revision_id.root}" in text
    implement_and_judge(host, "k1", "c3")
    _settle(host)

    after = _plan(host, implement("h3"))
    row = _row(after, "k1")
    composite = getattr(row, "composite", None)
    assert composite is not None
    parts = composite.parts
    assert [part.hypothesis_id for part in parts] == ["h1", "h2"]
    assert [part.metric.value for part in parts if part.metric] == [LOST["c1"], LOST["c2"]]
    assert composite.verdict.value == "better"
    assert composite.best_part == "h2"  # the stronger part alone
    shown = render(PromptTemplate.PORTFOLIO, after)
    # The task declares no noise: the verdict is a strict comparison and must not claim noise.
    assert "better in a strict comparison" in _flat(shown), shown
    assert "beyond" not in _flat(shown).split("combined from `h1`")[1].split(".")[0]
    assert "combined from `h1`" in shown, shown
    assert "`h2`" in shown
    # The composite beats main, so the existing merge path may bring it into main.
    assert [item.hypothesis_id for item in after.mergeable] == ["k1"]


@MODES
def test_what_the_implementer_could_not_apply_reaches_the_planner(*, restarting: bool) -> None:
    host, offer = _two_parts_that_lost(restarting=restarting, throughputs=LOST)
    revisions = {row.hypothesis_id: row.revision.revision_id.root for row in offer.buildable}
    pair = [(name, revisions[name]) for name in ("h1", "h2")]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(combine("k1", *pair)))
    _start(host)
    dropped = {"hypothesis_id": "h2", "part": "the prefix-cache hunk", "why": "conflicts with h1"}
    reply = ImplementerResult.model_validate_json(implemented(unapplied=[dropped]))
    checkpoint = Retained(revision=RevisionRef.of_git_commit("c3"))
    host.run.reply(turn_about(host, "implementer", "k1"), reply, checkpoint=checkpoint)
    verdict = ReviewResult.model_validate_json(reviewed())
    host.run.reply(turn_about(host, "judge", "k1"), verdict)
    _settle(host)

    after = _plan(host, implement("h3"))
    composite = getattr(_row(after, "k1"), "composite", None)
    assert composite is not None
    assert [item.model_dump() for item in composite.unapplied] == [dropped]
    assert "the prefix-cache hunk" in render(PromptTemplate.PORTFOLIO, after)


def _combine_of(*parts: ParentSnapshot) -> WorkPlan:
    """An accepted combine ``k1`` of ``parts``, as plan validation leaves it."""
    picks = [
        {"hypothesis_id": item.hypothesis_id, "revision": item.revision.revision_id.root}
        for item in parts
    ]
    fields = {"title": "title k1", "hypothesis": "the parts work together", "task": "apply"}
    return WorkPlan.model_validate(
        {"kind": WorkKind.IMPLEMENT, "work_id": "k1", **fields, "combine": tuple(picks)}
    )


# No flow story reaches these two: a combine starts the moment it is accepted, and its parts are
# not parent-checked, so a part leaves the offer between planning and start only through a check
# that withholds its revision in that window. The record is driven directly instead.


def test_a_part_withdrawn_before_the_combine_starts_is_named_to_the_implementer() -> None:
    a, b, c = (snapshot(name, 1, value=40.0) for name in ("a", "b", "c"))
    run = run_record(parents=(a, b, c), withheld=(b.revision.revision_id.root,))
    run.accepted = [_combine_of(a, b, c)]
    (start,) = run.ready_to_start(1)
    assert [part.hypothesis_id for part in start.combine] == ["a", "c"]
    asked = implement_prompt(start, (), baseline=run.baseline)
    assert [pick.hypothesis_id for pick in asked.combine_missing] == ["b"]
    told = _flat(render(PromptTemplate.IMPLEMENT, asked))
    assert "hypothesis `b` at `b-r1` is no longer buildable: leave it out" in told, told


def test_a_combine_left_with_one_part_never_starts_and_the_planner_hears_why() -> None:
    a, b = (snapshot(name, 1, value=40.0) for name in ("a", "b"))
    run = run_record(parents=(a, b), withheld=(b.revision.revision_id.root,))
    run.accepted = [_combine_of(a, b)]
    assert run.ready_to_start(1) == []
    assert not run.running
    assert run.free_agents() == run.config.max_in_flight  # its slot was not spent
    assert run.hypotheses["k1"].strategy is HypothesisStrategy.ABANDONED  # never continued
    asked = run.world().prompt()
    assert isinstance(asked, PlannerPrompt)
    composite = getattr(_row(asked, "k1"), "composite", None)
    assert composite is not None
    assert composite.cancelled
    assert [part.hypothesis_id for part in composite.parts] == ["a"]
    shown = _flat(render(PromptTemplate.PORTFOLIO, asked))
    assert "This combine never started" in shown, shown
    assert "only `a` at `a-r1` of its parts was still buildable, and a combine needs two" in shown


_TEXT = st.text(st.characters(codec="utf-8", exclude_categories=("Cs",)), min_size=1, max_size=40)
UNAPPLIED = st.lists(
    st.fixed_dictionaries(
        {"hypothesis_id": st.sampled_from(["h1", "h2", "part-b"]), "part": _TEXT, "why": _TEXT}
    ),
    max_size=3,
)


@settings(max_examples=examples(pr=15, nightly=150), deadline=None)
@given(unapplied=UNAPPLIED, stray=st.booleans())
def test_the_unapplied_reply_field_round_trips_and_rejects_unknown_keys(
    unapplied: list[dict[str, str]], *, stray: bool
) -> None:
    sent = implemented(unapplied=unapplied)
    reply = ImplementerResult.model_validate_json(sent)
    again = ImplementerResult.model_validate_json(reply.model_dump_json())
    assert again == reply
    assert [item.model_dump() for item in again.unapplied] == unapplied
    if stray and unapplied:
        bad = [{**unapplied[0], "severity": "high"}, *unapplied[1:]]
        with pytest.raises(ValueError, match="severity"):
            ImplementerResult.model_validate_json(implemented(unapplied=bad))


# Main moves twice under a hypothesis that goes on: m1 merges h1 (main c3), m2 merges h3 (main c6).
# Under read-only git, catching up applies main's change to the tree without recording main in the
# git history, so only the run's record knows which main each candidate's tree holds.
WALK = {"c2": 40.0, "c4": 40.0, "c5": 200.0, "c6": 200.0, "c7": 300.0, "c8": 40.0}


@MODES
def test_a_part_that_caught_up_with_main_twice_is_combined_and_merged_with_its_own_change_only(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=12), restarting=restarting, throughputs=WALK)
    first = _plan(host, implement("h1"))
    base = first.base_revision.revision_id.root
    _implemented(host, "h1", "c1")
    _plan(host, implement("h2"))
    _implemented(host, "h2", "c2", "inconclusive")
    _merged(host, "m1", "h1", "c3")
    _plan(host, implement("h2", continue_hypothesis=True))
    caught_up = _flat(
        render(PromptTemplate.IMPLEMENT, _implemented(host, "h2", "c4", "inconclusive"))
    )
    assert f"git diff {base} c3" in caught_up
    _plan(host, implement("h3"))
    _implemented(host, "h3", "c5")
    _merged(host, "m2", "h3", "c6")
    _plan(host, implement("h2", continue_hypothesis=True))
    again = _flat(render(PromptTemplate.IMPLEMENT, _implemented(host, "h2", "c7", "inconclusive")))
    # The second catch-up applies only m2's change: m1's is in the tree already.
    assert "git diff c3 c6" in again, again
    assert "merge-base" not in again
    _plan(host, implement("h4"))
    _implemented(host, "h4", "c8")

    _plan(host, combine("k1", ("h2", "c7"), ("h4", "c8")))
    _start(host)
    asked = turn_about(host, "implementer", "k1").context
    assert isinstance(asked, ImplementPrompt)
    told = _flat(render(PromptTemplate.IMPLEMENT, asked))
    # Each part's own change: h2's tree holds main c6, and h4 started from it.
    assert "git diff c6 c7" in told, told
    assert "git diff c6 c8" in told, told
    implement_and_judge(host, "k1", "c9")
    _settle(host)

    merging = _flat(render(PromptTemplate.MERGE, _merged(host, "m3", "h2", "c10")))
    assert "git diff c6 c7" in merging, merging
    assert "merge-base" not in merging


def _part(name: str, value: float, direction: Direction = "max") -> CombineSource:
    row = MetricRow(name="throughput", value=value, direction=direction, unit="tokens/s")
    at = RevisionRef.of_git_commit
    return CombineSource(hypothesis_id=name, revision=at(name), built_on=at("base"), metric=row)


def _space(noise: float, direction: Direction = "max") -> MetricSpace:
    return MetricSpace(objectives=(Objective("throughput", direction),), relative_noise=noise)


def test_the_composite_is_compared_with_the_strongest_part_not_the_first_within_noise() -> None:
    # B (102) is within 3% of A (100); the composite (104) is 4% over A but only 2% over B.
    parts = (_part("a", 100.0), _part("b", 102.0))
    composite = (MetricRow(name="throughput", value=104.0, direction="max"),)
    space = _space(0.03)
    best = best_part(parts, space)
    assert best is not None
    assert best.hypothesis_id == "b"
    assert versus_parts(composite, parts, space) is PartsVerdict.WITHIN_NOISE
    assert versus_parts(composite, parts, _space(0.0)) is PartsVerdict.BETTER


def test_a_composite_on_another_metric_than_its_parts_is_not_compared() -> None:
    parts = (_part("a", 100.0), _part("b", 102.0))
    composite = (MetricRow(name="latency", value=1.0, direction="min"),)
    assert versus_parts(composite, parts, _space(0.03)) is PartsVerdict.UNKNOWN


_VALUES = st.integers(1, 1000).map(float)


@settings(max_examples=examples(pr=50, nightly=500), deadline=None)
@given(
    values=st.lists(_VALUES, min_size=2, max_size=5),
    composite=_VALUES,
    noise=st.sampled_from((0.0, 0.01, 0.03, 0.1)),
    direction=st.sampled_from(("max", "min")),
    order=st.randoms(use_true_random=False),
)
def test_the_parts_verdict_is_the_composite_against_the_raw_best_part_in_any_order(
    values: list[float], composite: float, noise: float, direction: Direction, order: random.Random
) -> None:
    parts = tuple(_part(f"p{index}", value, direction) for index, value in enumerate(values))
    space = _space(noise, direction)
    strongest = max(values) if direction == "max" else min(values)
    row = MetricRow(name="throughput", value=composite, direction=direction)
    reference = space.compare(
        measurement(row), measurement(_part("ref", strongest, direction).metric)
    )
    got = versus_parts((row,), parts, space)
    assert got.value == reference.value
    shuffled = list(parts)
    order.shuffle(shuffled)
    assert versus_parts((row,), tuple(shuffled), space) is got


STRAY = {"hypothesis_id": "the scheduler part", "part": "all of it", "why": "a typo"}


def _reply(
    host: Host, hypothesis: str, candidate: str, unapplied: list[dict[str, str]] | None = None
) -> None:
    """The implementer of ``hypothesis`` answers with ``candidate``, listing ``unapplied``."""
    reply = ImplementerResult.model_validate_json(implemented(unapplied=unapplied or []))
    checkpoint = Retained(revision=RevisionRef.of_git_commit(candidate))
    host.run.reply(turn_about(host, "implementer", hypothesis), reply, checkpoint=checkpoint)


def _last_round(host: Host, hypothesis: str) -> RoundRecord:
    """The latest round of ``hypothesis`` in the record the search published."""
    record = host.record
    assert record is not None
    (found,) = (item for item in record.hypotheses if item.hypothesis_id == hypothesis)
    return found.rounds[-1]


def _judged(host: Host, hypothesis: str, *, passed: bool = True) -> None:
    verdict = ReviewResult.model_validate_json(reviewed(passed=passed))
    host.run.reply(turn_about(host, "judge", hypothesis), verdict)


@MODES
def test_a_stray_unapplied_entry_on_the_last_turn_still_has_the_composite_measured(
    *, restarting: bool
) -> None:
    host, offer = _two_parts_that_lost(restarting=restarting, throughputs={**LOST, "c4": 300.0})
    revisions = {row.hypothesis_id: row.revision.revision_id.root for row in offer.buildable}
    pair = [(name, revisions[name]) for name in ("h1", "h2")]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(combine("k1", *pair)))
    _start(host)
    _reply(host, "k1", "c3")
    _judged(host, "k1", passed=False)  # the first of the two turns ends without a measurement
    kept = {"hypothesis_id": "h2", "part": "the cache hunk", "why": "conflicts with h1"}
    _reply(host, "k1", "c4", unapplied=[kept, STRAY])
    _judged(host, "k1")
    _settle(host)

    after = _plan(host, implement("h3"))
    last = _last_round(host, "k1")
    assert last.candidate == RevisionRef.of_git_commit("c4")
    assert [row.value for row in last.metrics] == [300.0]
    composite = getattr(_row(after, "k1"), "composite", None)
    assert composite is not None
    assert composite.verdict.value == "better"
    # The entry naming no part is kept as the implementer wrote it, not reported against a part.
    assert [item.model_dump() for item in composite.unapplied] == [kept]
    assert [item.model_dump() for item in last.unattributed] == [STRAY]


@MODES
def test_an_unapplied_entry_outside_a_combine_is_kept_and_the_candidate_measured(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=4), restarting=restarting, throughputs={"c1": 200.0})
    _plan(host, implement("h1"))
    _start(host)
    _reply(host, "h1", "c1", unapplied=[STRAY])
    _judged(host, "h1")
    _settle(host)

    _plan(host, implement("h2"))
    last = _last_round(host, "h1")
    assert [row.value for row in last.metrics] == [200.0]
    assert last.unapplied == ()
    assert [item.model_dump() for item in last.unattributed] == [STRAY]


# The stories below play main's merges under read-only agent git, where a diff is applied with
# `git apply --reject`. A merger may edit the tree beyond applying its sources (a fix-up F that
# its prompt asks for), and the record cannot see it. So a tree's own change never starts at a
# merge commit: diffing a merge commit against a tree that lacks F removes F cleanly. It starts
# at the latest merged candidate the tree was built on; main holds that candidate, F and every
# other change a merge brought in.
FIXUP = {"c1": 200.0, "c2": 250.0, "c3": 40.0, "c4": 200.0, "c5": 250.0, "c6": 300.0, "c7": 300.0}


@MODES
def test_a_merger_fix_up_survives_the_merges_and_the_catch_up_of_children_of_what_it_merged(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=14), restarting=restarting, throughputs=FIXUP)
    first = _plan(host, implement("h1"))
    base = first.base_revision.revision_id.root
    _implemented(host, "h1", "c1")
    on_c1 = {"parent_hypothesis_id": "h1", "parent_revision": "c1"}
    _plan(host, implement("h3", **on_c1))
    _implemented(host, "h3", "c2")
    _plan(host, implement("h4", **on_c1))
    _implemented(host, "h4", "c3", "inconclusive")
    _merged(host, "m1", "h1", "c4")  # c4: the input, h1's change and the merger's fix-up F

    # h3's own change starts at c1, which main c4 holds: F stays, and h1 is not applied again.
    merging = _flat(
        render(PromptTemplate.MERGE, _merged(host, "m2", "h3", "c5"), agent_git_writable=False)
    )
    assert "`git diff c1 c2 | git apply --reject`" in merging, merging
    assert "git diff c4 " not in merging
    assert f"git diff {base} " not in merging

    # h4 goes on: it catches up with everything main gained since c1, F and h3's change included.
    _plan(host, implement("h4", continue_hypothesis=True))
    asked = _implemented(host, "h4", "c6")
    assert asked.rebase_onto == RevisionRef.of_git_commit("c5")
    caught_up = _flat(render(PromptTemplate.IMPLEMENT, asked, agent_git_writable=False))
    assert "`git diff c1 c5 | git apply --reject`" in caught_up, caught_up

    # Its tree now holds main c5, so its own change starts there.
    final = _flat(
        render(PromptTemplate.MERGE, _merged(host, "m3", "h4", "c7"), agent_git_writable=False)
    )
    assert "`git diff c5 c6 | git apply --reject`" in final, final


PARTLY = {"c1": 200.0, "c2": 210.0, "c3": 40.0, "c4": 260.0, "c5": 40.0}


@MODES
def test_a_child_of_one_source_of_a_merge_catches_up_with_the_others_from_its_parent(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=12), restarting=restarting, throughputs=PARTLY)
    first = _plan(host, implement("h1"))
    base = first.base_revision.revision_id.root
    _implemented(host, "h1", "c1")
    _plan(host, implement("h2"))
    _implemented(host, "h2", "c2")
    _plan(host, implement("h3", parent_hypothesis_id="h1", parent_revision="c1"))
    _implemented(host, "h3", "c3", "inconclusive")
    _merged(host, "m1", ("h1", "h2"), "c4")

    # h3's tree holds c1 but not c2: its catch-up starts at c1 and so carries h2's change and
    # whatever the merger added, never h1's again (from the input) and never nothing (from c4).
    _plan(host, implement("h3", continue_hypothesis=True))
    asked = _implemented(host, "h3", "c5", "inconclusive")
    caught_up = _flat(render(PromptTemplate.IMPLEMENT, asked, agent_git_writable=False))
    assert "`git diff c1 c4 | git apply --reject`" in caught_up, caught_up
    assert f"git diff {base} " not in caught_up


CHAIN = {"c1": 200.0, "c2": 210.0, "c3": 40.0, "c4": 260.0, "c5": 40.0}


@MODES
def test_a_child_of_the_later_of_two_merged_sources_on_one_line_starts_at_the_later(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=12), restarting=restarting, throughputs=CHAIN)
    _plan(host, implement("h1"))
    _implemented(host, "h1", "c1")
    _plan(host, implement("h2", parent_hypothesis_id="h1", parent_revision="c1"))
    _implemented(host, "h2", "c2")
    _plan(host, implement("h3", parent_hypothesis_id="h2", parent_revision="c2"))
    _implemented(host, "h3", "c3", "inconclusive")
    _merged(host, "m1", ("h1", "h2"), "c4")

    # c2 holds c1: starting at c1 would apply h2's change again.
    _plan(host, implement("h3", continue_hypothesis=True))
    asked = _implemented(host, "h3", "c5", "inconclusive")
    caught_up = _flat(render(PromptTemplate.IMPLEMENT, asked, agent_git_writable=False))
    assert "`git diff c2 c4 | git apply --reject`" in caught_up, caught_up


LATE = {"c1": 300.0, "c2": 250.0, "c3": 40.0, "c4": 250.0, "c5": 300.0, "c6": 40.0}


@MODES
def test_merging_an_ancestor_after_its_descendant_does_not_move_a_childs_start_back(
    *, restarting: bool
) -> None:
    host = begin(config(max_rounds=14), restarting=restarting, throughputs=LATE)
    _plan(host, implement("h1"))
    _implemented(host, "h1", "c1")
    _plan(host, implement("h2", parent_hypothesis_id="h1", parent_revision="c1"))
    _implemented(host, "h2", "c2")
    _plan(host, implement("h3", parent_hypothesis_id="h2", parent_revision="c2"))
    _implemented(host, "h3", "c3", "inconclusive")
    _merged(host, "m1", "h2", "c4")  # main holds c2, so c1 too
    _merged(host, "m2", "h1", "c5")  # c1's own change starts at the input, not at c2

    # h3's start stays c2: c1 holds less, and starting there would apply h2's change again.
    _plan(host, implement("h3", continue_hypothesis=True))
    asked = _implemented(host, "h3", "c6", "inconclusive")
    caught_up = _flat(render(PromptTemplate.IMPLEMENT, asked, agent_git_writable=False))
    assert "`git diff c2 c5 | git apply --reject`" in caught_up, caught_up


ON_K1 = {"c1": 150.0, "c2": 120.0, "c3": 300.0, "c4": 400.0, "c5": 150.0, "c6": 400.0}


@MODES
@pytest.mark.parametrize(
    "dropped",
    [
        pytest.param(False, id="applied"),
        pytest.param(True, id="dropped"),
    ],
)
def test_a_child_of_a_combine_whose_part_was_merged_starts_at_that_part(
    *, restarting: bool, dropped: bool
) -> None:
    host = begin(config(max_rounds=14), restarting=restarting, throughputs=ON_K1)
    first = _plan(host, implement("h1"))
    base = first.base_revision.revision_id.root
    _implemented(host, "h1", "c1")
    _plan(host, implement("h2"))
    _implemented(host, "h2", "c2")
    _plan(host, combine("k1", ("h1", "c1"), ("h2", "c2")))
    _start(host)
    left_out = {"hypothesis_id": "h1", "part": "all of it", "why": "conflicts with h2"}
    _reply(host, "k1", "c3", unapplied=[left_out] if dropped else None)
    _judged(host, "k1")
    _settle(host)
    _plan(host, implement("h5", parent_hypothesis_id="k1", parent_revision="c3"))
    _implemented(host, "h5", "c4")
    _merged(host, "m1", "h1", "c5")

    merging = _flat(
        render(PromptTemplate.MERGE, _merged(host, "m2", "h5", "c6"), agent_git_writable=False)
    )
    # The composite applied c1's change onto the input, so h5's tree holds c1, which main holds.
    # A part the implementer left out is not in the tree: starting at it would revert it.
    start = base if dropped else "c1"
    assert f"`git diff {start} c4 | git apply --reject`" in merging, merging


@MODES
def test_an_unapplied_entry_naming_no_part_is_shown_on_the_composite_row(
    *, restarting: bool
) -> None:
    host, offer = _two_parts_that_lost(restarting=restarting, throughputs=LOST)
    revisions = {row.hypothesis_id: row.revision.revision_id.root for row in offer.buildable}
    pair = [(name, revisions[name]) for name in ("h1", "h2")]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(combine("k1", *pair)))
    _start(host)
    _reply(host, "k1", "c3", unapplied=[STRAY])
    _judged(host, "k1")
    _settle(host)

    shown = _flat(render(PromptTemplate.PORTFOLIO, _plan(host, implement("h3"))))
    assert "Not attributed to a part: `the scheduler part`: all of it (a typo)" in shown, shown
