"""What the two serial searches share, told as stories against each of them.

Single and multi design a hypothesis the same way and play a round in the same workspace, so what
the shared steps promise is stated once and run against both: the designer's correction, the
attribution that steers it, the base each round starts on, the paid turns a round may spend, and
what a workspace is closed with. Every story runs live and again with a restart after every step.
"""

from __future__ import annotations

from typing import Unpack

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi import _host as multi_host
from tests.vibesys.orchestration.single import _host as single_host
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.serial_search._host import FACTS, SerialHost, World
from tests.vibesys.serial_search._replies import attribution, plan_reply
from tests.vibesys.vibesys_flows import across_restarts

from vs_core.api import (
    CaptureCommandBase,
    Finish,
    Operation,
    RequestTurn,
    Retained,
    RevisionRef,
    Settle,
    StartAttempt,
    Wait,
    Withdraw,
    WorkspaceMode,
)
from vs_sim.api.testing import examples

MODES = across_restarts
SEARCHES = pytest.mark.parametrize("search", ["single", "multi"])
PLANNER = "orchestrator"
IMPLEMENTER = "implementer"
PROFILE_GUIDED = {"command": ("profile",), "timeout_seconds": 10}


def start(
    search: str,
    *,
    restarting: bool,
    config: dict[str, object] | None = None,
    **world: Unpack[World],
) -> SerialHost:
    """Run the input's measurement up to the first design turn."""
    settings_ = {"max_rounds": 1, **(config or {})}
    if search == "single":
        host: SerialHost = single_host.SingleHost.start(
            single_host.SingleConfig.model_validate(settings_), restarting=restarting, **world
        )
    else:
        host = multi_host.MultiHost.start(
            multi_host.MultiConfig.model_validate(settings_), restarting=restarting, **world
        )
    return host


def design(host: SerialHost, *plans: str) -> None:
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply(*plans))


def implement(host: SerialHost, search: str, candidate: str, *, passed: bool = True) -> None:
    """The implementer answers with a retained ``candidate``; in multi the judge then rules."""
    host.drain_until_turn(IMPLEMENTER)
    asked = host.run.expect_turn(role=IMPLEMENTER)
    checkpoint = Retained(revision=RevisionRef.of_git_commit(candidate))
    if search == "single":
        host.run.reply(asked, single_host.implemented(passed=passed), checkpoint=checkpoint)
        return
    host.run.reply(asked, multi_host.implemented(), checkpoint=checkpoint)
    host.drain_until_turn("judge")
    host.run.reply(host.run.expect_turn(role="judge"), multi_host.reviewed(passed=passed))


def paid_turns(host: SerialHost, role: str) -> int:
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.charge_class == "paid"
        and role in item.request.turn.session.role_id.root
        for item in host.run.decisions
    )


def settlements(host: SerialHost) -> list[Settle]:
    return [
        item.request.disposition
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Withdraw)
        and isinstance(item.request.disposition, Settle)
    ]


def bases(host: SerialHost) -> list[RevisionRef]:
    return [
        item.request.workspace.base
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, StartAttempt)
    ]


# -- the designer ----------------------------------------------------------------------------


