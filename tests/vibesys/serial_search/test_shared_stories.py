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
from tests.vibesys.serial_search._host import FACTS, SerialHost, World, assert_flow_closed
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


# -- the base and the cadence ----------------------------------------------------------------


@SEARCHES
@MODES
def test_a_plan_that_reverts_to_the_input_starts_its_round_on_the_input(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2", revert_to_round=0))
    host.drain_until_turn(IMPLEMENTER)

    assert bases(host)[1].revision_id == FACTS.baseline.revision_id


@SEARCHES
@MODES
def test_a_round_the_cadence_skipped_is_never_a_winner_and_is_the_next_base(
    *, search: str, restarting: bool
) -> None:
    host = start(search, restarting=restarting, config={"max_rounds": 2, "official_eval_every": 2})
    design(host, "h1")
    implement(host, search, "c1")
    host.drain_until_turn(PLANNER)
    first = settlements(host)[0]

    assert (first.eligible, first.retention) == (False, "candidate")
    design_prompt = host.run.expect_turn(role=PLANNER).context
    assert design_prompt.base_revision == RevisionRef.of_git_commit("c1")  # ty: ignore[unresolved-attribute]
    host.run.reply_raw(host.run.expect_turn(role=PLANNER), plan_reply("h2"))
    implement(host, search, "c2")
    host.drain()

    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection is not None
    assert finish.result.selection.kind != "retained_candidate" or (
        finish.result.selection.revision != RevisionRef.of_git_commit("c1")
    )
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
