"""Pure resume selection for the plain issue-board policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_issue_board.api import IssueStatus

if TYPE_CHECKING:
    from vs_issue_board.api import IssueBoard
    from vs_loop_state.api import PlainLoopCursor


def resume_point(state: PlainLoopCursor, board: IssueBoard) -> tuple[int, str, int | None]:
    """Revisit an interrupted role only while its issue remains actionable."""
    issue_id = state.current_issue_id
    if issue_id is not None and state.phase in {"judge", "implementer"}:
        issue = board.get(issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, state.phase, issue_id
    return state.round_idx, "implementer", None
