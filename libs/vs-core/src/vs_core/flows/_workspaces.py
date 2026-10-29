"""``ctx.workspaces``: open a workspace as a resource, keep a candidate, verify a revision.

A workspace is opened with ``async with ctx.workspaces.open(...) as ws`` and is always settled
on the way out, however the body ended: after normal work, after an exception of the body and
after an operator stop. What the settlement asserts is what the body recorded with ``keep``;
a body that kept nothing settles plainly (discarded). If core will not take the assertion,
the plain settlement is proposed once, because a close nobody takes would hold the slot for
ever.

A workspace survives a restart after a code change when the flow that holds it checkpointed
while holding it: the restarted flow takes it back with ``ctx.workspaces.reopen``. What the
restart cannot give back settles as the flow last said with ``salvage`` (see ``_run``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter

from vs_core._registry import ContractError
from vs_core._strategy_operations import ParentVerification, PatchExport
from vs_core.types.common import (
    THIS_RUN,
    AttemptId,
    AttemptRef,
    DecisionId,
    ItemId,
    RevisionRef,
    Scope,
    Value,
    WorkspaceMode,
)
from vs_core.types.search import WorkspaceClosed, WorkspaceOpened
from vs_core.types.settlement import AssessmentProposal, Settlement
from vs_core.types.strategy import Operation, Settle, StartAttempt, Withdraw
from vs_core.types.until import Failed, Within

from ._errors import Stopped
from ._resource import Resource
from ._state import Held
from ._winner import Kept

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from vs_core.types.attempts import AttemptBudget, WorkspacePlan
    from vs_core.types.common import Seconds

    from ._kit import Kit
    from ._labels import Label

type Outcome = Literal["succeeded", "failed", "cancelled", "blocked"]

_SETTLEMENTS = TypeAdapter(tuple[Settlement, ...])


class NotOpened(Value):
    """The workspace could not be opened: the slot was never used. ``failed`` says why."""

    failed: Failed


class NotReopened(Value):
    """The restart did not keep ``attempt`` open for this flow; it settled it as salvaged.

    That happens when the flow opened the workspace after its latest checkpoint, or when the
    workspace is not one the flow held. Its settlement is recorded in the state's
    ``restart_settled``.
    """

    attempt: AttemptRef


class Assessment(Value):
    """What a body asserts about the candidate it keeps: how it ended and how it was judged."""

    outcome: Outcome
    assessments: tuple[AssessmentProposal, ...] = ()
    eligible: bool = False
    retention: Literal["discard", "wip", "candidate"] = "candidate"


@dataclass
class Workspace:
    """An opened workspace: where its work runs, and how it settled (after the ``async with``)."""

    ref: AttemptRef
    base: RevisionRef
    mode: WorkspaceMode = WorkspaceMode.ISOLATED_CHILD
    """How core admitted the workspace; a writing role works in it in this mode."""
    keeping: Settle | None = None
    """What the settlement will assert: set by ``ctx.workspaces.keep``."""
    settlement: Settlement | None = None
    """Core's settlement of the workspace; None until it closes, or if core took none."""
    refused: Failed | None = None
    """Why core refused what ``keep`` asserted, when it did: the workspace then settled plainly."""
    settling: Settle | None = None
    """The settlement asked for before a restart, still in flight when the flow reopened the
    workspace: the workspace only closes now, and its close takes that settlement."""

    @property
    def scope(self) -> Scope:
        """The scope the workspace's own work runs in."""
        return Scope(owner=self.ref.attempt_id, generation=self.ref.generation)

    @property
    def kept(self) -> Kept | None:
        """The candidate the workspace retained, once it closed and retained one."""
        return None if self.settlement is None else Kept.of(self.settlement)


def _settle_of(revision: RevisionRef | None, assessment: Assessment) -> Settle:
    return Settle(
        assessments=assessment.assessments,
        eligible=assessment.eligible,
        retention=assessment.retention,
        outcome=assessment.outcome,
        candidate=revision,
    )


def _plain(outcome: Outcome) -> Settle:
    return Settle(
        assessments=(), eligible=False, retention="discard", outcome=outcome, candidate=None
    )


