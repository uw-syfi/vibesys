"""A workstream whose implementer ends without a candidate is asked again only with news, as a flow.

`refusal` (tested on its own in `test_no_progress.py`) decides from the history of unmeasured
turns. These stories pin that the workstream flow feeds it the right history: a failed turn that
retained a revision, a candidate the judge rejected, a turn that timed out; that it is the bound
of the config that ends the workstream; and that the slot then goes back to the planner, which is
not asked again while nothing it can see has changed.
"""

from __future__ import annotations

from enum import StrEnum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.vibesys_flows import across_restarts
from tests.vibesys.orchestration.dynamic._host import (
    PLANNER_LABEL,
    Host,
    assert_flow_closed,
    begin,
    config,
)
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import FailureKind
from vs_core.api import (
    FailureReason,
    Finish,
    RequestTurn,
    Retained,
    RevisionRef,
    StartAttempt,
    Wait,
    Withdraw,
)
from vs_sim.api.testing import examples

MODES = across_restarts


class Turn(StrEnum):
    """How a turn of a workstream can end without a measured candidate."""

    FAILED_SAME = "failed-same"
    FAILED_NEW = "failed-new"
    REJECTED_SAME = "rejected-same"
    REJECTED_NEW = "rejected-new"
    NO_REPLY = "no-reply"


NEW_REVISION = {Turn.FAILED_NEW, Turn.REJECTED_NEW}


def start(host: Host, *hypotheses: str) -> None:
    """Plan these hypotheses and open the workspace of each."""
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(
        host.run.expect_turn(role=PLANNER_LABEL), plan_reply(*(implement(h) for h in hypotheses))
    )
    for asked in host.open(StartAttempt):
        host.answer(asked)


class Script:
    """The turns of one workstream, answered in the order given (the last repeats)."""

    def __init__(self, host: Host, turns: list[Turn]) -> None:
        """Answer ``host``'s implementer turns as ``turns`` say."""
        self.host, self.turns, self.asked, self.revisions = host, turns, 0, 0

    def revision(self, *, fresh: bool) -> Retained:
        """What core retained for the turn: always one revision, or a new one each turn."""
        self.revisions += fresh
        return Retained(revision=RevisionRef.of_git_commit(f"c{self.revisions}"))

    def answer(self) -> None:
        """Answer the open implementer turn (and the review it brings) as the script says."""
        run = self.host.run
        turn = self.turns[min(self.asked, len(self.turns) - 1)]
        self.asked += 1
        asked = run.expect_turn(role="implementer")
        if turn is Turn.NO_REPLY:
            run.fail(asked, FailureReason.TIMED_OUT)
            return
        failed = turn in {Turn.FAILED_SAME, Turn.FAILED_NEW}
        result = ImplementerResult.model_validate_json(
            implemented("implementation_failed" if failed else "supported")
        )
        run.reply(asked, result, checkpoint=self.revision(fresh=turn in NEW_REVISION))
        if not failed:
            run.reply(
                run.expect_turn(role="judge"),
                ReviewResult.model_validate_json(reviewed(passed=False)),
            )

    def play(self) -> None:
        """Answer implementer turns until the workstream asks for no more."""
        for _ in range(20):
            if not self.host.open(RequestTurn, label="implementer"):
                break
            self.answer()
        self.host.drain()


def implementer_turns(host: Host) -> int:
    """Implementer turns the run asked for, corrections included."""
    return sum(
        isinstance(item, Wait)
        and isinstance(item.request, RequestTurn)
        and item.request.turn.session.role_id.root.endswith("implementer")
        for item in host.run.decisions
    )


@MODES
@pytest.mark.parametrize(
    ("bound", "ended"), [(2, FailureKind.TURN_LIMIT), (5, FailureKind.REPEATED_TURN)]
)
def test_a_repeating_failure_costs_at_most_the_bound_then_the_slot_returns_to_the_planner(
    *, restarting: bool, bound: int, ended: FailureKind
) -> None:
    host = begin(config(max_rounds=2, max_unmeasured_turns=bound), restarting=restarting)
    start(host, "h1")
    script = Script(host, [Turn.FAILED_SAME])
    script.answer()
    script.answer()  # the first failure always earns one more turn; the second repeats it

    host.drain_until_turn(PLANNER_LABEL)
    assert implementer_turns(host) == 2
    record = host.record
    assert record is not None
    (done,) = record.finished
    assert done.failure is not None
    assert done.failure.kind is ended


