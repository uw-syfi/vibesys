"""What the search does with the planner's replies: ask again, correct, schedule, remember.

A planner reply can be lost, can break the plan's schema, can break a rule of the run, or can be a
good plan with unusual text in it. These stories play the host of the dynamic flow for one planner
and read what the search decided: which request it asks next, what the correction names, which
workstream starts from which base, and what the planner is shown of the text it wrote. Every
story runs live and again with a restart after every step.
"""

from __future__ import annotations

import json
import unicodedata

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import implement, implemented, plan_reply
from tests.vibesys.orchestration.dynamic._slots import (
    implement_and_judge,
    open_next_slot,
    planner_context,
    turn_about,
    verify_parents,
)
from tests.vibesys.orchestration.dynamic.core_policy._contexts import render
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import (
    CUT,
    PlannerCorrection,
    PlannerPrompt,
    ProfileHistoryRow,
    PromptTemplate,
    ViolationCode,
)
from vs_core.api import (
    FailureReason,
    Finish,
    InvocationId,
    InvocationRef,
    RequestTurn,
    Retained,
    RevisionRef,
    SessionId,
    StartAttempt,
)
from vs_core.api.search import ReplyError, ReplyErrorCode
from vs_sim.api.testing import examples

MODES = across_restarts
LOST_PLANNER_TURNS = 3
"""How often one planning ask is made before the planner is given up on: first ask and two more."""


def start(
    *, restarting: bool, throughputs: dict[str, float] | None = None, **settings: object
) -> Host:
    """Run the input's measurement up to the first planner turn."""
    host = begin(config(**settings), restarting=restarting, throughputs=throughputs or {})
    host.drain_until_turn(PLANNER_LABEL)
    return host


def plan(host: Host, *entries: dict[str, object], reasoning: str = "portfolio") -> None:
    reply = plan_reply(*entries, reasoning=reasoning)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), reply)


def answer_all_but_the_planner(host: Host) -> None:
    """Answer the other waits the ordinary way, and move the run clock, until the planner is asked."""
    for _ in range(100):
        if host.open(RequestTurn, label=PLANNER_LABEL):
            return
        others = [item for item in host.run.open if item.until is not None]
        if others:
            host.answer(others[0])
        elif host.run.sleeping_until:
            host.run.advance_to(min(host.run.sleeping_until))
        else:
            return


def corrected(host: Host) -> PlannerCorrection:
    context = planner_context(host)
    assert isinstance(context, PlannerCorrection)
    return context


# -- a planner turn that is lost -----------------------------------------------------------------


@MODES
def test_a_lost_planner_turn_is_asked_again_with_the_same_request_and_no_complaint(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting)
    assert isinstance(planner_context(host), PlannerPrompt)
    # The first ask went out while the input was being measured; the asks after it show the
    # measurement, and each is the one before it again.
    host.run.lose(host.run.expect_turn(role=PLANNER_LABEL))
    answer_all_but_the_planner(host)
    first = planner_context(host)
    assert isinstance(first, PlannerCorrection)

    host.run.lose(host.run.expect_turn(role=PLANNER_LABEL))
    answer_all_but_the_planner(host)

    again = planner_context(host)
    assert isinstance(again, PlannerCorrection)
    assert again.problem is None  # nothing was wrong with a reply: there was none
    asked = {"template", "of", "problem"}
    assert again.model_dump(exclude=asked) == first.model_dump(exclude=asked)
    text = render(PromptTemplate.PORTFOLIO_CORRECTION, again)
    assert "Correction required" not in text
    assert "ended without a plan" not in text


@MODES
def test_a_planner_lost_every_time_it_is_asked_ends_the_run_without_a_stall(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting)

    for _ in range(LOST_PLANNER_TURNS):
        host.run.lose(host.run.expect_turn(role=PLANNER_LABEL))
        answer_all_but_the_planner(host)

    # No fourth ask: the run ended, with nothing started and nothing adopted.
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome != "success"
    assert finish.result.selection is None
    assert_flow_closed(host)
    assert not host.opened


# -- a reply that breaks the plan's schema -------------------------------------------------------


