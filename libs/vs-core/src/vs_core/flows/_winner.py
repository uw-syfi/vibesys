"""The winner: what a flow names with ``ctx.choose``, and how the framework ends the run with it.

A flow may name its current winner any time. When the root flow returns, or the operator stops
the run, or the deadline passes (the host stops the run at the deadline), the framework takes
the last choice, has core adopt it, and finishes the run with the result. There is one path
for all three; a flow never finishes the run itself. Nothing chosen means the input stands.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from vs_core._registry import ContractError
from vs_core.types.common import THIS_RUN, DecisionId, ObservationStatus, RevisionRef, Value
from vs_core.types.search import AdoptionFinished
from vs_core.types.settlement import (
    AdoptionFailed,
    AdoptionFailureReason,
    AdoptionResult,
    RetainedCandidate,
    RunResultProposal,
    Selection,
    Settlement,
    TrustedBaseline,
)
from vs_core.types.strategy import Finish, ProposeWinner
from vs_core.types.until import Failed, FailureReason

if TYPE_CHECKING:
    from vs_core.types.search import Event

    from ._kit import Kit


class Input:
    """The run's input, named as the winner: the baseline stands, nothing changed."""

    def __repr__(self) -> str:
        """Show the sentinel by name."""
        return "INPUT"


INPUT: Final = Input()
"""``ctx.choose(INPUT)``: no candidate beat the input."""

ADOPTED: Final = "adopted"
"""The verdict of the span that adopted the chosen winner, when core adopted it."""
NOT_ADOPTED: Final = "not_adopted"
"""The verdict of that span when core would not (its reason says why)."""


class NoWinner(Value):
    """What a root flow returns when the run ends without a winner, and why.

    The run fails with this reason. Nothing is adopted, unless ``keep_choice`` says the work
    chosen so far is worth keeping although the run failed: the framework then adopts the last
    choice and the failed result carries it as its selection.
    """

    reason: str
    keep_choice: bool = False


class Kept(Value):
    """A candidate a closed workspace retained: what ``ctx.choose`` can name."""

    settlement: Settlement

    @property
    def revision(self) -> RevisionRef:
        """The retained revision."""
        if self.settlement.candidate is None:
            message = "a kept candidate has a revision"
            raise ContractError(("kept",), message)
        return self.settlement.candidate

    @classmethod
    def of(cls, settlement: Settlement) -> Kept | None:
        """The candidate ``settlement`` kept, or None when it kept none."""
        if settlement.candidate is None or settlement.retention != "candidate":
            return None
        return cls(settlement=settlement)


class EndReason(StrEnum):
    """Why the framework ended the run as it did. Closed: the result's reason starts with it."""

    ADOPTED = "adopted"
    """The chosen candidate was adopted."""
    KEPT_INPUT = "kept-input"
    """Nothing beat the input, and the input was adopted as it stood."""
    NO_WINNER = "no-winner"
    """The root flow returned ``NoWinner``."""
    ADOPTION_FAILED = "adoption-failed"
    """Core would not adopt the chosen winner."""
    STOPPED = "stopped"
    """The operator stopped the run (the result is cancelled)."""
    DEADLINE = "deadline"
    """The run deadline passed (the result is cancelled)."""
    FINISH_REFUSED = "finish-refused"
    """Core refused the result; the run ended with the plain failure."""


class Adopted(Value):
    """The adoption of the chosen winner succeeded."""

    selection: Selection


class NotAdopted(Value):
    """The adoption of the chosen winner did not succeed, and why (a closed reason)."""

    selection: Selection
    reason: AdoptionFailureReason | FailureReason


def reason_text(ending: EndReason, detail: str = "") -> str:
    """The reason a run result carries: the ending, then what it was about."""
    return f"{ending.value}: {detail}" if detail else ending.value


async def adopt(kit: Kit, selection: Selection) -> Adopted | NotAdopted:
    """Ask core to adopt ``selection`` and report how it went.

    The span of the request carries the label ``ctx.choose`` gave the choice, with the verdict
    ``ADOPTED`` or ``NOT_ADOPTED`` once core has answered.
    """
    request = ProposeWinner(
        decision_id=DecisionId(root="adopt"), scope=THIS_RUN, selection=selection
    )
    pending = kit.start(request, kit.cleanup_within())
    kit.run.adopting(pending, None, None)
    outcome = _adoption_of(selection, await kit.settle(pending))
    if isinstance(outcome, Adopted):
        kit.run.adopting(pending, ADOPTED, None)
    else:
        kit.run.adopting(pending, NOT_ADOPTED, outcome.reason.value)
    return outcome


