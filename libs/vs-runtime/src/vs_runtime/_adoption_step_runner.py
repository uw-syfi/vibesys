"""A step that adopts a selected revision: publish it, then fast-forward the operator's checkout.

Adoption leaves nothing to remove. ``Workspaces.adopt`` publishes idempotently, so publishing the
same revision again is the same commit, and ``update_checkout`` is a fast-forward or a typed
skip, so repeating it is a no-op. ``clear`` therefore returns ``Cleared`` once the run's
workspace answers, because a workspace that cannot be read leaves unproven whether an earlier
attempt is still publishing. The result of the step is what ``run`` returns, never a record
``clear`` adopts: every attempt reads the same tree and the same publication branch.

An adoption cancelled while it publishes ends its wait, not the publication: the commit the
thread may still write is the commit the next attempt writes. That is the one residual.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_core.api import (
    AdoptRevision,
    ContractError,
    ObservationStatus,
    RequestObserved,
    RevisionRef,
    SetupFailureKind,
    TrustedBaseline,
    VerifyAdoption,
)
from vs_core.api.recovery import Finished
from vs_runtime._adoption_commit import adoption_commit_message
from vs_runtime._cancel_wait import cancelled_for, finished_before_cancel
from vs_runtime._core_identity import core_identity
from vs_runtime._observation_factory import ObservationFacts, ObservationSubject
from vs_runtime._step_runner import Cleared, ClearUnavailableError
from vs_runtime._workspace_requests import commit_of
from vs_runtime.contracts import RevisionLedger, RuntimeContractError, Workspaces

if TYPE_CHECKING:
    from vs_core.api import AdoptionPublication, CheckoutUpdate, Request, RequestId, Selection
    from vs_core.api.recovery import StepResult
    from vs_runtime._evidence_ledger import EvidenceLookup
    from vs_runtime._observation_factory import ObservationFactory
    from vs_runtime._step_runner import Adopted, CancelSignal
    from vs_sim.api import Clock

_BACKEND = "the run's workspace"
_NOT_RETAINED = "selected revision is not a retained revision of this run"
_NOT_BASELINE = "selected baseline is not the run's trusted input baseline"
_PROTECTED = "candidate retained; adoption in a resumed epoch requires project-edit protection"


class AdoptionWorkspaces(Workspaces, RevisionLedger, Protocol):
    """The run's workspaces, which also say which revisions the run retains."""


@dataclass(frozen=True)
class _Refusal:
    """The selection is not a revision adoption may use."""

    reason: str


@dataclass(frozen=True)
class _Adopted:
    """What one adoption did: its facts, where it published, and what it did to the checkout."""

    facts: ObservationFacts
    publication: AdoptionPublication | None = None
    checkout: CheckoutUpdate | None = None