def invalid_reply(reason: str) -> dict[str, object]:
    """The failure of a planner turn whose reply broke its schema, as the validator reported it."""
    return {
        "turn": InvocationRef(
            session_id=SessionId(root="planner"),
            invocation_id=InvocationId(root="planner-1"),
            generation=0,
        ),
        "errors": (
            ReplyError(
                path=("workstreams", 0, "implement", "hypothesis_id"),
                code=ReplyErrorCode.INVALID_VALUE,
                message=reason,
            ),
        ),
    }


@MODES
def test_a_reply_that_breaks_the_plan_schema_is_corrected_with_the_validators_reason(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting)
    reason = "'0 ' is not a valid identifier: it has leading or trailing whitespace; use '0'"
    facts = invalid_reply(reason)

    host.run.fail(host.run.expect_turn(role=PLANNER_LABEL), FailureReason.INVALID_REPLY, **facts)

    correction = corrected(host)
    assert correction.problem is not None
    assert correction.problem.kind.value == "unparseable"
    assert correction.problem.errors == facts["errors"]
    assert correction.of == facts["turn"]
    plan(host, implement("0"))
    assert host.open(StartAttempt)


@MODES
def test_a_plan_whose_id_is_not_in_its_canonical_spelling_is_corrected_and_nothing_starts(
    *, restarting: bool
) -> None:
    # Past bug: the id "0 " was kept as written by the workstream and stripped by the search state,
    # so recording its round crashed the loop. The id is now refused where the plan enters.
    host = start(restarting=restarting)

    plan(host, implement("0 "))

    correction = corrected(host)
    assert correction.problem is not None
    assert correction.problem.kind.value == "unparseable"
    assert [
        error.path for error in correction.problem.errors if error.path[-1] == "hypothesis_id"
    ] == [("workstreams", 0, "implement", "hypothesis_id")]
    assert not host.open(StartAttempt)
    plan(host, implement("0"))
    open_next_slot(host)
    implement_and_judge(host, "0", "c1")
    host.drain()
    assert host.record is not None
    assert [item.hypothesis_id for item in host.record.hypotheses] == ["0"]
    assert_flow_closed(host)


@MODES
def test_a_planner_that_breaks_its_schema_every_time_ends_the_run_after_a_bounded_number_of_asks(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting)

    asks = 0
    while host.open(RequestTurn, label=PLANNER_LABEL):
        asks += 1
        assert asks <= 8, host.run.timeline()  # a guard against a search that never gives up
        reason = invalid_reply("must match pattern")
        host.run.fail(
            host.run.expect_turn(role=PLANNER_LABEL), FailureReason.INVALID_REPLY, **reason
        )
        answer_all_but_the_planner(host)

    assert 2 <= asks <= 4
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome != "success"
    assert host.record is not None
    assert host.record.hypotheses == ()
    assert not host.opened


# -- a reply that breaks a rule of the run -------------------------------------------------------


@MODES
def test_a_plan_that_continues_an_unknown_hypothesis_is_corrected_before_anything_starts(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting, max_in_flight=2)

    plan(host, implement("Q9", continue_hypothesis=True), implement("Q10"))

    correction = corrected(host)
    assert correction.problem is not None
    assert correction.problem.kind.value == "violations"
    (violation,) = correction.problem.violations
    assert violation.code is ViolationCode.HYPOTHESIS_UNKNOWN
    assert violation.subject == "Q9"
    assert violation.path == ("workstreams", 0, "hypothesis_id")
    # Not even the valid half is started while the planner is being asked to fix the other.
    assert not host.open(StartAttempt)
    plan(host, implement("Q10"), implement("Q11"))
    assert len(host.open(StartAttempt)) == 2


# -- where a workstream starts -------------------------------------------------------------------


@MODES
def test_a_workstream_that_names_no_parent_starts_from_the_input_never_from_the_best_candidate(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting, max_rounds=3, throughputs={"c1": 150.0, "c2": 200.0})
    bases = []
    for hypothesis, candidate, extra in (
        ("h1", "c1", {}),
        ("h2", "c2", {"parent_hypothesis_id": "h1"}),
        ("h3", "c3", {}),
    ):
        plan(host, implement(hypothesis, **extra))
        verify_parents(host)
        (asked,) = host.open(StartAttempt)
        assert isinstance(asked.request, StartAttempt)
        bases.append(asked.request.workspace.base)
        host.answer(asked)
        implement_and_judge(host, hypothesis, candidate)
        host.drain_until_turn(PLANNER_LABEL) if hypothesis != "h3" else host.drain()

    input_revision, from_h1, again = bases
    assert from_h1 != input_revision  # a named parent is built on
    assert from_h1.git_commit == "c1"
    # h2 is the best candidate by now (200 against 150 for h1), and h3 still starts from the input.
    assert again == input_revision