@SEARCHES
@MODES
def test_a_designer_that_never_gives_a_valid_plan_ends_the_run_without_a_round(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    for _ in range(2):  # the correction the ledger allows, after the first answer
        host.drain_until_turn(PLANNER)
        host.run.reply_raw(host.run.expect_turn(role=PLANNER), "not json")
    host.drain(limit=100)

    assert not host.opened
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "failure"
    assert "designer" in finish.result.reason


@SEARCHES
@MODES
def test_a_plan_the_ledger_refuses_is_corrected_once_by_a_turn_that_names_the_problem(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h1"))  # a reused id
    correction = host.run.expect_turn(role=PLANNER)

    assert getattr(correction.context, "problem", None) is not None
    host.run.reply_raw(correction, plan_reply("h2"))
    host.drain_until_turn(IMPLEMENTER)


# -- the attribution -------------------------------------------------------------------------


@SEARCHES
@MODES
def test_the_attribution_is_captured_before_each_new_hypothesis_and_reaches_the_designer(
    *, search: str, restarting: bool
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 2, "attribution": PROFILE_GUIDED},
        attribution_output=attribution(),
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    prompt = host.run.expect_turn(role=PLANNER).context
    captures = [
        item
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Operation)
        and isinstance(item.request.request, CaptureCommandBase)
    ]

    assert len(captures) == 2
    assert [item.component for item in prompt.ranked_bottlenecks] == ["gemm", "io"]  # ty: ignore[unresolved-attribute]


@SEARCHES
@MODES
@settings(max_examples=6, deadline=None, derandomize=True, database=None)
@given(exit_code=st.integers(1, 255), output=st.text(max_size=20))
def test_a_failed_attribution_command_ends_the_run_naming_what_ran_and_why(
    *, search: str, restarting: bool, exit_code: int, output: str
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={
            "max_rounds": 2,
            "attribution": {"command": ("bash", "attribute.sh"), "timeout_seconds": 10},
        },
        attribution_output=output,
        attribution_exit_code=exit_code,
    )
    host.drain(limit=100)

    assert not host.opened
    finish = host.run.finished
    assert isinstance(finish, Finish)
    reason = finish.result.reason
    assert "`bash attribute.sh`" in reason
    assert f"exited with code {exit_code}" in reason


# -- the base and the evaluation budget -----------------------------------------------------------

INPUT = FACTS.baseline
SLOWER_THAN_INPUT = 30.0
"""What a candidate measures (the input measures 50): it passes review but is slower."""


def commit(name: str) -> RevisionRef:
    return RevisionRef.of_git_commit(name)


def next_plan(host: SerialHost, *plans: str) -> None:
    """The designer answers its next turn; returns once the round's implementer is asked."""
    design(host, *plans)
    host.drain_until_turn(IMPLEMENTER)


@SEARCHES
@MODES
def test_a_plan_that_names_the_input_as_its_base_starts_its_round_on_the_input(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2", base_round=0))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host)[1].revision_id == FACTS.baseline.revision_id


@SEARCHES
@MODES
def test_an_idea_starts_from_the_best_not_from_the_slower_idea_retained_after_it(
    *, search: str, restarting: bool
) -> None:
    """Regression: the base followed the newest retained checkpoint, however slow."""
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3},
        throughputs={"c1": 100.0, "c2": SLOWER_THAN_INPUT},
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    implement(host, search, "c2")
    host.drain_until_turn(PLANNER)
    prompt = host.run.expect_turn(role=PLANNER).context
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h3"))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host) == [INPUT, commit("c1"), commit("c1")]
    # the designer is shown the passed ideas and which is best
    assert prompt.base_revision == commit("c1")  # ty: ignore[unresolved-attribute]
    assert [(item.round_number, item.best) for item in prompt.passed] == [(1, True), (2, False)]  # ty: ignore[unresolved-attribute]


@SEARCHES
@MODES
def test_a_plan_may_start_from_a_passed_idea_that_is_not_the_best(
    *, search: str, restarting: bool
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3},
        throughputs={"c1": 100.0, "c2": 150.0},
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    implement(host, search, "c2")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h3", base_round=1))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host)[2] == commit("c1")


@SEARCHES
@MODES
@pytest.mark.parametrize(
    ("named", "why"),
    [(1, "names an idea that did not pass"), (7, "names no idea of this run")],
    ids=["failed", "unknown"],
)
def test_a_base_naming_an_unknown_or_failed_idea_is_refused_by_its_number(
    *, search: str, restarting: bool, named: int, why: str
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3, "max_retries_per_round": 1, "max_continuation_rounds": 0},
    )
    design(host, "h1")
    implement(host, search, "c1", passed=False)
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2", base_round=named))
    problem = getattr(host.run.expect_turn(role=PLANNER).context, "problem", None)

    assert problem is not None
    assert f"base_round {named} {why}" in problem


@SEARCHES
@MODES
def test_an_idea_that_continues_its_hypothesis_builds_on_its_own_work_in_progress(
    *, search: str, restarting: bool
) -> None:
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": 3},
        throughputs={"c1": 100.0},
    )
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    # h2 retains work in progress that did not pass; the best is still c1
    implement(host, search, "c2", passed=False)
    host.drain()

    # round 3 continues h2 (no designer turn), on the work in progress round 2 retained
    assert bases(host)[:3] == [INPUT, commit("c1"), commit("c2")]


@SEARCHES
@MODES
def test_a_round_beyond_the_evaluation_budget_is_never_a_winner_and_never_the_next_base(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2, "evaluation_budget": 1})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    implement(host, search, "c2")
    host.drain()

    first, second = settlements(host)[:2]
    assert (first.eligible, first.retention) == (True, "candidate")
    assert (second.eligible, second.retention) == (False, "candidate")
    assert host.measured == ["c1"]
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection is not None
    assert finish.result.selection.kind != "retained_candidate" or (
        finish.result.selection.revision == commit("c1")
    )
    assert_flow_closed(host)