class AdoptionStepRunner:
    """Runs ``AdoptRevision`` and ``VerifyAdoption`` steps on the run's workspaces."""

    def __init__(
        self,
        workspaces: AdoptionWorkspaces,
        evidence: EvidenceLookup,
        observations: ObservationFactory,
        *,
        clock: Clock,
        publishes: bool = True,
    ) -> None:
        """Adopt on *workspaces*; a revision is the run's when they retain it.

        With ``publishes`` false (a run an operator resumed) the operator's tree is kept: a step
        succeeds when the tree already matches the selection, and is refused or fails otherwise.
        """
        self._workspaces = workspaces
        self._evidence = evidence
        self._observations = observations
        self._clock = clock
        self._publishes = publishes

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Adopt the selected revision, or return ``Cancelled`` once the signal is set."""
        del (
            attempt,
            deadline_at,
        )  # the same selection reads the same tree; the loop cancels at the deadline
        request = _adoption(step, tag)
        work = asyncio.ensure_future(self._adopt(request))
        if not await finished_before_cancel(work, cancel):
            return cancelled_for(cancel)
        return self._finished(request, work.result())

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """Nothing of an earlier attempt needs removing; say so once the workspace answers."""
        commit = commit_of(_adoption(step, tag).selection.revision)
        if commit is None:
            return Cleared()
        try:
            await self._workspaces.root.has_revision(commit)
        except (OSError, RuntimeContractError) as error:
            raise ClearUnavailableError(tag, _BACKEND) from error
        return Cleared()

    async def _adopt(self, request: AdoptRevision | VerifyAdoption) -> _Adopted:
        selection = request.selection
        target = await self._target(selection)
        if isinstance(target, _Refusal):
            return _Adopted(ObservationFacts.refused(target.reason))
        if not self._publishes:
            return _Adopted(await self._keep_tree(request, target))
        return await self._publish(request, target)

    async def _target(self, selection: Selection) -> str | _Refusal:
        """The commit adoption may use, or the reason the selection is not one."""
        root = self._workspaces.root
        commit = commit_of(selection.revision)
        if commit is None or not await root.has_revision(commit):
            return _Refusal(_NOT_RETAINED)
        if isinstance(selection, TrustedBaseline):
            return commit if commit == root.trusted_input_baseline else _Refusal(_NOT_BASELINE)
        known = commit in {root.revision, root.trusted_input_baseline}
        return (
            commit if known or await self._workspaces.retains(commit) else _Refusal(_NOT_RETAINED)
        )

    async def _keep_tree(
        self, request: AdoptRevision | VerifyAdoption, commit: str
    ) -> ObservationFacts:
        if await self._workspaces.root.matches_revision(commit):
            return _succeeded(request.selection.revision, "")
        if isinstance(request, AdoptRevision):
            return ObservationFacts.refused(_PROTECTED)
        return ObservationFacts(
            status=ObservationStatus.FAILED,
            accepted=True,
            diagnostic="root workspace content differs from the selected revision",
        )

    async def _publish(self, request: AdoptRevision | VerifyAdoption, commit: str) -> _Adopted:
        ref = request.selection.revision
        message = (
            adoption_commit_message(request, commit, self._evidence)
            if isinstance(request, AdoptRevision)
            else "vibesys: adopt retained revision"
        )
        try:
            publication = await self._workspaces.adopt(commit, message=message)
        except RuntimeContractError as error:
            return _Adopted(
                ObservationFacts(
                    status=ObservationStatus.FAILED, accepted=True, diagnostic=str(error)
                )
            )
        if isinstance(request, VerifyAdoption):
            return _Adopted(_succeeded(ref, "published"), publication)
        checkout = await self._workspaces.update_checkout(publication)
        facts = _succeeded(ref, f"checkout {checkout.status.value.replace('_', ' ')}")
        return _Adopted(facts, publication, checkout)

    def _finished(self, request: AdoptRevision | VerifyAdoption, adopted: _Adopted) -> Finished:
        facts = adopted.facts
        observation = self._observations.observe(
            ObservationSubject.of(request), facts, observed_at=self._clock.now()
        )
        refused = observation.status is ObservationStatus.REJECTED
        reported = RequestObserved(
            observation=observation,
            setup_failure=SetupFailureKind.PERMANENT if refused else SetupFailureKind.UNKNOWN,
            revision=facts.revision,
            publication=adopted.publication,
            checkout=adopted.checkout,
        )
        return Finished(observation=observation, observed=reported)


def _succeeded(revision: RevisionRef, diagnostic: str) -> ObservationFacts:
    return ObservationFacts(
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        revision=revision,
        diagnostic=diagnostic,
    )


def _adoption(step: Request, tag: RequestId) -> AdoptRevision | VerifyAdoption:
    if not isinstance(step, AdoptRevision | VerifyAdoption):
        kind = type(step).__name__
        raise ContractError(("step", str(tag)), f"not an adoption: {kind}")
    if core_identity(step) != tag:
        raise ContractError(("step", str(tag)), "the tag is not the step's request id")
    return step
