"""The ending of a run: the framework adopts the last ``ctx.choose`` by one path.

A flow never finishes the run. Whatever ends it (the root returning, the operator's stop, the
deadline control), the framework proposes the last choice (nothing chosen: the input), then
finishes with the result the adoption allows. Each story runs live and with a restart after
every step, so the choice itself is proven to survive a restart.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .flow_support import FACTS, Config, start

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api.testing import FlowRun

type Begin = Callable[..., FlowRun[Config]]

BASELINE = core.TrustedBaseline(revision=FACTS.baseline)


@pytest.fixture(params=[False, True], ids=["live", "restart-every-step"])
def begin(request: pytest.FixtureRequest) -> Begin:
    """Starts a flow; a live run, or a restart after every step."""
    restarting = bool(request.param)
    return lambda root, **kwargs: start(root, restarting=restarting, **kwargs)


def kept(name: str) -> core.Kept:
    """A candidate a closed workspace retained."""
    revision = core.RevisionRef(revision_id=core.RevisionId(root=name), digest=f"{name}-digest")
    return core.Kept(
        settlement=core.Settlement(
            settlement_id=core.SettlementId(root=name),
            attempt=core.AttemptRef(attempt_id=core.AttemptId(root=name), generation=0),
            candidate=revision,
            assessments=(),
            eligible=True,
            retention="candidate",
            outcome="succeeded",
        )
    )


def selection_of(candidate: core.Kept) -> core.RetainedCandidate:
    """The selection ``ctx.choose(candidate)`` stands for."""
    return core.RetainedCandidate(
        settlement_id=candidate.settlement.settlement_id, revision=candidate.revision
    )


def result_of(run: FlowRun[Config], mode: str = "drain") -> core.RunResultProposal:
    """The result the run finished with, in the mode its stop (if any) chose."""
    assert run.finished is not None
    assert run.finished.mode == mode
    return run.finished.result


# -- a normal end ---------------------------------------------------------------------------------


def test_nothing_chosen_means_the_input_stands(begin: Begin) -> None:
    async def idle(_ctx: core.Flow, _config: Config) -> None:
        return None

    run = begin(idle)
    result = result_of(run)
    assert (result.outcome, result.selection) == ("success", BASELINE)
    assert result.reason.startswith("kept-input")


def test_the_last_choice_is_the_one_adopted(begin: Begin) -> None:
    async def choosy(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(kept("a"))
        ctx.choose(kept("b"))
        await ctx.timer(5.0)

    run = begin(choosy, adopting=False)
    run.advance_to(5.0)
    proposal = run.expect(core.ProposeWinner)
    assert proposal.request.selection == selection_of(kept("b"))  # ty: ignore[unresolved-attribute]
    run.adopt(proposal)
    result = result_of(run)
    assert (result.outcome, result.selection) == ("success", selection_of(kept("b")))
    assert result.reason.startswith("adopted")


@pytest.mark.parametrize("chosen", [True, False], ids=["a-candidate", "nothing"])
def test_an_uncompared_return_says_so_only_of_an_adopted_candidate(
    begin: Begin, *, chosen: bool
) -> None:
    """A root that had no input reading to compare with says its adopted candidate is uncompared.

    Keeping the input needs no comparison, so a run that chose nothing still keeps the input.
    """

    async def uncompared(ctx: core.Flow, _config: Config) -> core.Uncompared:
        if chosen:
            ctx.choose(kept("a"))
        return core.Uncompared()

    result = result_of(begin(uncompared))
    assert result.outcome == "success"
    if chosen:
        assert result.selection == selection_of(kept("a"))
        assert result.reason == "adopted: a"
        assert result.uncompared
    else:
        assert result.selection == BASELINE
        assert result.reason.startswith("kept-input")


def test_choosing_the_input_after_a_candidate_takes_the_candidate_back(begin: Begin) -> None:
    async def regret(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(kept("a"))
        ctx.choose(core.INPUT)

    result = result_of(begin(regret))
    assert result.selection == BASELINE


def test_no_winner_fails_the_run_and_adopts_nothing(begin: Begin) -> None:
    async def hopeless(ctx: core.Flow, _config: Config) -> core.NoWinner:
        ctx.choose(kept("a"))  # even a choice made earlier is not adopted
        return core.NoWinner(reason="nothing converged")

    run = begin(hopeless, adopting=False)
    assert not any(
        isinstance(d, core.Wait) and isinstance(d.request, core.ProposeWinner)
        for d in run.decisions
    )
    result = result_of(run)
    assert (result.outcome, result.selection) == ("failure", None)
    assert result.reason == "no-winner: nothing converged"


def test_no_winner_that_keeps_the_choice_fails_the_run_and_adopts_it(begin: Begin) -> None:
    async def partial(ctx: core.Flow, _config: Config) -> core.NoWinner:
        ctx.choose(kept("a"))
        return core.NoWinner(reason="out of rounds", keep_choice=True)

    result = result_of(begin(partial))
    assert result.outcome == "failure"
    assert result.reason == "no-winner: out of rounds"
    assert result.selection == core.RetainedCandidate(
        settlement_id=kept("a").settlement.settlement_id, revision=kept("a").revision
    )


def test_no_winner_that_keeps_the_choice_but_chose_nothing_adopts_nothing(begin: Begin) -> None:
    async def nothing(_ctx: core.Flow, _config: Config) -> core.NoWinner:
        return core.NoWinner(reason="nothing to keep", keep_choice=True)

    result = result_of(begin(nothing, adopting=False))
    assert (result.outcome, result.selection) == ("failure", None)


@given(reason=st.sampled_from(list(core.AdoptionFailureReason)))
def test_a_kept_choice_that_core_refuses_is_named_in_the_failure(
    reason: core.AdoptionFailureReason,
) -> None:
    async def partial(ctx: core.Flow, _config: Config) -> core.NoWinner:
        ctx.choose(kept("a"))
        return core.NoWinner(reason="out of rounds", keep_choice=True)

    run = start(partial, adopting=False)
    run.refuse_adoption(run.expect(core.ProposeWinner), reason)
    result = result_of(run)
    assert (result.outcome, result.selection) == ("failure", None)
    assert result.reason == f"no-winner: out of rounds; adoption-failed: {reason.value}"


@given(reason=st.sampled_from(list(core.AdoptionFailureReason)))
def test_a_failed_adoption_is_a_failed_run_with_the_typed_reason(
    reason: core.AdoptionFailureReason,
) -> None:
    async def choosy(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(kept("a"))

    run = start(choosy, adopting=False)
    run.refuse_adoption(run.expect(core.ProposeWinner), reason)
    result = result_of(run)
    assert result.outcome == "failure"
    assert result.selection is None
    assert result.reason == f"adoption-failed: {reason.value}"


def test_a_proposal_core_refuses_is_a_failed_run_too(begin: Begin) -> None:
    async def choosy(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(kept("a"))

    run = begin(choosy, adopting=False)
    run.fail(run.expect(core.ProposeWinner), core.FailureReason.REFUSED)
    result = result_of(run)
    assert result.outcome == "failure"
    assert result.reason.startswith("adoption-failed")


def test_a_refused_result_is_proposed_again_as_a_plain_failure_once(begin: Begin) -> None:
    async def idle(_ctx: core.Flow, _config: Config) -> None:
        return None

    run = begin(idle)
    assert result_of(run).outcome == "success"
    run.fail(run.expect(core.Finish), core.FailureReason.REFUSED)
    result = result_of(run)
    assert result.outcome == "failure"
    assert result.reason.startswith("finish-refused")
    with pytest.raises(core.ContractError, match="plain failure"):
        run.fail(run.expect(core.Finish, label="finish"), core.FailureReason.REFUSED)


# -- stop and deadline ---------------------------------------------------------------------------


async def until_stopped(ctx: core.Flow, _config: Config) -> None:
    ctx.choose(kept("a"))
    await ctx.stopped


def test_a_stop_adopts_the_last_choice_and_cancels_the_run(begin: Begin) -> None:
    run = begin(until_stopped)
    assert run.finished is None
    run.stop()
    result = result_of(run)
    assert (result.outcome, result.selection) == ("cancelled", selection_of(kept("a")))
    assert result.reason.startswith("stopped")


def test_the_deadline_control_cancels_the_run_with_its_own_reason(begin: Begin) -> None:
    run = begin(until_stopped)
    run.stop("deadline")
    result = result_of(run)
    assert (result.outcome, result.selection) == ("cancelled", selection_of(kept("a")))
    assert result.reason.startswith("deadline")


@pytest.mark.parametrize("ending", [None, "stop", "deadline"], ids=["returned", "stop", "deadline"])
@pytest.mark.parametrize("chosen", [True, False], ids=["a-candidate", "nothing"])
def test_an_uncompared_adoption_is_a_field_of_the_result_however_the_run_ends(
    begin: Begin, *, ending: str | None, chosen: bool
) -> None:
    """The mark is a fact of the result, not of its reason's first word, so a stop keeps it.

    A run ends by its root returning, an operator's stop or the deadline; whichever it is, a
    result that adopted a candidate the root returned ``Uncompared`` for says so, and one that
    kept the input does not (keeping the input needs no comparison).
    """

    async def uncompared(ctx: core.Flow, _config: Config) -> core.Uncompared:
        if chosen:
            ctx.choose(kept("a"))
        if ending is not None:
            await ctx.stopped
        return core.Uncompared()

    run = begin(uncompared)
    if ending is not None:
        run.stop(ending)
    result = result_of(run)
    assert result.selection == (selection_of(kept("a")) if chosen else BASELINE)
    assert result.outcome == ("success" if ending is None else "cancelled")
    assert result.uncompared is chosen


def test_a_stop_with_no_choice_cancels_with_the_input(begin: Begin) -> None:
    async def waiting(ctx: core.Flow, _config: Config) -> None:
        await ctx.stopped

    run = begin(waiting)
    run.stop()
    result = result_of(run)
    assert (result.outcome, result.selection) == ("cancelled", BASELINE)


def test_a_stop_that_ends_in_no_winner_is_cancelled_without_a_selection(begin: Begin) -> None:
    async def giving_up(ctx: core.Flow, _config: Config) -> core.NoWinner:
        await ctx.stopped
        return core.NoWinner(reason="out of time")

    run = begin(giving_up)
    run.stop()
    result = result_of(run)
    assert (result.outcome, result.selection) == ("cancelled", None)
    assert result.reason == "stopped: out of time"


def test_a_failed_adoption_after_the_deadline_is_still_a_failure(begin: Begin) -> None:
    run = begin(until_stopped, adopting=False)
    run.stop("deadline")
    run.refuse_adoption(run.expect(core.ProposeWinner), core.AdoptionFailureReason.VERIFY_FAILED)
    assert result_of(run).outcome == "failure"


@pytest.mark.parametrize("mode", list(core.StopMode))
@given(reason=st.sampled_from(list(core.AdoptionFailureReason)))
@example(reason=core.AdoptionFailureReason.ADOPT_FAILED)
def test_an_adoption_that_fails_under_the_operators_stop_is_no_failure_of_the_run(
    mode: core.StopMode, reason: core.AdoptionFailureReason
) -> None:
    """Regression: an adoption a stop raced ended the run as failed, and no resume could reopen it.

    A stop is a pause the operator can lift. What the adoption did is no verdict on the run: the
    result is the stop's (``cancelled``, with the adoption in the reason), whichever way the
    adoption failed, and the run stays resumable.
    """
    run = start(until_stopped, adopting=False)
    run.stop(mode=mode)
    run.refuse_adoption(run.expect(core.ProposeWinner), reason)
    result = result_of(run, mode.value)
    assert result.outcome == "cancelled"
    assert result.reason == f"stopped: adoption-failed: {reason.value}"
    assert result.selection is None


@pytest.mark.parametrize("mode", list(core.StopMode))
@given(cause=st.sampled_from(list(core.RejectionCode)))
@example(cause=core.RejectionCode.ALREADY_SETTLED)
def test_a_proposal_core_refuses_under_the_operators_stop_is_no_failure_of_the_run(
    mode: core.StopMode, cause: core.RejectionCode
) -> None:
    """Regression: core refused the stop's proposal (``already-settled``), and the run ended for good."""
    run = start(until_stopped, adopting=False)
    run.stop(mode=mode)
    run.fail(run.expect(core.ProposeWinner), core.FailureReason.REFUSED, cause=cause)
    result = result_of(run, mode.value)
    assert result.outcome == "cancelled"
    assert result.reason == "stopped: adoption-failed: refused"


