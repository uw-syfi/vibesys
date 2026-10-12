"""Owner of patch export, over the public ``Workspaces`` interface.

A population policy that ranks candidates by their code needs the patch a retained revision
differs from the run's trusted baseline by. The export reads one retained revision and changes
nothing, so the operation is a query: inspection proves nothing happened and the request is
executed again. A revision the run does not retain is refused as an outcome, not an error; an
export that cannot be produced at all propagates as an infrastructure failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from vs_runtime._operation_catalog import Inspection, NotApplied

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_core.api import OperationRequest, RevisionRef
    from vs_runtime._core_requests import ExecutionContext
    from vs_runtime.contracts import RevisionLedger, Workspaces


class ExportRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    revision: RevisionRef


class ExportPatchOwner:
    """Export the canonical patch of a retained revision."""

    def __init__(
        self,
        workspaces: Workspaces,
        ledger: RevisionLedger,
        commit_of: Callable[[RevisionRef], str | None],
    ) -> None:
        """Bind the workspaces, the retention ledger and the revision encoding."""
        self._workspaces = workspaces
        self._ledger = ledger
        self._commit_of = commit_of

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """Export the revision's patch; infrastructure errors raise."""
        del context
        asked = cast("ExportRequest", request)
        commit = self._commit_of(asked.revision)
        if commit is None:
            return {"status": "rejected", "detail": "revision reference is not canonical"}
        if not await self._ledger.retains(commit):
            return {
                "status": "rejected",
                "detail": "revision is not retained by the run workspace",
            }
        return {"status": "succeeded", "patch": await self._workspaces.export_patch(commit)}

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Inspection never exports: a query leaves no effect to prove."""
        del request, context
        return NotApplied()
