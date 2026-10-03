"""Policy functions for validating issue-board updates."""

from __future__ import annotations

from dataclasses import dataclass

from vs_issue_tracker.core import Issue, IssueTracker, IssueType


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


@dataclass(frozen=True)
class InvalidIssueType:
    """``create_issue`` named a type outside :class:`IssueType`."""

    given: str


@dataclass(frozen=True)
class TypeNotAllowed:
    """The creator may not file issues of ``type``; ``allowed`` lists what it may file."""

    creator: str
    type: IssueType
    allowed: tuple[IssueType, ...]


@dataclass(frozen=True)
class CapReached:
    """The creator already filed ``already`` open issues this iteration, at or over ``cap``."""

    already: int
    cap: int


CreateRejection = InvalidIssueType | TypeNotAllowed | CapReached
"""Why ``create_issue`` refused; callers render the agent-facing text."""


def check_create_allowed(
    store: IssueTracker,
    *,
    type_enum: IssueType,
    policy: CreateIssuePolicy,
) -> TypeNotAllowed | CapReached | None:
    """Return ``None`` if creation is allowed, else why it is not."""
    if type_enum not in policy.allowed_types:
        allowed = tuple(sorted(policy.allowed_types, key=lambda t: t.value))
        return TypeNotAllowed(creator=policy.creator, type=type_enum, allowed=allowed)
    if policy.cap is not None:
        already = store.open_count_by_creator_in_iter(policy.creator, policy.iteration)
        if already >= policy.cap:
            return CapReached(already=already, cap=policy.cap)
    return None


def create_issue_under_policy(
    store: IssueTracker,
    *,
    type_str: str,
    title: str,
    description: str,
    policy: CreateIssuePolicy,
) -> Issue | CreateRejection:
    """Parse, validate, and write a new issue if policy allows it.

    Returns the created issue, or the rejection when nothing was written.
    """
    try:
        type_enum = parse_type(type_str)
    except ValueError:
        return InvalidIssueType(given=type_str)
    rejection = check_create_allowed(store, type_enum=type_enum, policy=policy)
    if rejection is not None:
        return rejection
    return store.create(
        type=type_enum,
        title=title,
        description=description,
        created_by=policy.creator,
        iteration=policy.iteration,
    )
