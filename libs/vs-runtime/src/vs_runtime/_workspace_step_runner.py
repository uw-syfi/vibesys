"""A step runner that gives a step a workspace to return to.

A step that writes a workspace (an agent turn, a command run in it) must leave the tree as it
found it when an attempt dies, or the next attempt starts from the dead one's half-written
files. ``WorkspaceStepRunner`` wraps the runner that does the work. Before an attempt it records
the workspace as it stands, through the access settlement the session path already uses (the
snapshot is the commit recorded for the step, and it outlives the process). ``clear`` first
clears the inner runner, so nothing still writes, and then restores the workspace to the
commit recorded for the tag. The tag is the step's request id; each start of the step is its own
key (the tag and an ordinal), because a settled receipt does not judge a later start's writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Protocol

from vs_core.api import FailureReason, RejectionCode
from vs_core.api.recovery import Failed
from vs_runtime._access_settlement import AccessKey
from vs_runtime._step_runner import Cleared, ClearUnavailableError
from vs_runtime.contracts import RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_core.api import Request, RequestId, Scope, WorkspaceRef
    from vs_core.api.recovery import StepResult
    from vs_runtime._access_settlement import AccessReceipt, AccessSettlement
    from vs_runtime._step_runner import Adopted, CancelSignal, StepRunner
    from vs_runtime._workspace_access import AccessGrant, AccessGuardedWorkspace

_BINDING = "step"
_BACKEND = "the workspace"


@dataclass(frozen=True)
class WorkspaceTarget:
    """The workspace a step may write, and what its role may write there."""

    ref: WorkspaceRef | Scope
    workspace: AccessGuardedWorkspace
    grant: AccessGrant


class StepWorkspaces(Protocol):
    """Names the workspace a step writes, from the step itself."""

    async def target(self, step: Request) -> WorkspaceTarget | None:
        """The workspace *step* writes, or ``None`` for a step that writes none."""
        ...


def _record_key(tag: RequestId, ordinal: int) -> AccessKey:
    """The ``ordinal``-th recorded start of *tag*, numbered from 1 with no gaps."""
    return AccessKey(binding=_BINDING, invocation=f"{tag.root}/{ordinal}")


class WorkspaceStepRunner:
    """Runs a step on an inner runner, with its workspace recorded before and restored on clear."""

    def __init__(
        self, inner: StepRunner, workspaces: StepWorkspaces, settlement: AccessSettlement
    ) -> None:
        """Wrap *inner*; *workspaces* names each step's workspace and *settlement* records it."""
        self._inner = inner
        self._workspaces = workspaces
        self._settlement = settlement

    async def run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
        """Record the workspace, run the step on the inner runner, then judge what it wrote."""
        target = await self._workspaces.target(step)
        if target is None:
            return await self._inner.run(step, tag, attempt, cancel)
        key = self._unsettled(tag) or self._next_record(tag)
        await self._settlement.begin(key, target.ref, target.workspace, target.grant)
        try:
            result = await self._inner.run(step, tag, attempt, cancel)
        finally:
            self._settlement.fence(key)
        violation = await self._settlement.settle(key)
        if violation is not None:
            return Failed(reason=FailureReason.REFUSED, cause=RejectionCode.CAPABILITY)
        return result

    async def clear(self, tag: RequestId) -> Adopted | Cleared:
        """Clear the inner runner, then restore the workspace to the commit recorded for *tag*.

        Work the inner runner adopted is not restored under: it still runs in the workspace, or
        it ended and its result stands.
        """
        cleared = await self._inner.clear(tag)
        if isinstance(cleared, Cleared):
            try:
                await self._restore(tag)
            except RuntimeContractError as error:
                raise ClearUnavailableError(tag, _BACKEND) from error
        return cleared

    def _records(self, tag: RequestId) -> Iterator[tuple[AccessKey, AccessReceipt]]:
        """The recorded starts of *tag*, oldest first."""
        for ordinal in count(1):
            key = _record_key(tag, ordinal)
            receipt = self._settlement.receipt(key)
            if receipt is None:
                return
            yield key, receipt

    def _next_record(self, tag: RequestId) -> AccessKey:
        """The key of the start about to be recorded: one past the last."""
        return _record_key(tag, sum(1 for _ in self._records(tag)) + 1)

    def _unsettled(self, tag: RequestId) -> AccessKey | None:
        """The oldest recorded start of *tag* whose writes are not judged, if any.

        Only work the inner runner adopted leaves one: ``clear`` settles the rest. The next
        ``run`` attaches to that work in the same workspace, so it judges the writes against
        the commit recorded before the work began instead of taking a new one over them.
        """
        return next((key for key, receipt in self._records(tag) if not receipt.settled), None)

    async def _restore(self, tag: RequestId) -> None:
        """Restore, then settle, every recorded start of *tag*, oldest first.

        Each start began from a restored tree, so every baseline is the tree before the step
        touched anything. A start left unsettled would fence the workspace against the next
        baseline, and a settled one may hold writes whose result was never recorded, so all of
        them are restored; a restore already done is repeated without harm.
        """
        for key, _ in self._records(tag):
            await self._settlement.restore_interrupted(key)
