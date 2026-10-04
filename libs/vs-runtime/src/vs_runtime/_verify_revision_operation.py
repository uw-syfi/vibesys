"""Owner of parent-revision verification, over the public ``Workspaces`` interface.

A parent offered to a child must still be retained by this run's workspace.
``Workspaces.retains`` answers that from the run's revision ledger. Exporting a
patch does not: it succeeds for a dangling commit that nothing references, so it
only supplies the content digest after retention is proven. The interface has no
typed unknown-revision error, so ValueError and RuntimeContractError from the
export mean "not retained"; any other error is an infrastructure failure and
propagates. Verification is a query: it changes nothing, so inspection answers by
verifying again.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol, cast

from vs_runtime._operation_catalog import Applied, Inspection
from vs_runtime.contracts import RuntimeContractError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_core.api import OperationRequest, RevisionRef
    from vs_runtime._core_requests import ExecutionContext
    from vs_runtime.contracts import Workspaces


class VerifyRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    parent: RevisionRef


class VerifyRevisionOwner:
    """Verify that a parent revision is retained by the run and exports as a patch."""

    def __init__(
        self, workspaces: Workspaces, commit_of: Callable[[RevisionRef], str | None]
    ) -> None:
        """Bind the workspaces and the owner of the revision reference encoding.

        ``commit_of`` returns the workspace revision a reference names, or None
        when the reference is not canonical (including a digest that disagrees).
        """
        self._workspaces = workspaces
        self._commit_of = commit_of

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """Report verified only when the revision is retained; infrastructure errors raise."""
        del context
        return await self._verify(request)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """A query has no effect to prove, so inspection answers by verifying again."""
        del context
        return Applied(await self._verify(request))

    async def _verify(self, request: OperationRequest) -> Mapping[str, object]:
        parent = cast("VerifyRequest", request).parent
        commit = self._commit_of(parent)
        if commit is None:
            return _unverified("revision reference is not canonical")
        if not await self._workspaces.retains(commit):
            return _unverified("revision is not retained by the run workspace")
        try:
            patch = await self._workspaces.export_patch(commit)
        except (ValueError, RuntimeContractError):
            return _unverified("revision is not retained by the run workspace")
        digest = hashlib.sha256(patch.encode()).hexdigest()
        return {"status": "succeeded", "verified": True, "detail": f"patch sha256 {digest}"}


def _unverified(detail: str) -> Mapping[str, object]:
    return {"status": "succeeded", "verified": False, "detail": detail}