@MODES
def test_the_first_failure_without_a_next_step_or_evidence_still_gets_one_more_turn(
    *, restarting: bool
) -> None:
    host = begin(config(), restarting=restarting)
    start(host, "h1")
    asked = host.run.expect_turn(role="implementer")
    host.run.reply(
        asked,
        ImplementerResult.model_validate_json(implemented("implementation_failed")),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )

    again = host.run.expect_turn(role="implementer")
    assert getattr(again.context, "turns_without_candidate", None) == 1
    host.run.reply(
        again,
        ImplementerResult.model_validate_json(implemented()),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c2")),
    )
    host.run.reply(host.run.expect_turn(role="judge"), ReviewResult.model_validate_json(reviewed()))
    host.drain()

    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection.kind == "retained_candidate"  # ty: ignore[unresolved-attribute]
    assert_flow_closed(host)


@MODES
def test_a_turn_that_retains_a_new_revision_is_asked_again_and_can_be_measured(
    *, restarting: bool
) -> None:
    host = begin(config(max_unmeasured_turns=3), restarting=restarting)
    start(host, "h1")
    script = Script(host, [Turn.FAILED_NEW, Turn.FAILED_NEW])
    script.answer()
    script.answer()
    assert host.open(RequestTurn, label="implementer")  # the third turn is asked
    host.run.reply(
        host.run.expect_turn(role="implementer"),
        ImplementerResult.model_validate_json(implemented()),
        checkpoint=script.revision(fresh=True),
    )
    host.run.reply(host.run.expect_turn(role="judge"), ReviewResult.model_validate_json(reviewed()))
    host.drain()

    assert implementer_turns(host) == 3
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_a_rejected_candidate_the_judge_saw_before_does_not_earn_a_turn_under_a_larger_bound(
    *, restarting: bool
) -> None:
    host = begin(config(max_unmeasured_turns=5), restarting=restarting)
    start(host, "h1")
    Script(host, [Turn.REJECTED_SAME]).play()

    assert implementer_turns(host) == 2
    record = host.record
    assert record is not None
    (done,) = record.finished
    assert done.failure is not None
    assert done.failure.kind is FailureKind.REPEATED_TURN


@MODES
@settings(max_examples=examples(pr=10, nightly=25), deadline=None, derandomize=True, database=None)
@given(
    turns=st.lists(st.sampled_from(Turn), min_size=1, max_size=8),
    bound=st.integers(1, 4),
)
def test_unmeasured_turns_follow_the_bound_whatever_the_outcomes(
    *, restarting: bool, turns: list[Turn], bound: int
) -> None:
    """Whatever the outcomes, a workstream costs at most `bound` turns and at least two.

    Failed turns, rejected candidates and turns without a reply all count; the first one always
    earns another turn when the bound allows; and with every turn leaving the same revision the
    agent's wording cannot buy more.
    """
    host = begin(config(max_unmeasured_turns=bound), restarting=restarting)
    start(host, "h1")
    Script(host, turns).play()

    taken = implementer_turns(host)
    assert min(bound, 2) <= taken <= bound
    if not NEW_REVISION & set(turns) and Turn.NO_REPLY not in turns:
        assert taken <= 2
    assert isinstance(host.run.finished, Finish)
    assert_flow_closed(host)


@MODES
def test_the_planner_is_not_re_asked_while_nothing_it_sees_has_changed(*, restarting: bool) -> None:
    """A free slot beside a running workstream costs one planning call, not a retry loop.

    live-2: four planner turns in 45 s for one free slot, each repeating a plan the run could
    not accept. One planning call is its first reply plus the corrections it may spend; another
    needs a workstream to finish, which changes what the planner sees. h1 fails at once and h2
    stays in flight; the planner keeps answering with h1 again, which the run cannot accept.
    """
    host = begin(
        config(max_rounds=3, max_in_flight=2, max_unmeasured_turns=1), restarting=restarting
    )
    start(host, "h1", "h2")
    first = next(
        item
        for item in host.run.open
        if item.label == "implementer" and getattr(item.context, "hypothesis_id", "") == "h1"
    )
    host.run.reply(
        first,
        ImplementerResult.model_validate_json(implemented("implementation_failed")),
        checkpoint=Retained(revision=RevisionRef.of_git_commit("c1")),
    )
    for asked in host.open(Withdraw, label="settle"):  # close h1's workspace
        host.answer(asked)

    planner_turns = 0
    for _ in range(12):
        planner = host.open(RequestTurn, label=PLANNER_LABEL)
        if not planner:
            break
        planner_turns += 1
        host.run.reply_raw(planner[0], plan_reply(implement("h1")))
    # one call: the reply and the correction it is given, whatever the planner answers
    assert planner_turns == 2
    assert not host.open(RequestTurn, label=PLANNER_LABEL)
    record = host.record
    assert record is not None
    assert [item.plan.work_id for item in record.running] == ["h2"]
