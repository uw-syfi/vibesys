"""A workstream's candidate and settlement, as the flow decides them.

Two rules that used to live in the strategy's state machine: a resumed implementer's candidate is
the revision of its own latest checkpoint (changed or not), and a settlement core refuses never
strands a workstream, whatever the refusal code.
"""

from __future__ import annotations

import pytest
from tests.support.vibesys_flows import across_restarts
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.orchestration.dynamic._replies import (
    implement,
    implemented,
    plan_reply,
    reviewed,
)

from vibesys.orchestration.dynamic.models import ImplementerResult, ReviewResult
from vibesys.orchestration.dynamic.search.api import FailureKind
from vs_core.api import (
    Finish,
    Measure,
    RejectionCode,
    Retained,
    RevisionRef,
    Settle,
    StartAttempt,
    Wait,
    Withdraw,
)

MODES = across_restarts


def started(*, restarting: bool) -> Host:
    """The input is measured and the planner has planned one hypothesis; its slot is admitted."""
    host = begin(config(), restarting=restarting)
    host.drain_until_turn(PLANNER_LABEL)
    host.run.reply_raw(host.run.expect_turn(role=PLANNER_LABEL), plan_reply(implement("h1")))
    host.answer(host.run.expect(StartAttempt, label="start"))
    return host


def implement_turn(host: Host, commit: str) -> None:
    """The implementer answers and core retains ``commit`` for the turn."""
    result = ImplementerResult.model_validate_json(implemented())
    host.run.reply(
        host.run.expect_turn(role="implementer"),
        result,
        checkpoint=Retained(revision=RevisionRef.of_git_commit(commit)),
    )


def judge_turn(host: Host, *, passed: bool) -> None:
    verdict = ReviewResult.model_validate_json(reviewed(passed=passed))
    host.run.reply(host.run.expect_turn(role="judge"), verdict)


def settles(host: Host) -> list[Settle]:
    """The dispositions the flow closed workspaces with, in order."""
    return [
        item.request.disposition
        for item in host.run.decisions
        if isinstance(item, Wait)
        and isinstance(item.request, Withdraw)
        and isinstance(item.request.disposition, Settle)
    ]


def measured(host: Host) -> list[RevisionRef]:
    """The candidates the flow asked to have measured for scoring, in order."""
    plans = [
        item.request.plan
        for item in host.run.decisions
        if isinstance(item, Wait) and isinstance(item.request, Measure)
    ]
    return [
        plan.candidate
        for plan in plans
        if plan.purpose != "baseline" and isinstance(plan.candidate, RevisionRef)
    ]


# -- a resumed implementer ------------------------------------------------------------------------


@MODES
@pytest.mark.parametrize("unchanged", [True, False], ids=["unchanged-resume", "changed-resume"])
def test_a_resumed_implementer_settles_with_the_revision_of_its_own_latest_checkpoint(
    *, restarting: bool, unchanged: bool
) -> None:
    """The judge sends the implementer back; whether or not it changes anything, its second
    checkpoint is what is measured and what the workspace settles with."""
    host = started(restarting=restarting)
    implement_turn(host, "c1")
    judge_turn(host, passed=False)
    second = "c1" if unchanged else "c2"
    implement_turn(host, second)
    judge_turn(host, passed=True)
    host.drain()

    latest = RevisionRef.of_git_commit(second)
    assert measured(host) == [latest]
    (settle,) = settles(host)
    assert settle.candidate == latest
    assert isinstance(host.run.finished, Finish)


# -- a settlement core refuses --------------------------------------------------------------------


@MODES
@pytest.mark.parametrize("code", list(RejectionCode))
@pytest.mark.parametrize("refusals", [1, 2], ids=["assertion-refused", "every-close-refused"])
def test_a_settlement_core_refuses_ends_the_workstream_instead_of_stranding_it(
    *, restarting: bool, code: RejectionCode, refusals: int
) -> None:
    host = started(restarting=restarting)
    host.refusals["settle"] = refusals
    host.refusal_code = code
    implement_turn(host, "c1")
    judge_turn(host, passed=True)
    host.drain()

    assert host.run.finished is not None, host.run.timeline()
    assert not [item for item in host.run.open if item.until is not None], host.run.timeline()
    record = host.record
    assert record is not None
    assert not record.running
    assert len(record.finished) == 1
    # The planner is told core refused to settle the workstream as asked.
    refusal = record.finished[0].failure
    assert refusal is not None
    assert (refusal.kind, refusal.cause) == (FailureKind.REFUSED, code)
    # Core took no assertion about the candidate, so it is not eligible and the input stands.
    assert not any(row.eligible for item in record.hypotheses for row in item.rounds)
    finish = host.run.finished
    assert isinstance(finish, Finish)
    assert finish.result.selection.kind == "trusted_baseline"  # ty: ignore[unresolved-attribute]
