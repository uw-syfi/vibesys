"""Adoption: make the winning revision the run's result in the root workspace.

A winner is a retained eligible candidate or the run's trusted baseline. Adoption
runs in rounds. Each round issues one AdoptRevision (restore the root to the
selected revision) and, once that is applied or unknown, one VerifyAdoption (prove
the root holds exactly that revision). Only a positive verification publishes
AdoptionResult, and it publishes it once.

Request identities derive from the selection and a round number, and the round
number is the count of AdoptRevision records already in the intent ledger. The
ledger is durable, so a restarted run recomputes the same identities, never
re-adopts when a verification is merely outstanding, and a retry gets a fresh
identity that the executor answers by inspecting the root before restoring it.

Adoption stores its progress in ``SettlementState.adoption``:

- no observation: the round's AdoptRevision is outstanding.
- an adopt observation that is applied or unknown: VerifyAdoption is outstanding.
- a verify observation that is pending: VerifyAdoption is outstanding.
- ``verified``: complete. Later events change nothing.
- any other observation: the round failed and the fence is released.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from ._values import digest
from .types.attempts import AttemptPhase
from .types.common import (
    ContractValidationError,
    DecisionId,
    Observation,
    ObservationStatus,
    RejectionCode,
    RequestId,
    Scope,
    WorkspaceMode,
)
from .types.kernel import AreaChange
from .types.settlement import (
    Adoption,
    AdoptionObserved,
    AdoptionResult,
    AdoptRevision,
    Selection,
    SettlementState,
    TrustedBaseline,
    VerifyAdoption,
    WinnerProposed,
)
from .types.strategy import Accepted, ProposeWinner, Rejected

if TYPE_CHECKING:
    from .types.intents import IntentsState
    from .types.kernel import RunState, SettlementContext
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


def _rounds(intents: IntentsState, selection: Selection) -> int:
    """How many AdoptRevision requests the ledger holds for this selection."""
    return sum(
        isinstance(row.request, AdoptRevision) and row.request.selection == selection
        for row in intents.intents
    )


def _ledger_has(intents: IntentsState, request_id: RequestId, selection: Selection) -> bool:
    return any(
        row.request_id == request_id
        and isinstance(row.request, AdoptRevision | VerifyAdoption)
        and row.request.selection == selection
        for row in intents.intents
    )


def _phase(adoption: Adoption | None, intents: IntentsState) -> _Phase:
    if adoption is None:
        return _Phase.IDLE
    if adoption.verified:
        return _Phase.DONE
    if adoption.observation is None:
        return _Phase.ADOPTING
    return _observed_phase(adoption.selection, adoption.observation, intents)


def _observed_phase(
    selection: Selection, observation: Observation, intents: IntentsState
) -> _Phase:
    latest = max(_rounds(intents, selection) - 1, 0)
    pending = observation.status == ObservationStatus.PENDING
    if observation.request_id == _verify_id(selection, latest):
        return _Phase.VERIFYING if pending else _Phase.FAILED
    if observation.request_id == _adopt_id(selection, latest):
        if pending:
            return _Phase.ADOPTING
        if _awaits_inspection(observation):
            return _Phase.VERIFYING
    return _Phase.FAILED


def _awaits_inspection(observation: Observation) -> bool:
    """Applied, unknown and retryable outcomes are settled by looking at the root."""
    return observation.status in (ObservationStatus.SUCCEEDED, ObservationStatus.UNKNOWN) or (
        observation.status == ObservationStatus.FAILED and not observation.terminal
    )


def fences_root_mutation(state: SettlementState, intents: IntentsState) -> bool:
    """Whether an adoption is underway, so no other root mutation is authorized.

    The fence starts when the adoption is proposed and ends when it is verified or
    a round fails. Callers that authorize a mutation of the run's root workspace
    (an exclusive-root attempt, a root restore) must refuse while this is true.
    """
    return _phase(state.adoption, intents) in (_Phase.ADOPTING, _Phase.VERIFYING)


def _run_scope(run: RunState) -> Scope:
    return Scope(owner=run.run_id, generation=run.generation)


def _deadline(run: RunState) -> float:
    return max(run.deadline_at, run.now_at + run.limits.reconciliation_bound)


def _adopt(context: SettlementContext, selection: Selection, round_: int) -> AdoptRevision:
    return AdoptRevision(
        request_id=_adopt_id(selection, round_),
        scope=_run_scope(context.run),
        deadline_at=_deadline(context.run),
        selection=selection,
    )


def _verify(context: SettlementContext, selection: Selection, round_: int) -> VerifyAdoption:
    return VerifyAdoption(
        request_id=_verify_id(selection, round_),
        scope=_run_scope(context.run),
        deadline_at=_deadline(context.run),
        selection=selection,
    )


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
    revision as a candidate (so never a work-in-progress partial) and an accepted
    accuracy proof for the same revision. The baseline must be the run's own.
    """
    if isinstance(selection, TrustedBaseline):
        if selection.revision != context.run.facts.baseline:
            return "trusted baseline differs from the run's baseline"
        return None
    if not state.retains(selection):
        return "selection is not an eligible retained candidate of this run"
    if context.evaluation.accuracy_proof(selection.revision) is None:
        return "selected revision has no accepted accuracy proof"
    return None


