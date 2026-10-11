"""Run-level policy: adopt the work the closed issues accumulated, then stop with the right result.

The root workspace stays at the input until adoption, so the closed issues' work lives in the
retained candidate of the last of them (each issue builds on the one before). The run adopts
that candidate at the end whatever the exit status, so work done before a budget stop or a
blocked queue is not lost. The result is a success only when the board drained quietly, and a
run whose issues changed nothing adopts nothing.
"""

from vibesys.candidates import api as candidates
from vibesys.orchestration.issue_queue.strategy._draft import Draft
from vibesys.orchestration.issue_queue.strategy._ids import IDS, decision_of_operation
from vibesys.orchestration.issue_queue.strategy._measure import verification_plan
from vibesys.orchestration.issue_queue.strategy._state import (
    IssueQueueStrategyState,
    Step,
    Verification,
)
from vs_core.api import (
    AdoptionFailed,
    AdoptionResult,
    IntentBlocked,
    Measure,
    MeasurementResult,
    Rejected,
    RevisionRef,
    RunResultProposal,
    Stop,
    run_scope,
)

OPERATOR_STOP = "stopped by the operator"


def decide(draft: Draft) -> None:
    """Close the run once nothing is in flight, propose the adoption, then stop after it."""
    state = draft.state
    if (
        state.phase is candidates.RunPhase.SEARCHING
        and state.job is None
        and (state.stopping or state.ending is not None)
    ):
        draft.update(phase=candidates.RunPhase.SELECTING)
    state = draft.state
    if state.phase is candidates.RunPhase.SELECTING:
        _propose(draft)
    elif state.phase is candidates.RunPhase.STOPPING:
        _stop(draft)


def _propose(draft: Draft) -> None:
    state = draft.state
    tip = state.tip
    if tip.settlement is None or tip.revision is None or state.stopping:
        draft.update(phase=candidates.RunPhase.STOPPING, winner=None)
        _stop(draft)
        return
    if draft.config.accuracy_configured and not state.verification.done:
        _verify(draft, tip.revision)
        return
    winner = candidates.Winner(settlement=tip.settlement, revision=tip.revision, hypothesis_id=None)
    draft.emit(candidates.proposal(IDS, draft.view, winner))
    draft.update(phase=candidates.RunPhase.ADOPTING, winner=winner)


def _verify(draft: Draft, revision: RevisionRef) -> None:
    """Measure the final candidate's accuracy; core adopts only a revision with that proof."""
    if draft.state.verification.step is Step.AWAITING:
        return
    identifier = IDS.decision_id("measure", "tip")
    plan = verification_plan(draft.config, draft.view.facts, revision, draft.view.run.now_at)
    draft.emit(Measure(decision_id=identifier, scope=run_scope(draft.view), plan=plan))
    draft.update(verification=Verification(step=Step.AWAITING, awaiting=identifier))


def _stop(draft: Draft) -> None:
    state = draft.state
    ending = state.ending
    if state.stopping:
        result = RunResultProposal(outcome="cancelled", reason=OPERATOR_STOP)
    elif ending is None:
        result = RunResultProposal(outcome="failure", reason="the run ended without a reason")
    elif ending.outcome == "success" and state.tip.settlement is not None and state.winner is None:
        reason = f"{ending.reason}, but the work of the closed issues was not adopted"
        if state.adoption_error:
            reason = f"{reason}: {state.adoption_error}"
        result = RunResultProposal(outcome="failure", reason=reason)
    else:
        # What was adopted is named whatever the ending, so the result says what the root holds.
        selection = None if state.winner is None else candidates.selection_of(state.winner)
        result = RunResultProposal(
            outcome=ending.outcome, reason=ending.reason, selection=selection
        )
    draft.emit(
        Stop(
            decision_id=IDS.decision_id("stop", "run"),
            scope=run_scope(draft.view),
            mode="drain",
            result=result,
        )
    )
    draft.update(phase=candidates.RunPhase.FINISHED)


def on_measurement(
    state: IssueQueueStrategyState, event: MeasurementResult
) -> IssueQueueStrategyState:
    """The final candidate's accuracy measurement ended; core decides at adoption whether it proved it."""
    if state.verification.step is not Step.AWAITING or event.scope.owner.kind != "run":
        return state
    return state.model_copy(update={"verification": Verification(done=True)})


def on_blocked(
    state: IssueQueueStrategyState, event: IntentBlocked
) -> IssueQueueStrategyState | None:
    """The final accuracy measurement was blocked, so its proof will not come.

    The run goes on to propose the adoption, which core refuses without an accepted accuracy
    proof; the run then stops without a winner and says why. None when the blocked request is
    not the one this step awaits.
    """
    if state.verification.step is not Step.AWAITING or event.scope.owner.kind != "run":
        return None
    if decision_of_operation(event.target.root) is not None:
        return None
    return state.model_copy(update={"verification": Verification(done=True)})


def on_rejected(state: IssueQueueStrategyState, event: Rejected) -> IssueQueueStrategyState | None:
    """Core refused the proposed adoption: stop without a winner and say why."""
    if state.phase is not candidates.RunPhase.ADOPTING:
        return None
    if event.decision_id != IDS.decision_id("propose", "run"):
        return None
    return state.model_copy(
        update={
            "phase": candidates.RunPhase.STOPPING,
            "winner": None,
            "adoption_error": event.detail,
        }
    )


def on_adoption_failed(
    state: IssueQueueStrategyState, event: AdoptionFailed
) -> IssueQueueStrategyState:
    """Core gave up on the adoption for good: stop without a winner."""
    if state.phase is not candidates.RunPhase.ADOPTING:
        return state
    return state.model_copy(
        update={
            "phase": candidates.RunPhase.STOPPING,
            "winner": None,
            "adoption_error": f"core gave up adopting it ({event.reason.value})",
        }
    )


def on_adoption(state: IssueQueueStrategyState, event: AdoptionResult) -> IssueQueueStrategyState:
    """Stop after a successful adoption; a failed one stops without a winner."""
    phase, winner = candidates.after_adoption(state.phase, state.winner, event)
    if phase is state.phase and winner is state.winner:
        return state
    error = "" if winner is not None else "core could not restore and verify the candidate"
    return state.model_copy(update={"phase": phase, "winner": winner, "adoption_error": error})
