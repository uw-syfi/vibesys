from __future__ import annotations

from dataclasses import dataclass

from vs_issue_board.core import Issue, IssueBoard, IssueType


@dataclass(frozen=True)
class CreateIssuePolicy:
    """Per-call policy for ``create_issue``.

    Captures who is creating the issue, which iteration the cap is scoped to,
    the cap itself (``None`` = unlimited), and which issue types the creator
    may file.
    """

    creator: str
    iteration: int
    cap: int | None
    allowed_types: frozenset[IssueType]


def parse_type(value: str) -> IssueType:
    """Coerce a free-form string to an :class:`IssueType`."""
    return IssueType(value)


def check_create_allowed(
    store: IssueBoard,
    *,
    type_enum: IssueType,
    policy: CreateIssuePolicy,
) -> str | None:
    """Return ``None`` if creation is allowed, else a human-readable error."""
    if type_enum not in policy.allowed_types:
        allowed = sorted(t.value for t in policy.allowed_types)
        return (
            f"error: as '{policy.creator}' you may only file types {allowed}, "
            f"not '{type_enum.value}'"
        )
    if policy.cap is not None:
        store.reload()
        already = store.open_count_by_creator_in_iter(policy.creator, policy.iteration)
        if already >= policy.cap:
            return (
                f"error: per-iteration cap reached "
                f"({already}/{policy.cap}). Cannot create more issues "
                f"this iteration. Use search_issues to dedupe before "
                f"creating, and prioritize the most impactful issues."
            )
    return None


def create_issue_under_policy(
    store: IssueBoard,
    *,
    type_str: str,
    title: str,
    description: str,
    policy: CreateIssuePolicy,
) -> tuple[Issue | None, str]:
    """Parse, validate, and write a new issue if policy allows it."""
    try:
        type_enum = parse_type(type_str)
    except ValueError:
        return None, (
            f"error: type must be one of {[t.value for t in IssueType]}, got '{type_str}'"
        )
    err = check_create_allowed(store, type_enum=type_enum, policy=policy)
    if err is not None:
        return None, err
    issue = store.create(
        type=type_enum,
        title=title,
        description=description,
        created_by=policy.creator,
        iteration=policy.iteration,
    )
    return issue, f"created issue #{issue.id}"