def _root_holder(context: SettlementContext) -> bool:
    """Whether an attempt still holds the exclusive root the adoption would rewrite."""
    return any(
        attempt.workspace.mode == WorkspaceMode.EXCLUSIVE_ROOT
        and (
            attempt.phase not in (AttemptPhase.QUEUED, AttemptPhase.TERMINAL, AttemptPhase.PARKED)
            or attempt.release_dependencies
            or attempt.pending_intents
        )
        for attempt in context.attempts.attempts
    )


def _with_adoption(
    state: SettlementState, adoption: Adoption, *requests: AdoptRevision | VerifyAdoption
) -> AreaChange[SettlementState]:
    return AreaChange[SettlementState](
        state=state.model_copy(update={"adoption": adoption}), requests=requests
    )


def _existing(
    state: SettlementState, context: SettlementContext, selection: Selection
) -> AreaChange[SettlementState] | None:
    """Answer a proposal that meets an adoption already started or finished."""
    current = state.adoption
    phase = _phase(current, context.intents)
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
    round_ = _rounds(context.intents, selection)
    return _with_adoption(state, Adoption(selection=selection), _adopt(context, selection, round_))


def _replay(
    state: SettlementState, context: SettlementContext, current: Adoption, phase: _Phase
) -> AreaChange[SettlementState]:
    """Re-proposing the same winner only fills a request the ledger lost."""
    selection = current.selection
    latest = max(_rounds(context.intents, selection) - 1, 0)
    if phase == _Phase.ADOPTING:
        missing = not _ledger_has(context.intents, _adopt_id(selection, latest), selection)
        requests = (_adopt(context, selection, latest),) if missing else ()
        return AreaChange[SettlementState](state=state, requests=requests)
    missing = not _ledger_has(context.intents, _verify_id(selection, latest), selection)
    requests = (_verify(context, selection, latest),) if missing else ()
    return AreaChange[SettlementState](state=state, requests=requests)


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
    latest = max(_rounds(context.intents, selection) - 1, 0)
    phase = _phase(adoption, context.intents)
    ledger = context.intents
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
    if observation.status == ObservationStatus.PENDING or not _awaits_inspection(observation):
        return _with_adoption(state, recorded)
    selection = adoption.selection
    if _ledger_has(context.intents, _verify_id(selection, round_), selection):
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
    if observation.status == ObservationStatus.PENDING:
        return _with_adoption(state, adoption.model_copy(update={"observation": observation}))
    if observation.status == ObservationStatus.SUCCEEDED and event.revision == selection.revision:
        done = Adoption(selection=selection, observation=observation, verified=True)
        return AreaChange[SettlementState](
            state=state.model_copy(update={"adoption": done}),
            events=(AdoptionResult(selection=selection, observation=observation),),
        )
    retryable = (
        _awaits_inspection(observation) and observation.status != ObservationStatus.SUCCEEDED
    )
    if retryable and round_ < context.run.limits.max_retries:
        return _with_adoption(
            state, Adoption(selection=selection), _adopt(context, selection, round_ + 1)
        )
    return _with_adoption(state, adoption.model_copy(update={"observation": observation}))


def advance(
    state: SettlementState, context: SettlementContext, event: SettlementEvent
) -> AreaChange[SettlementState]:
    """Consume only the two adoption events, preserving sibling-owned state fields."""
    if isinstance(event, WinnerProposed):
        return _propose(state, context, event)
    if isinstance(event, AdoptionObserved):
        return _observe(state, context, event)
    raise ContractValidationError("event.kind", "event does not belong to adoption")
