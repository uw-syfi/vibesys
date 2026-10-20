"""Adoption: make the winning revision the run's result in the root workspace.

A winner is a retained eligible candidate or the run's trusted baseline. Adoption
runs in rounds. Each round issues one AdoptRevision (restore the root to the
selected revision) and, once that is applied or unknown, one VerifyAdoption (prove
the root holds exactly that revision). Only a positive verification publishes
AdoptionResult, and it publishes it once.

Request identities derive from the selection and a round number, and the round
number is the count of AdoptRevision records already in the record ledger. The
ledger is durable, so a restarted run recomputes the same identities, never
re-adopts when a verification is merely outstanding, and a retry gets a fresh
identity that the executor answers by inspecting the root before restoring it.

Adoption stores its progress in ``SettlementState.adoption``:

- no observation: the round's AdoptRevision is outstanding.
- an adopt observation that is applied or unknown: VerifyAdoption is outstanding.
- a verify observation that is pending: VerifyAdoption is outstanding.
- ``verified``: complete. Later events change nothing, except that a resume which lifts the
  stop the adoption answered takes it back (`reopen`): the run is not ending.
- ``failure`` set: the adoption failed for good, AdoptionFailed was emitted once,
  and the fence is released.

Rounds are bounded in one place, ``_may_start_round``: the ledger holds at most
``Limits.max_adoption_rounds`` AdoptRevision rounds per selection that did not verify,
whether they came from an automatic retry or from the strategy proposing the winner again.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

from ._values import digest
from .types.attempts import AttemptPhase
from .types.common import (
    CompletionStatus,
    ContractValidationError,
    DecisionId,
    Observation,
    ObservationStatus,
    RejectionCode,
    RequestId,
    Scope,
    WorkspaceMode,
)
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import Outbound, within
from .types.scheduling import AdoptionFenceLifted
from .types.settlement import (
    Adoption,
    AdoptionFailed,
    AdoptionFailureReason,
    AdoptionObserved,
    AdoptionResult,
    AdoptionView,
    AdoptRevision,
    Selection,
    SettlementState,
    TrustedBaseline,
    VerifyAdoption,
    WinnerProposed,
)
from .types.strategy import Accepted, ProposeWinner, Rejected

if TYPE_CHECKING:
    from .types.kernel import RunState, SettlementContext
    from .types.records import RecordsState
    from .types.settlement import SettlementEvent


class _Phase(StrEnum):
    """Where the current adoption round stands."""

    IDLE = "idle"
    ADOPTING = "adopting"
    VERIFYING = "verifying"
    DONE = "done"
    FAILED = "failed"


def _key(selection: Selection) -> str:
    return digest(selection)[:16]


def _adopt_id(selection: Selection, round_: int) -> RequestId:
    return RequestId(root=f"adopt:{_key(selection)}:{round_}")


def _verify_id(selection: Selection, round_: int) -> RequestId:
    return RequestId(root=f"verify-adoption:{_key(selection)}:{round_}")


def _rounds(records: RecordsState, selection: Selection) -> int:
    """How many AdoptRevision requests the ledger holds for this selection."""
    return sum(
        isinstance(row.request, AdoptRevision) and row.request.selection == selection
        for row in records.records
    )


def _completed_adoptions(run: RunState, selection: Selection) -> int:
    """How many proposals of this selection ended with its adoption verified."""
    return sum(
        isinstance(item.decision, ProposeWinner)
        and item.decision.selection == selection
        and item.completion == CompletionStatus.SUCCEEDED
        for item in run.receipts
    )


def _may_start_round(context: SettlementContext, selection: Selection) -> bool:
    """The one bound on rounds: ``max_adoption_rounds`` that did not verify, the first included.

    A round that verified is an adoption that completed, not a retry: a resume that lifts a stop
    reopens the adoption (`AdoptionReopened`), and the run's later endings adopt the same winner
    again without the earlier ones using up the bound.
    """
    spent = _rounds(context.records, selection) - _completed_adoptions(context.run, selection)
    return spent < context.run.limits.max_adoption_rounds


def _ledger_has(records: RecordsState, request_id: RequestId, selection: Selection) -> bool:
    return any(
        row.request_id == request_id
        and isinstance(row.request, AdoptRevision | VerifyAdoption)
        and row.request.selection == selection
        for row in records.records
    )


def _phase(adoption: Adoption | None, records: RecordsState) -> _Phase:
    if adoption is None:
        return _Phase.IDLE
    if adoption.verified:
        return _Phase.DONE
    if adoption.failure is not None:
        return _Phase.FAILED
    if adoption.observation is None:
        return _Phase.ADOPTING
    return _observed_phase(adoption.selection, adoption.observation, records)


def _observed_phase(
    selection: Selection, observation: Observation, records: RecordsState
) -> _Phase:
    latest = max(_rounds(records, selection) - 1, 0)
    # A retryable answer is not an outcome: the record ledger asks again under the same
    # identity, and a conclusive failure arrives once its retries are spent.
    pending = observation.status == ObservationStatus.PENDING or observation.retryable
    if observation.request_id == _verify_id(selection, latest):
        return _Phase.VERIFYING if pending else _Phase.FAILED
    if observation.request_id == _adopt_id(selection, latest):
        if pending:
            return _Phase.ADOPTING
        if _awaits_inspection(observation):
            return _Phase.VERIFYING
    return _Phase.FAILED


def _awaits_inspection(observation: Observation) -> bool:
    """Applied and unknown outcomes are settled by looking at the root."""
    return observation.status in (ObservationStatus.SUCCEEDED, ObservationStatus.UNKNOWN)


def fences_root_mutation(state: SettlementState, records: RecordsState) -> bool:
    """Whether an adoption is underway, so no other root mutation is authorized.

    The fence starts when the adoption is proposed and ends when it is verified or
    a round fails. Callers that authorize a mutation of the run's root workspace
    (an exclusive-root attempt, a root restore) must refuse while this is true.
    """
    return _phase(state.adoption, records) in (_Phase.ADOPTING, _Phase.VERIFYING)


_VIEW_PHASE = {
    _Phase.IDLE: "adopting",
    _Phase.ADOPTING: "adopting",
    _Phase.VERIFYING: "verifying",
    _Phase.DONE: "verified",
    _Phase.FAILED: "failed",
}


def view(state: SettlementState, records: RecordsState) -> AdoptionView | None:
    """The strategy-facing summary of the current adoption."""
    adoption = state.adoption
    if adoption is None:
        return None
    phase = _phase(adoption, records)
    return AdoptionView(
        selection=adoption.selection,
        phase=cast("Literal['adopting', 'verifying', 'verified', 'failed']", _VIEW_PHASE[phase]),
        rounds=_rounds(records, adoption.selection),
        failure=adoption.failure,
    )


def _run_scope(run: RunState) -> Scope:
    return Scope(owner=run.run_id, generation=run.generation)


def _limit(run: RunState) -> float:
    return max(run.deadline_at - run.now_at, run.limits.reconciliation_bound)


def _adopt(context: SettlementContext, selection: Selection, round_: int) -> Outbound:
    request = AdoptRevision(
        request_id=_adopt_id(selection, round_),
        scope=_run_scope(context.run),
        selection=selection,
        evidence=context.evaluation.measurements_of(selection.revision),
    )
    return within(request, _limit(context.run))


def _verify(context: SettlementContext, selection: Selection, round_: int) -> Outbound:
    request = VerifyAdoption(
        request_id=_verify_id(selection, round_),
        scope=_run_scope(context.run),
        selection=selection,
    )
    return within(request, _limit(context.run))


def _canonical_receipt(context: SettlementContext, selection: Selection) -> DecisionId | None:
    """The newest live ProposeWinner command naming exactly this selection."""
    live = [
        item
        for item in context.run.receipts
        if isinstance(item.decision, ProposeWinner)
        and item.decision.selection == selection
        and isinstance(item.feedback, Accepted)
        and item.completion is None
    ]
    return live[-1].decision_id if live else None


def _refuse(
    state: SettlementState,
    context: SettlementContext,
    selection: Selection,
    code: RejectionCode,
    detail: str,
) -> AreaChange[SettlementState]:
    """Reject the command that proposed this winner; a bare event has no command."""
    decision = _canonical_receipt(context, selection)
    if decision is None:
        raise ContractValidationError("selection", detail)
    return AreaChange[SettlementState](
        state=state,
        events=(Rejected(decision_id=decision, code=code, path=("selection",), detail=detail),),
    )


def _ineligible(
    state: SettlementState, context: SettlementContext, selection: Selection
) -> str | None:
    """Why this selection may not be adopted, or None when it may.

    A retained candidate needs one eligible settlement that retained exactly this
    revision as a candidate (so never a work-in-progress partial) and, when the task gates on
    accuracy, an accepted accuracy proof for the same revision. A run whose facts carry no task
    terms keeps the proof requirement. The baseline must be the run's own.
    """
    if isinstance(selection, TrustedBaseline):
        if selection.revision != context.run.facts.baseline:
            return "trusted baseline differs from the run's baseline"
        return None
    if not state.retains(selection):
        return "selection is not an eligible retained candidate of this run"
    task = context.run.facts.task
    if (task is None or task.requires_accuracy_proof) and (
        context.evaluation.accuracy_proof(selection.revision) is None
    ):
        return "selected revision has no accepted accuracy proof"
    return None


def _root_holder(context: SettlementContext) -> bool:
    """Whether an attempt still holds the exclusive root the adoption would rewrite."""
    return any(
        attempt.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
        and (
            attempt.phase not in (AttemptPhase.QUEUED, AttemptPhase.TERMINAL, AttemptPhase.PARKED)
            or attempt.release_dependencies
            or attempt.pending_records
        )
        for attempt in context.attempts.attempts
    )


def _with_adoption(
    state: SettlementState, adoption: Adoption, *requests: Outbound
) -> AreaChange[SettlementState]:
    return AreaChange[SettlementState](
        state=state.model_copy(update={"adoption": adoption}), requests=requests
    )


def _existing(
    state: SettlementState, context: SettlementContext, selection: Selection
) -> AreaChange[SettlementState] | None:
    """Answer a proposal that meets an adoption already started or finished."""
    current = state.adoption
    phase = _phase(current, context.records)
    same = current is not None and current.selection == selection
    if phase == _Phase.DONE:
        if same:
            return AreaChange[SettlementState](state=state)
        detail = "a different winner is already adopted"
        return _refuse(state, context, selection, RejectionCode.ALREADY_SETTLED, detail)
    if phase in (_Phase.ADOPTING, _Phase.VERIFYING):
        if same and current is not None:
            return _replay(state, context, current, phase)
        detail = "another adoption is in progress"
        return _refuse(state, context, selection, RejectionCode.IDENTITY_CONFLICT, detail)
    return None


def _propose(
    state: SettlementState, context: SettlementContext, event: WinnerProposed
) -> AreaChange[SettlementState]:
    selection = event.selection
    existing = _existing(state, context, selection)
    if existing is not None:
        return existing
    reason = _ineligible(state, context, selection)
    if reason is not None:
        return _refuse(state, context, selection, RejectionCode.EVIDENCE, reason)
    if _root_holder(context):
        detail = "an attempt still holds the root workspace"
        return _refuse(state, context, selection, RejectionCode.DEPENDENCY, detail)
    if not _may_start_round(context, selection):
        detail = "adoption retries are exhausted for this selection"
        return _refuse(state, context, selection, RejectionCode.BUDGET, detail)
    round_ = _rounds(context.records, selection)
    return _with_adoption(state, Adoption(selection=selection), _adopt(context, selection, round_))


def _replay(
    state: SettlementState, context: SettlementContext, current: Adoption, phase: _Phase
) -> AreaChange[SettlementState]:
    """Re-proposing the same winner only fills a request the ledger lost."""
    selection = current.selection
    latest = max(_rounds(context.records, selection) - 1, 0)
    if phase == _Phase.ADOPTING:
        missing = not _ledger_has(context.records, _adopt_id(selection, latest), selection)
        requests = (_adopt(context, selection, latest),) if missing else ()
        return AreaChange[SettlementState](state=state, requests=requests)
    missing = not _ledger_has(context.records, _verify_id(selection, latest), selection)
    requests = (_verify(context, selection, latest),) if missing else ()
    return AreaChange[SettlementState](state=state, requests=requests)


def reopen(state: SettlementState) -> SettlementState:
    """Take back a completed adoption: the stop it answered was lifted, so the run goes on.

    A stop is a pause the operator can lift, and the adoption a stopped run completed was its
    answer to that stop, not to its ending. Once the stop is lifted the adoption is open again,
    and the ending the run reaches later adopts its own winner, whichever that is. Only a
    verified adoption is taken back. One underway finishes (its restore is a request in flight),
    and a failed one stays a failure, with the rounds it spent.
    """
    adoption = state.adoption
    if adoption is None or not adoption.verified:
        return state
    return state.model_copy(update={"adoption": None})


def _stale(adoption: Adoption, observation: Observation) -> bool:
    """A repeat or older fact about the request already recorded."""
    previous = adoption.observation
    return (
        previous is not None
        and previous.request_id == observation.request_id
        and observation.sequence <= previous.sequence
    )


def _observe(
    state: SettlementState, context: SettlementContext, event: AdoptionObserved
) -> AreaChange[SettlementState]:
    adoption = state.adoption
    observation = event.observation
    if adoption is None or adoption.verified or observation.scope != _run_scope(context.run):
        return AreaChange[SettlementState](state=state)
    selection = adoption.selection
    latest = max(_rounds(context.records, selection) - 1, 0)
    phase = _phase(adoption, context.records)
    ledger = context.records
    adopting = observation.request_id == _adopt_id(selection, latest) and _ledger_has(
        ledger, observation.request_id, selection
    )
    verifying = observation.request_id == _verify_id(selection, latest) and _ledger_has(
        ledger, observation.request_id, selection
    )
    if phase == _Phase.ADOPTING and adopting and not _stale(adoption, observation):
        return _adopt_observed(state, context, adoption, observation, latest)
    if phase == _Phase.VERIFYING and verifying and not _stale(adoption, observation):
        return _verify_observed(state, context, adoption, event, latest)
    if phase == _Phase.VERIFYING and adopting and not _stale(adoption, observation):
        previous = adoption.observation
        if (
            previous is not None
            and previous.request_id == observation.request_id
            and observation.status != ObservationStatus.PENDING
        ):
            return _adopt_observed(state, context, adoption, observation, latest)
    return AreaChange[SettlementState](state=state)


def _adopt_observed(
    state: SettlementState,
    context: SettlementContext,
    adoption: Adoption,
    observation: Observation,
    round_: int,
) -> AreaChange[SettlementState]:
    recorded = adoption.model_copy(update={"observation": observation})
    if observation.status == ObservationStatus.PENDING or observation.retryable:
        return _with_adoption(state, recorded)
    if not _awaits_inspection(observation):
        return _fail(state, context, recorded, AdoptionFailureReason.ADOPT_FAILED)
    selection = adoption.selection
    if _ledger_has(context.records, _verify_id(selection, round_), selection):
        return _with_adoption(state, recorded)
    return _with_adoption(state, recorded, _verify(context, selection, round_))


def _verify_observed(
    state: SettlementState,
    context: SettlementContext,
    adoption: Adoption,
    event: AdoptionObserved,
    round_: int,
) -> AreaChange[SettlementState]:
    observation = event.observation
    selection = adoption.selection
    if observation.status == ObservationStatus.PENDING or observation.retryable:
        return _with_adoption(state, adoption.model_copy(update={"observation": observation}))
    if observation.status == ObservationStatus.SUCCEEDED and event.revision == selection.revision:
        done = Adoption(selection=selection, observation=observation, verified=True)
        return AreaChange[SettlementState](
            state=state.model_copy(update={"adoption": done}),
            signals=_completion(context, selection, CompletionStatus.SUCCEEDED),
            events=(AdoptionResult(selection=selection, observation=observation),),
        )
    recorded = adoption.model_copy(update={"observation": observation})
    if observation.status == ObservationStatus.SUCCEEDED:
        # Applied content differs from the selection: definite, so no retry.
        return _fail(state, context, recorded, AdoptionFailureReason.CONTENT_MISMATCH)
    if not _awaits_inspection(observation):
        return _fail(state, context, recorded, AdoptionFailureReason.VERIFY_FAILED)
    if _may_start_round(context, selection):
        return _with_adoption(
            state, Adoption(selection=selection), _adopt(context, selection, round_ + 1)
        )
    return _fail(state, context, recorded, AdoptionFailureReason.RETRIES_EXHAUSTED)


def _completion(
    context: SettlementContext, selection: Selection, status: CompletionStatus
) -> tuple[DecisionCompleted, ...]:
    """Complete the proposing command only once its adoption is verified or failed.

    The restore request finishing proves nothing, so this is the sole completion
    of a ProposeWinner decision. A bare WinnerProposed event has no command.
    """
    decision = _canonical_receipt(context, selection)
    return () if decision is None else (DecisionCompleted(decision_id=decision, status=status),)


def _fail(
    state: SettlementState,
    context: SettlementContext,
    adoption: Adoption,
    reason: AdoptionFailureReason,
) -> AreaChange[SettlementState]:
    """Record the terminal failure and tell the strategy, once."""
    observation = adoption.observation
    if observation is None:
        raise ContractValidationError("observation", "an adoption fails on an observation")
    failed = adoption.model_copy(update={"failure": reason})
    return AreaChange[SettlementState](
        state=state.model_copy(update={"adoption": failed}),
        signals=_completion(context, adoption.selection, CompletionStatus.FAILED),
        events=(
            AdoptionFailed(
                selection=adoption.selection,
                reason=reason,
                observation=observation,
                rounds=_rounds(context.records, adoption.selection),
            ),
        ),
    )


def advance(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Consume only the two adoption events, preserving sibling-owned state fields."""
    if isinstance(event, WinnerProposed):
        change = _propose(state, context, event)
    elif isinstance(event, AdoptionObserved):
        change = _observe(state, context, event)
    else:
        raise ContractValidationError("event.kind", "event does not belong to adoption")
    return _announce_fence_lift(state, context, change)


def _announce_fence_lift(
    before: SettlementState, context: SettlementContext, change: AreaChange[SettlementState]
) -> AreaChange[SettlementState]:
    """Tell Scheduling once when the root fence lifts, so queued admissions retry."""
    if fences_root_mutation(before, context.records) and not fences_root_mutation(
        change.state, context.records
    ):
        return change.model_copy(update={"signals": (*change.signals, AdoptionFenceLifted())})
    return change
