"""Which candidate workspaces core still references, read from its committed state.

A candidate worktree outlives a host only while core may still ask for it. Core asks for an
attempt generation's workspace while the attempt has not ended, and while any request scoped
to it has not completed (a discard, a cancel's access settlement, a snapshot). Once the run
has a final result core asks for nothing, whatever it left open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_core.api import AttemptId, AttemptPhase, AttemptRef, RequestPhase, RunStatus
from vs_runtime._workspace_lookup import candidate_member_id

if TYPE_CHECKING:
    from vs_core.api import CoreState


@dataclass(frozen=True, slots=True)
class WorkspaceHolds:
    """Core's references to candidate workspaces at one moment.

    ``final`` is true once the run's result is final: no request will ever come, so no
    workspace is held. Otherwise ``members`` names the candidate members (attempt
    generations) whose workspaces a resumed run may still ask for.
    """

    final: bool
    members: frozenset[str]

    def holds(self, member_id: str) -> bool:
        """Whether core may still ask for this member's workspace."""
        return not self.final and member_id in self.members


def core_workspace_holds(core: CoreState) -> WorkspaceHolds:
    """The candidate workspaces *core* references, from its committed state."""
    if core.run.status == RunStatus.TERMINAL:
        return WorkspaceHolds(final=True, members=frozenset())
    open_attempts = (
        AttemptRef(attempt_id=view.attempt_id, generation=view.generation)
        for view in core.attempts.attempts
        if view.phase != AttemptPhase.TERMINAL
    )
    pending_scopes = (
        AttemptRef(attempt_id=record.request.scope.owner, generation=record.request.scope.generation)
        for record in core.records.records
        if record.phase != RequestPhase.COMPLETED
        and isinstance(record.request.scope.owner, AttemptId)
    )
    return WorkspaceHolds(
        final=False,
        members=frozenset(
            candidate_member_id(attempt) for attempt in (*open_attempts, *pending_scopes)
        ),
    )


__all__ = ["WorkspaceHolds", "core_workspace_holds"]
