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
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    ImplementPrompt,
    PlannerPrompt,
    ProfileHistoryRow,
    PromptTemplate,
)
from vs_core.api import Measure, Operation, Retained, RevisionRef, StartAttempt, Withdraw
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from tests.vibesys.orchestration.dynamic._host import World

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


def _implemented(host: Host, identifier: str, candidate: str) -> None:
    _start(host)
    implement_and_judge(host, identifier, candidate)
    _settle(host)


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
    shown = render(PromptTemplate.PORTFOLIO, after)
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