class Workspaces:
    """The workspace aspect of ``ctx``."""

    def __init__(self, kit: Kit) -> None:
        """Bind the aspect to its flow."""
        self._kit = kit

    def open(
        self,
        *,
        attempt: AttemptId,
        item: ItemId,
        plan: WorkspacePlan,
        budget: AttemptBudget,
        within: Within | None = None,
    ) -> Resource[Workspace | NotOpened]:
        """A workspace, as a resource: ``NotOpened`` if it cannot be, else always settled.

        ``within`` bounds the wait for the workspace to open (the run's remaining time when
        omitted).
        """
        request = StartAttempt(
            decision_id=DecisionId(root="start"),
            scope=THIS_RUN,
            attempt_id=attempt,
            item_id=item,
            workspace=plan,
            budget=budget,
        )
        return Resource(self._kit, _workspace(self._kit, request, within))

    def reopen(self, attempt: AttemptRef) -> Resource[Workspace | NotReopened]:
        """A workspace a restart kept open for this flow, as a resource, settled like ``open``'s.

        A flow resumed from a checkpoint it recorded while it held ``attempt`` gets the same
        workspace back, with nothing issued; otherwise ``NotReopened``.
        """
        return Resource(self._kit, _reopened(self._kit, attempt))

    def salvage(
        self, workspace: Workspace, revision: RevisionRef | None, assessment: Assessment
    ) -> None:
        """Record how ``workspace`` settles if a restart cannot give it back to this flow.

        A restart after a code change gives a held workspace back to the flow only when the flow
        resumes from a checkpoint it recorded while holding it, and the flow reopens it. If not
        (the new code does not start the flow again, or the flow ends without reopening it), the
        restart settles it with this assertion, so work the flow wants kept (a candidate's code,
        as work in progress) is not discarded. Without a salvage it settles as cancelled and
        discarded. The salvage does not change how the workspace settles when the flow closes it.
        """
        disposition = _settle_of(revision, assessment)
        task = self._kit.task
        task.holds = [
            held.model_copy(update={"salvage": disposition})
            if held.attempt == workspace.ref
            else held
            for held in task.holds
        ]

    def keep(
        self, workspace: Workspace, revision: RevisionRef | None, assessment: Assessment
    ) -> None:
        """Record that the workspace settles with ``assessment`` about ``revision``.

        ``revision`` is None when the work made no revision: the settlement then asserts only
        how the workspace ended (a retained settlement needs a revision, and core refuses one
        without).
        """
        workspace.keeping = _settle_of(revision, assessment)

    async def verify(
        self, revision: RevisionRef, *, within: Seconds, label: Label | None = None
    ) -> ParentVerification | Failed:
        """Whether ``revision`` still reproduces its retained, evaluated content.

        ``label`` labels the verification's span.
        """
        operations = self._kit.run.wiring.operations
        if operations is None:
            message = "this search was registered without operations"
            raise ContractError(("workspaces", "verify"), message)
        request = Operation(
            decision_id=DecisionId(root="verify"),
            scope=THIS_RUN,
            request=operations.verify(parent=revision),  # ty: ignore[missing-argument]
        )
        event = await self._kit.wait(request, Within(limit=within), label)
        if isinstance(event, Failed):
            return event
        outcome = getattr(getattr(event, "result", None), "outcome", None)
        if not isinstance(outcome, ParentVerification):
            message = "the verification returned something that is not a ParentVerification"
            raise ContractError(("workspaces", "verify"), message)
        return outcome

    async def export_patch(self, revision: RevisionRef, *, within: Seconds) -> PatchExport | Failed:
        """The canonical patch of retained ``revision`` against the input, for a search that ranks by code.

        The search must declare the export in its operations.
        """
        operations = self._kit.run.wiring.operations
        if operations is None or operations.export_patch is None:
            message = "this search was registered without an export_patch operation"
            raise ContractError(("workspaces", "export_patch"), message)
        request = Operation(
            decision_id=DecisionId(root="export-patch"),
            scope=THIS_RUN,
            request=operations.export_patch(revision=revision),  # ty: ignore[missing-argument]
        )
        event = await self._kit.wait(request, Within(limit=within))
        if isinstance(event, Failed):
            return event
        outcome = getattr(getattr(event, "result", None), "outcome", None)
        if not isinstance(outcome, PatchExport):
            message = "the export returned something that is not a PatchExport"
            raise ContractError(("workspaces", "export_patch"), message)
        return outcome

    def kept(self) -> tuple[Kept, ...]:
        """Every candidate the run's closed workspaces retained, oldest first."""
        settlements = self._kit.read(lambda view: view.settlements, _SETTLEMENTS)
        return tuple(kept for item in settlements if (kept := Kept.of(item)) is not None)