def _adoption_of(selection: Selection, event: Event) -> Adopted | NotAdopted:
    match event:
        case AdoptionFinished(result=AdoptionResult(observation=seen)):
            if seen.status is ObservationStatus.SUCCEEDED:
                return Adopted(selection=selection)
            return NotAdopted(selection=selection, reason=AdoptionFailureReason.VERIFY_FAILED)
        case AdoptionFinished(result=AdoptionFailed(reason=reason)):
            return NotAdopted(selection=selection, reason=reason)
        case Failed(reason=reason):
            return NotAdopted(selection=selection, reason=reason)
        case unexpected:
            message = f"adoption ended with {unexpected.kind}"
            raise ContractError(("adopt",), message)


def _stop_ending(kit: Kit) -> EndReason | None:
    """How the run was stopped, if it was: the operator, or the host's deadline control."""
    stop = kit.run.stop
    if stop is None:
        return None
    return EndReason.DEADLINE if stop.control.control_id.root == "deadline" else EndReason.STOPPED


def _finish_mode(kit: Kit) -> Literal["drain", "cancel"]:
    """What the stop asked for; a run that ends by itself has nothing in flight to cancel."""
    stop = kit.run.stop
    if stop is None or stop.control.mode is None:
        return "drain"
    return stop.control.mode.value


def _selection_name(selection: Selection) -> str:
    if isinstance(selection, RetainedCandidate):
        return selection.settlement_id.root
    return "the input"


async def conclude(kit: Kit, returned: NoWinner | None) -> None:
    """End the run: adopt the last choice (unless there is no winner), then finish.

    A stop or a deadline makes the result ``cancelled`` (a proposal core refuses because the run
    is closing included); a failed adoption or a ``NoWinner`` is a ``failure`` with a typed reason,
    unless the operator stopped the run: a stop is a pause the operator can lift, so its ending
    is ``cancelled`` whatever the adoption did, and a resume reopens the run.
    """
    stopped = _stop_ending(kit)
    result = await _result(kit, returned, stopped)
    mode = _finish_mode(kit)
    refused = await kit.finish(
        Finish(decision_id=DecisionId(root="finish"), scope=THIS_RUN, mode=mode, result=result)
    )
    # Core refused the result. A run that nobody ends would hang, so the plain failure is
    # proposed once.
    plain = RunResultProposal(
        outcome="failure", reason=reason_text(EndReason.FINISH_REFUSED, refused.reason.value)
    )
    again = await kit.finish(
        Finish(decision_id=DecisionId(root="finish"), scope=THIS_RUN, mode=mode, result=plain)
    )
    message = f"core refused the plain failure of the run: {again.reason.value}"
    raise ContractError(("finish",), message)


async def _no_winner(kit: Kit, returned: NoWinner, stopped: EndReason | None) -> RunResultProposal:
    outcome: Literal["cancelled", "failure"] = "cancelled" if stopped else "failure"
    reason = reason_text(stopped or EndReason.NO_WINNER, returned.reason)
    if not returned.keep_choice or kit.run.choice is None:
        return RunResultProposal(outcome=outcome, reason=reason)
    adopted = await adopt(kit, kit.run.choice)
    if isinstance(adopted, NotAdopted):
        failed = reason_text(EndReason.ADOPTION_FAILED, adopted.reason.value)
        return RunResultProposal(outcome=outcome, reason=f"{reason}; {failed}")
    return RunResultProposal(outcome=outcome, reason=reason, selection=adopted.selection)


async def _result(
    kit: Kit, returned: NoWinner | None, stopped: EndReason | None
) -> RunResultProposal:
    if isinstance(returned, NoWinner):
        return await _no_winner(kit, returned, stopped)
    selection = kit.run.choice or TrustedBaseline(revision=kit.run.facts.baseline)
    adopted = await adopt(kit, selection)
    if isinstance(adopted, NotAdopted):
        detail = reason_text(EndReason.ADOPTION_FAILED, adopted.reason.value)
        if stopped is EndReason.STOPPED:
            # The operator's stop is a pause, so what the adoption did is no verdict on the run:
            # core may refuse the proposal because the stop races it, or the restore may fail.
            # The result says so, and the run stays resumable (`reopens_on_resume`): its next
            # ending adopts again.
            return RunResultProposal(outcome="cancelled", reason=reason_text(stopped, detail))
        if stopped is not None and adopted.reason is FailureReason.REFUSED:
            # The deadline closed the run to decisions: it ended and nothing failed.
            return RunResultProposal(outcome="cancelled", reason=reason_text(stopped, detail))
        return RunResultProposal(outcome="failure", reason=detail)
    kind = EndReason.KEPT_INPUT if isinstance(selection, TrustedBaseline) else EndReason.ADOPTED
    detail = reason_text(kind, _selection_name(selection))
    if stopped is not None:
        return RunResultProposal(
            outcome="cancelled", reason=reason_text(stopped, detail), selection=selection
        )
    return RunResultProposal(outcome="success", reason=detail, selection=selection)
