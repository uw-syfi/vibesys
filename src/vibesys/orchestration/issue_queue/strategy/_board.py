"""Pure reading of the board snapshot: which issue is next, what is left, whether it is quiet.

These are the decisions the legacy loop made by querying the tracker; here they are functions
of the last snapshot, so the stop decision of a run is a function of the board it saw.
"""

from vibesys.orchestration.issue_queue.strategy._prompts import IssueBrief, IssueDetail
from vs_issue_tracker.api import IssueRow, IssueStatus, IssueType

_TYPE_RANK = {IssueType.BUG: 0, IssueType.FEATURE: 1, IssueType.PERF: 2}


def next_open(issues: tuple[IssueRow, ...]) -> IssueRow | None:
    """The open issue with the earliest type priority, then the earliest creation."""
    candidates = [issue for issue in issues if issue.status is IssueStatus.OPEN]
    if not candidates:
        return None
    return min(candidates, key=lambda issue: (_TYPE_RANK[issue.type], issue.id))


def remaining(issues: tuple[IssueRow, ...]) -> tuple[IssueRow, ...]:
    """Every issue that is not closed."""
    return tuple(issue for issue in issues if issue.status is not IssueStatus.CLOSED)


def blocked_queue(issues: tuple[IssueRow, ...]) -> bool:
    """Whether work remains and every remaining issue is blocked, so nothing can proceed."""
    left = remaining(issues)
    return bool(left) and all(issue.status is IssueStatus.BLOCKED for issue in left)


def has_open(issues: tuple[IssueRow, ...]) -> bool:
    """Whether any issue is open."""
    return any(issue.status is IssueStatus.OPEN for issue in issues)


def brief(issue: IssueRow) -> IssueBrief:
    """One issue as a line of the board list."""
    return IssueBrief(
        id=issue.id,
        type=issue.type.value,
        status=issue.status.value,
        title=issue.title,
        attempts=issue.attempts,
    )


def briefs(issues: tuple[IssueRow, ...]) -> tuple[IssueBrief, ...]:
    """The whole board as a list."""
    return tuple(brief(issue) for issue in issues)


def detail(issue: IssueRow) -> IssueDetail:
    """The issue under work, in full."""
    return IssueDetail(**brief(issue).model_dump(), description=issue.description)