@SEARCHES
@MODES
@settings(max_examples=examples(pr=6, nightly=40), deadline=None, derandomize=True, database=None)
@given(
    rounds=st.lists(
        st.tuples(
            st.sampled_from([30.0, 70.0, 90.0, 120.0, 160.0]),
            st.booleans(),
            st.none() | st.integers(0, 4),
        ),
        min_size=1,
        max_size=4,
    )
)
def test_every_base_is_the_best_a_named_passed_idea_or_the_input_and_the_best_was_measured(
    *, search: str, restarting: bool, rounds: list[tuple[float, bool, int | None]]
) -> None:
    """Whatever each idea measures, passes and names as its base, the run follows the rules.

    The oracle: an idea passes when it passes review (the host's gates always read); it is best
    when it also beats the input (50) by more than every earlier best; a named base is honored
    only for the input or a passed idea, else the plan is refused and asked again without one.
    """
    speeds = {f"c{number}": speed for number, (speed, _, _) in enumerate(rounds, start=1)}
    host = start(
        search,
        restarting=restarting,
        config={"max_rounds": len(rounds), "max_continuation_rounds": 0},
        throughputs=speeds,
    )
    passed: dict[int, RevisionRef] = {}
    best: tuple[float, int] | None = None
    expected: list[RevisionRef] = []
    for number, (speed, ok, drawn_base) in enumerate(rounds, start=1):
        named = drawn_base
        host.drain_until_turn(PLANNER)
        asked = host.run.expect_turn(role=PLANNER)
        base_of_best = INPUT if best is None else commit(f"c{best[1]}")
        honored = named is None or named == 0 or named in passed
        extra = {} if named is None else {"base_round": named}
        host.run.reply_raw(asked, plan_reply(f"h{number}", **extra))
        if not honored:
            correction = host.run.expect_turn(role=PLANNER)
            assert f"base_round {named} " in str(correction.context.problem)  # ty: ignore[unresolved-attribute]
            host.run.reply_raw(correction, plan_reply(f"h{number}"))
            named = None
        expected.append(base_of_best if named is None else INPUT if named == 0 else passed[named])
        implement(host, search, f"c{number}", passed=ok)
        if ok:
            passed[number] = commit(f"c{number}")
            if speed > 50.0 and (best is None or speed > best[0]):
                best = (speed, number)
    host.drain()

    assert bases(host) == expected
    finish = host.run.finished
    assert isinstance(finish, Finish)
    selection = finish.result.selection
    assert selection is not None
    if best is None:
        assert selection.kind != "retained_candidate"
    else:
        assert selection.kind == "retained_candidate"
        assert selection.revision == commit(f"c{best[1]}")
        # the adopted result rests on a trusted measurement of that exact commit
        assert selection.revision.revision_id.root in host.measured
    assert_flow_closed(host)


# -- the paid turns and the workspace --------------------------------------------------------


@SEARCHES
@MODES
@settings(max_examples=8, deadline=None, derandomize=True, database=None)
@given(retries=st.integers(1, 3))
def test_a_round_never_spends_more_paid_turns_than_its_retry_budget(
    *, search: str, restarting: bool, retries: int
) -> None:
    host = start(search, restarting=restarting, config={"max_retries_per_round": retries})
    design(host, "h1")
    for _ in range(retries + 1):  # more rejections than the budget allows
        if not any(item.label == IMPLEMENTER for item in host.run.open):
            host.step(0)
        if any(item.label == IMPLEMENTER for item in host.run.open):
            implement(host, search, "c1", passed=False)
    host.drain()

    assert paid_turns(host, IMPLEMENTER) <= retries
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_the_retained_checkpoint_of_a_round_that_failed_review_is_work_in_progress(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_retries_per_round": 1})
    design(host, "h1")
    implement(host, search, "c1", passed=False)
    host.drain()

    (settled,) = settlements(host)
    assert not settled.eligible
    assert settled.retention in {"wip", "discard"}
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_a_settlement_core_refuses_ends_the_round_instead_of_stranding_it(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    design(host, "h1")
    implement(host, search, "c1")
    # Every close of the workspace is refused once with its assertion; core takes the plain close.
    for _ in range(100):
        pending = host.open(Withdraw)
        if pending:
            host.answer(pending[0], 3)
        elif not host.step(0):
            break
    assert host.refused_closes >= 1
    assert_flow_closed(host)


@SEARCHES
@MODES
def test_a_start_core_refuses_is_a_failed_round_not_a_stall(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting)
    host.refusals["start"] = 1
    design(host, "h1")
    for asked in host.open(StartAttempt):
        host.answer(asked)
    host.drain()

    assert host.refused_starts == 1
    assert_flow_closed(host)


@SEARCHES
@MODES
@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "exclusive-root"])
def test_the_round_works_where_the_environment_lets_a_candidate_work(
    *, search: str, restarting: bool, isolated: bool
) -> None:
    host = start(search, restarting=restarting, config={"exclusive_root": not isolated})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain()

    expected = WorkspaceMode.ISOLATED_CHILD if isolated else WorkspaceMode.EXCLUSIVE_ROOT
    plans = [
        item.request.workspace.mode
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, StartAttempt)
    ]
    turns = [
        item.request.turn.workspace.mode
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.session.role_id.root.endswith(IMPLEMENTER)
        and hasattr(item.request.turn.workspace, "mode")
    ]
    assert plans == [expected]
    assert turns
    assert set(turns) == {expected}
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection is not None
    assert_flow_closed(host)
