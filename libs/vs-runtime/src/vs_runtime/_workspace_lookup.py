"""Find the live workspace an attempt or the run owns, shared by every executor that needs one."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from vs_core.api import AttemptRef, RunId, Scope, WorkspaceRef
from vs_runtime._workspace_receipts import attempt_key

if TYPE_CHECKING:
    from vs_runtime._workspace_receipts import AttemptBinding, WorkspaceReceipts
    from vs_runtime._workspaces import RuntimeWorkspace, RuntimeWorkspaces


def candidate_member_id(attempt: AttemptRef) -> str:
    """Stable member identity: one candidate path per attempt generation."""
    return "attempt-" + hashlib.sha256(attempt_key(attempt).encode()).hexdigest()[:24]


async def find_attempt_workspace(
    workspaces: RuntimeWorkspaces,
    receipts: WorkspaceReceipts,
    attempt: AttemptRef,
    binding: AttemptBinding,
) -> RuntimeWorkspace | str:
    """The attempt's live workspace, reopened from disk after a restart; else why it is gone."""
    if binding.workspace_id is None:
        if receipts.root_holder() != attempt:
            return "the exclusive root is held by another attempt generation"
        return workspaces.root
    member = candidate_member_id(attempt)
    live = workspaces.live_candidate(member) or await workspaces.reattach_candidate(member)
    if live is None:
        return "the attempt's workspace no longer exists"
    return live


async def find_scope_workspace(
    workspaces: RuntimeWorkspaces, receipts: WorkspaceReceipts, ref: WorkspaceRef | Scope
) -> RuntimeWorkspace | None:
    """The workspace a core scope names: the run's root, or an attempt's bound workspace."""
    scope = ref.scope if isinstance(ref, WorkspaceRef) else ref
    owner = scope.owner
    if isinstance(owner, RunId):
        return workspaces.root
    attempt = AttemptRef(attempt_id=owner, generation=scope.generation)
    binding = receipts.load_binding(attempt)
    if binding is None:
        return None
    found = await find_attempt_workspace(workspaces, receipts, attempt, binding)
    return None if isinstance(found, str) else found
