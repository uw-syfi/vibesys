"""A workstream whose agent turns go wrong leaves the other workstreams of the run as they were.

Several workstreams run side by side, each with its own session and workspace. One whose turns are
all lost ends as a lost workstream and returns its slot; one whose judge rejects its first change
is asked again in the same conversation and workspace and told why. Neither changes what another
workstream decides or what the run adopts. How often a lost turn is asked again is
`test_agent_waits.py`'s; what core does between (record, retry, charge) is core's own.
"""

from __future__ import annotations

from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)
from tests.vibesys.orchestration.dynamic._slots import implement_and_judge, turn_about
from tests.vibesys.search_faults import assert_flow_closed
from tests.vibesys.vibesys_flows import across_restarts

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import FailureKind, ImplementPrompt
from vs_core.api import FailureReason, Finish, RequestTurn, Retained, RevisionRef, StartAttempt

MODES = across_restarts
LOST_TURNS = 3
"""How often one implementer turn is asked before it is given up on: first ask and two more."""


def start(*, restarting: bool, **settings: object) -> Host:
    host = begin(config(**settings), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    return host


def open_slots(host: Host) -> None:
    for asked in host.open(StartAttempt):
        host.answer(asked)


def lose_until_given_up(host: Host, hypothesis: str) -> None:
    """The implementer's turns of ``hypothesis`` are lost every time they are asked."""
    for ask in range(LOST_TURNS):
        host.run.lose(turn_about(host, "implementer", hypothesis))
        if ask < LOST_TURNS - 1:
            host.run.advance_to(min(host.run.sleeping_until))


@MODES
def test_a_workstream_whose_turns_are_all_lost_ends_lost_and_the_others_still_win(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting, max_in_flight=2, max_rounds=2)
    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("A"), implement("C"))
    )
    open_slots(host)
    implement_and_judge(host, "C", "c-win")
    lose_until_given_up(host, "A")
    host.drain()

    record = host.record
    assert record is not None
    lost = next(item for item in record.finished if item.plan.work_id == "A")
    assert lost.failure is not None
    assert (lost.failure.kind, lost.failure.ended) == (FailureKind.ENDED, FailureReason.LOST)
    rounds = {item.hypothesis_id: item.rounds for item in record.hypotheses}
    assert [item.eligible for item in rounds["A"]] == [False]
    assert [item.eligible for item in rounds["C"]] == [True]
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.outcome == "success"
    assert finish.result.selection.revision.git_commit == "c-win"  # ty: ignore[unresolved-attribute]
    assert_flow_closed(host)


@MODES
def test_a_judge_rejection_asks_the_implementer_again_in_its_session_and_workspace(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting, max_rounds=1)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("B")))
    (start_asked,) = host.open(StartAttempt)
    host.answer(start_asked)
    first = turn_about(host, "implementer", "B")
    result = ImplementerResult.model_validate_json(implemented())
    host.run.reply(first, result, checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")))
    verdict = ReviewResult.model_validate_json(reviewed(passed=False))
    host.run.reply(turn_about(host, "judge", "B"), verdict)

    again = turn_about(host, "implementer", "B")
    assert isinstance(first.request, RequestTurn)
    assert isinstance(again.request, RequestTurn)
    assert again.request.turn.session == first.request.turn.session
    assert again.request.turn.workspace == first.request.turn.workspace
    assert getattr(again.context, "rejection", None) == "fix"  # the judge's feedback


@MODES
def test_a_lost_implementer_turn_is_asked_again_in_its_session_without_feedback_it_never_got(
    *, restarting: bool
) -> None:
    host = start(restarting=restarting, max_rounds=1)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("B")))
    open_slots(host)
    first = turn_about(host, "implementer", "B")

    host.run.lose(first)
    host.run.advance_to(min(host.run.sleeping_until))

    (again,) = [item for item in host.run.open if item.label == "implementer"]
    assert isinstance(first.request, RequestTurn)
    assert isinstance(again.request, RequestTurn)
    assert again.request.turn.session == first.request.turn.session
    assert again.request.turn.workspace == first.request.turn.workspace
    assert isinstance(again.context, ImplementPrompt)
    assert again.context.rejection is None