def _workspace(
    kit: Kit, request: StartAttempt, within: Within | None
) -> AsyncGenerator[Workspace | NotOpened]:
    async def acquire() -> Workspace | NotOpened:
        event = await kit.wait(request, within or kit.until_deadline())
        match event:
            case WorkspaceOpened(attempt=opened):
                plan = request.workspace
                kit.task.holds.append(Held(attempt=opened, base=plan.base, mode=plan.mode))
                return Workspace(ref=opened, base=plan.base, mode=plan.mode)
            case Failed():
                return NotOpened(failed=event)
            case unexpected:
                message = f"a workspace opened with {unexpected.kind}"
                raise ContractError(("workspaces", "open"), message)

    return _holding(kit, acquire)


def _reopened(kit: Kit, attempt: AttemptRef) -> AsyncGenerator[Workspace | NotReopened]:
    async def acquire() -> Workspace | NotReopened:
        task = kit.task
        held = next((item for item in task.carried if item.attempt == attempt), None)
        if held is None or held.base is None or held.mode is None:
            return NotReopened(attempt=attempt)
        task.carried.remove(held)
        task.holds.append(held)  # carried: the snapshot the flow resumed from names it
        return Workspace(
            ref=held.attempt,
            base=held.base,
            mode=held.mode,
            keeping=held.settling,
            settling=held.settling,
        )

    return _holding(kit, acquire)


async def _holding[N](
    kit: Kit, acquire: Callable[[], Awaitable[Workspace | N]]
) -> AsyncGenerator[Workspace | N]:
    """Acquire a workspace and yield it to the body; settle it however the body ended."""
    workspace = await acquire()
    if not isinstance(workspace, Workspace):
        yield workspace  # it did not open: there is nothing to settle
        return
    try:
        yield workspace
    except Stopped:
        await _settle(kit, workspace, workspace.keeping or _plain("cancelled"))
        raise
    except GeneratorExit:
        # The flow is being discarded (its run restarted elsewhere): nothing can serve a
        # settlement now, and the restarted run settles the workspaces its flow no longer holds.
        raise
    except BaseException:
        await _settle(kit, workspace, workspace.keeping or _plain("failed"))
        raise
    await _settle(kit, workspace, workspace.keeping or _plain("succeeded"))


async def _settle(kit: Kit, workspace: Workspace, disposition: Settle) -> None:
    """Close the workspace, asserting ``disposition``; plainly, once, if core refuses that.

    The hold records the settlement asked for while it is in flight: a restart then leaves the
    workspace to it rather than settling it again, and a resumed flow that reopens and closes the
    workspace waits for that same settlement (the run re-attaches the close to it).
    """
    settled = await _withdraw(kit, workspace.ref, disposition)
    if isinstance(settled, Failed):
        workspace.refused = settled
        settled = await _withdraw(kit, workspace.ref, _plain("failed"))
    workspace.settlement = None if isinstance(settled, Failed) else settled
    kit.task.holds = [held for held in kit.task.holds if held.attempt != workspace.ref]


async def _withdraw(kit: Kit, target: AttemptRef, disposition: Settle) -> Settlement | Failed:
    task = kit.task
    task.holds = [
        held.model_copy(update={"settling": disposition}) if held.attempt == target else held
        for held in task.holds
    ]
    request = Withdraw(
        decision_id=DecisionId(root="settle"),
        scope=THIS_RUN,
        target=target,
        disposition=disposition,
    )
    match await kit.wait(request, kit.cleanup_within()):
        case WorkspaceClosed(settlement=settlement):
            return settlement
        case Failed() as refused:
            return refused
        case unexpected:
            message = f"a workspace closed with {unexpected.kind}"
            raise ContractError(("workspaces", "close"), message)