# -- text the agents wrote -----------------------------------------------------------------------


@MODES
def test_long_agent_text_is_kept_whole_in_the_record_yet_the_planner_is_shown_it_bounded(
    *, restarting: bool
) -> None:
    long = "word " * 4000
    host = start(restarting=restarting, max_rounds=2)
    plan(
        host,
        implement("h1", title=long, hypothesis=long, task=long, pass_criteria=long),
        reasoning=long,
    )
    open_next_slot(host)
    result = ImplementerResult.model_validate_json(implemented("supported", long, next_step=long))
    host.run.reply(
        turn_about(host, "implementer", "h1"),
        result,
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    verdict = ReviewResult.model_validate_json(
        json.dumps({"passed": True, "analysis": long, "feedback": long})
    )
    host.run.reply(turn_about(host, "judge", "h1"), verdict)
    host.drain_until_turn(PLANNER_LABEL)

    # No agent text field is capped, so nothing is cut at the agent boundary: the record keeps it.
    assert host.record is not None
    (done,) = host.record.finished
    for field in ("title", "hypothesis", "task", "pass_criteria"):
        assert getattr(done.plan, field) == long
    (hypothesis,) = host.record.hypotheses
    assert hypothesis.rounds[0].summary == long
    # The planner is shown a bounded view, and says where it was cut.
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    (row,) = context.history
    assert not isinstance(row, ProfileHistoryRow)
    for shown in (row.title, row.summary):
        assert 0 < len(shown) < len(long)
        assert shown.endswith(CUT)
    prompt = render(PromptTemplate.PORTFOLIO, context)
    assert len(prompt) < 20_000
    assert long not in prompt


# -- any id and title the planner can write ------------------------------------------------------

_IDS = st.text(
    st.characters(categories=("L", "M", "N", "P", "S"), include_characters=" "),
    min_size=1,
    max_size=128,
).filter(lambda value: value == value.strip() and unicodedata.is_normalized("NFC", value))
_TITLES = st.text(st.characters(exclude_categories=("Cs",)), min_size=1, max_size=80).filter(
    str.strip
)


@MODES
@settings(max_examples=examples(pr=10, nightly=25), deadline=None, derandomize=True, database=None)
@given(identifier=_IDS, title=_TITLES)
@example(identifier="0", title="T" * 80)
@example(identifier="KV.Cache_v2 / ../Ünïcode", title="title")
def test_any_planned_id_and_title_reach_a_trusted_round_a_parent_lookup_and_the_next_prompt(
    *, restarting: bool, identifier: str, title: str
) -> None:
    # Ids and titles are agent output that becomes workspace names, Git refs, state namespaces,
    # core identities and prompt text. Past failures: the id "0 " (stored stripped, so the round
    # matched no hypothesis), an 80 character title, a path-like Unicode id.
    host = start(restarting=restarting, max_rounds=2)
    plan(host, implement(identifier, title=title))
    open_next_slot(host)
    implement_and_judge(host, identifier, "c1")
    host.drain_until_turn(PLANNER_LABEL)

    assert host.record is not None
    (hypothesis,) = host.record.hypotheses
    assert hypothesis.hypothesis_id == identifier
    assert [item.eligible for item in hypothesis.rounds] == [True]
    context = planner_context(host)
    assert isinstance(context, PlannerPrompt)
    assert [option.hypothesis_id for option in context.buildable] == [identifier]
    assert identifier in render(PromptTemplate.PORTFOLIO, context)
    # The id names its candidate as a parent, exactly.
    plan(host, implement("child", parent_hypothesis_id=identifier))
    verify_parents(host)
    (asked,) = host.open(StartAttempt)
    assert isinstance(asked.request, StartAttempt)
    assert asked.request.workspace.base.git_commit == "c1"
