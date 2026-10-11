"""Public issue board storage, policy, and display helpers.

``IssueTracker`` defines storage-neutral issue operations; ``IssueBoard`` is
its JSON-backed implementation. Use ``CreateIssuePolicy`` and its helpers when
creating issues under an iteration cap; a refusal is a typed ``CreateRejection``
that callers render. Format helpers produce short and full
text representations.
"""

from vs_issue_tracker.core import (
    Issue,
    IssueBoard,
    IssueBoardLoadError,
    IssueEvent,
    IssueStatus,
    IssueTracker,
    IssueType,
)
from vs_issue_tracker.errors import IssueTrackerLoadError
from vs_issue_tracker.fake import FakeIssueTracker, FaultHook, FaultPhase
from vs_issue_tracker.format import format_issue_full, format_issue_short
from vs_issue_tracker.github import GitHubIssueTracker, open_issue_tracker
from vs_issue_tracker.operations import (
    APPLY_KIND,
    LOG_KIND,
    READ_KIND,
    AppliedChanges,
    ApplyIssueChanges,
    BoardSnapshot,
    ChangeResult,
    CreateIssue,
    IssueBody,
    IssueChange,
    IssueRow,
    LoggedProgress,
    LogProgress,
    OutcomeStatus,
    ReadBoard,
    RecordAttempt,
    SetIssueStatus,
    TemplateBody,
    TextBody,
    board_operation_registrations,
)
from vs_issue_tracker.policy import (
    CapReached,
    CreateIssuePolicy,
    CreateRejection,
    InvalidIssueType,
    TypeNotAllowed,
    check_create_allowed,
    create_issue_under_policy,
    parse_type,
)
from vs_issue_tracker.progress import (
    FileProgressLog,
    GitHubProgressLog,
    MemoryProgressLog,
    ProgressLog,
    open_progress_log,
)
from vs_issue_tracker.session import (
    IssueTrackerConfig,
    IssueTrackerSession,
    open_issue_tracker_session,
)

__all__ = [
    "APPLY_KIND",
    "LOG_KIND",
    "READ_KIND",
    "AppliedChanges",
    "ApplyIssueChanges",
    "BoardSnapshot",
    "CapReached",
    "ChangeResult",
    "CreateIssue",
    "CreateIssuePolicy",
    "CreateRejection",
    "FakeIssueTracker",
    "FaultHook",
    "FaultPhase",
    "FileProgressLog",
    "GitHubIssueTracker",
    "GitHubProgressLog",
    "InvalidIssueType",
    "Issue",
    "IssueBoard",
    "IssueBoardLoadError",
    "IssueBody",
    "IssueChange",
    "IssueEvent",
    "IssueRow",
    "IssueStatus",
    "IssueTracker",
    "IssueTrackerConfig",
    "IssueTrackerLoadError",
    "IssueTrackerSession",
    "IssueType",
    "LogProgress",
    "LoggedProgress",
    "MemoryProgressLog",
    "OutcomeStatus",
    "ProgressLog",
    "ReadBoard",
    "RecordAttempt",
    "SetIssueStatus",
    "TemplateBody",
    "TextBody",
    "TypeNotAllowed",
    "board_operation_registrations",
    "check_create_allowed",
    "create_issue_under_policy",
    "format_issue_full",
    "format_issue_short",
    "open_issue_tracker",
    "open_issue_tracker_session",
    "open_progress_log",
    "parse_type",
]