@given(stop_after=st.integers(min_value=0, max_value=2))
def test_the_ending_is_the_same_whenever_the_stop_comes(stop_after: int) -> None:
    """However many timers fired before the stop, the choice made along the way is adopted."""

    async def ticking(ctx: core.Flow, _config: Config) -> None:
        for number in range(3):
            ctx.choose(kept(f"c{number}"))
            await ctx.first(ctx.timer(float(number + 1)), ctx.stopped)
            if ctx.stop_requested:
                return

    run = start(ticking, restarting=True)
    for tick in range(stop_after):
        run.advance_to(float(tick + 1))
    run.stop()
    result = result_of(run)
    assert result.outcome == "cancelled"
    assert result.selection == selection_of(kept(f"c{stop_after}"))


def test_a_stopped_run_whose_proposal_core_refuses_is_cancelled_not_failed() -> None:
    async def choosy(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(kept("a"))
        await ctx.stopped

    run = start(choosy, adopting=False)
    run.stop()
    run.fail(
        run.expect(core.ProposeWinner),
        core.FailureReason.REFUSED,
        cause=core.RejectionCode.CLOSED_SCOPE,
    )
    result = result_of(run)
    assert result.outcome == "cancelled"
    assert result.reason == "stopped: adoption-failed: refused"


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_an_unset_uncompared_is_left_out_of_the_result_serialization(
    outcome: Literal["success", "failure", "cancelled"],
) -> None:
    """Decision digests and version 38 documents hold no ``uncompared`` key for a compared run."""
    plain = core.RunResultProposal(outcome=outcome, reason="r")
    assert "uncompared" not in json.loads(plain.model_dump_json())
    assert plain == core.RunResultProposal.model_validate_json(plain.model_dump_json())


# -- who stopped the run decides how it ends -------------------------------------------------------


@pytest.mark.parametrize("adoption", ["adopted", "refused", "verify-failed", "no-winner"])
@pytest.mark.parametrize("requester", ["operator", "deadline", "environment"])
def test_the_operator_pauses_the_deadline_ends_and_the_run_environment_fails_the_run(
    requester: str, adoption: str
) -> None:
    """Regression (sweep F2): a run its environment stopped ended ``stopped``, like a pause.

    The run environment stops the run when it can run none of the run's work (a probe that
    failed for good). That ending is a failure, whatever the adoption did; the operator's stop
    stays a cancelled pause, and the deadline's a cancelled end.
    """
    control = {
        "operator": "stop",
        "deadline": core.DEADLINE_STOP,
        "environment": core.ENVIRONMENT_STOP,
    }[requester]

    async def ended(ctx: core.Flow, _config: Config) -> core.NoWinner | None:
        ctx.choose(kept("a"))
        await ctx.stopped
        return core.NoWinner(reason="gave up") if adoption == "no-winner" else None

    run = start(ended, adopting=adoption == "adopted")
    run.stop(control, mode=core.StopMode.CANCEL)
    if adoption == "refused":
        run.fail(run.expect(core.ProposeWinner), core.FailureReason.REFUSED)
    elif adoption == "verify-failed":
        run.refuse_adoption(
            run.expect(core.ProposeWinner), core.AdoptionFailureReason.VERIFY_FAILED
        )
    result = result_of(run, "cancel")
    first = result.reason.split(":", 1)[0]
    if requester == "environment":
        assert (result.outcome, first) == ("failure", core.EndReason.FAILED.value)
    elif requester == "operator":
        assert (result.outcome, first) == ("cancelled", core.EndReason.STOPPED.value)
    else:
        failed = adoption == "verify-failed"
        assert result.outcome == ("failure" if failed else "cancelled")
        assert first == ("adoption-failed" if failed else core.EndReason.DEADLINE.value)


@pytest.mark.parametrize("operator", [core.StopMode.DRAIN, core.StopMode.CANCEL])
def test_an_environment_failure_after_the_operators_stop_fails_the_run(
    begin: Begin, operator: core.StopMode
) -> None:
    """Regression (review follow-up 1 of #3327): the first stop alone named the ending.

    The operator stopped the run, then its environment failed for good while the run wound down.
    The result said ``stopped`` (a resumable pause) while its controls said no resume reopens it.
    A final stop of the host's decides the ending over an operator's stop recorded before it.
    """

    async def ended(ctx: core.Flow, _config: Config) -> None:
        ctx.choose(kept("a"))
        await ctx.stopped

    run = begin(ended, adopting=False)
    run.stop("stop", mode=operator)
    run.stop(core.ENVIRONMENT_STOP, mode=core.StopMode.CANCEL)
    run.adopt(run.expect(core.ProposeWinner))
    result = run.finished.result if run.finished is not None else None
    assert result is not None
    assert (result.outcome, result.reason.split(":", 1)[0]) == (
        "failure",
        core.EndReason.FAILED.value,
    )
