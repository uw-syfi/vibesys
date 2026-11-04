"""Public issue board storage, policy, and display helpers.

``IssueTracker`` defines storage-neutral issue operations; ``IssueBoard`` is
its JSON-backed implementation. Use ``CreateIssuePolicy`` and its helpers when
creating issues under an iteration cap; a refusal is a typed ``CreateRejection``
that callers render. Format helpers produce short and full
text representations.

``Roadmap`` defines a search's planning items (Majors and the Minors that block them)
independently of storage; ``DocumentRoadmap`` keeps them as one document in a store the
application opens, ``FakeRoadmap`` in memory. ``ReadRoadmap`` and ``PutRoadmapItems`` are the
core operations a strategy reaches it through.
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
from vs_issue_tracker.roadmap import (
    NOTE_MAX_LENGTH,
    ROADMAP_DOCUMENT,
    ROADMAP_ID_PATTERN,
    TITLE_MAX_LENGTH,
    DocumentRoadmap,
    FakeRoadmap,
    Roadmap,
    RoadmapDocuments,
    RoadmapItem,
    RoadmapItemKind,
    RoadmapLoadError,
    RoadmapRejectedError,
    RoadmapStatus,
    merged,
    roadmap_problem,
)
from vs_issue_tracker.roadmap_operations import (
    ROADMAP_PUT_KIND,
    ROADMAP_READ_KIND,
    PutRoadmapItems,
    ReadRoadmap,
    RoadmapItemsPut,
    RoadmapSnapshot,
    roadmap_operation_registrations,
)
from vs_issue_tracker.session import (
    IssueTrackerConfig,
    IssueTrackerSession,
    open_issue_tracker_session,
)

__all__ = [
    "APPLY_KIND",
    "LOG_KIND",
    "NOTE_MAX_LENGTH",
    "READ_KIND",
    "ROADMAP_DOCUMENT",
    "ROADMAP_ID_PATTERN",
    "ROADMAP_PUT_KIND",
    "ROADMAP_READ_KIND",
    "TITLE_MAX_LENGTH",
    "AppliedChanges",
    "ApplyIssueChanges",
    "BoardSnapshot",
    "CapReached",
    "ChangeResult",
    "CreateIssue",
    "CreateIssuePolicy",
    "CreateRejection",
    "DocumentRoadmap",
    "FakeIssueTracker",
    "FakeRoadmap",
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
    "PutRoadmapItems",
    "ReadBoard",
    "ReadRoadmap",
    "RecordAttempt",
    "Roadmap",
    "RoadmapDocuments",
    "RoadmapItem",
    "RoadmapItemKind",
    "RoadmapItemsPut",
    "RoadmapLoadError",
    "RoadmapRejectedError",
    "RoadmapSnapshot",
    "RoadmapStatus",
    "SetIssueStatus",
    "TemplateBody",
    "TextBody",
    "TypeNotAllowed",
    "board_operation_registrations",
    "check_create_allowed",
    "create_issue_under_policy",
    "format_issue_full",
    "format_issue_short",
    "merged",
    "open_issue_tracker",
    "open_issue_tracker_session",
    "open_progress_log",
    "parse_type",
    "roadmap_operation_registrations",
    "roadmap_problem",
]
