"""Shared implementation behind the issue-tracker tool wrappers.

Both the LangChain ``@tool`` callables in :mod:`vibeserve_agent.loops.plain.tools`
and the FastMCP ``@mcp.tool()`` callables in
:mod:`vibeserve_agent.loops.plain.mcp_server` delegate into the helpers in this
module so that the deepagents path and the MCP path enforce identical
per-iteration cap and type-allowlist semantics with byte-identical error
strings and formatted output.

This module is pure-Python and only depends on
:mod:`vibeserve_agent.loops.plain.issue_board` (which itself only imports ``pydantic``).
It is safe to import from any subprocess that does not have LangChain or the
deepagents stack installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from vibeserve_agent.loops.plain.issue_board import (
    Issue,
    IssueBoard,
    IssueType,
)


@dataclass(frozen=True)
class CreateIssuePolicy:
    """Per-call policy for ``create_issue``.

    Captures the four parameters that ``build_issue_tools`` previously closed
    over: who is creating the issue, which iteration the cap is scoped to,
    the cap itself (``None`` = unlimited), and which issue types the creator
    may file.
    """

    creator: str
    iteration: int
    cap: int | None
    allowed_types: frozenset[IssueType]


def parse_type(value: str) -> IssueType:
    """Coerce a free-form string to an :class:`IssueType`.

    Raises :class:`ValueError` on a miss; callers should catch and format
    the error using the same wording as ``check_create_allowed``.
    """
    return IssueType(value)


def check_create_allowed(
    store: IssueBoard,
    *,
    type_enum: IssueType,
    policy: CreateIssuePolicy,
) -> str | None:
    """Return ``None`` if creation is allowed, else a human-readable error.

    Mirrors the in-tool error messages from the original ``build_issue_tools``
    so MCP clients see exactly the same wording the deepagents path produces.
    The cap is re-derived from the persisted store on every call, never from
    a closure-mutable counter — this keeps resume-after-crash trivial.
    """
    if type_enum not in policy.allowed_types:
        allowed = sorted(t.value for t in policy.allowed_types)
        return (
            f"error: as '{policy.creator}' you may only file types {allowed}, "
            f"not '{type_enum.value}'"
        )
    if policy.cap is not None:
        store.reload()
        already = store.open_count_by_creator_in_iter(
            policy.creator, policy.iteration
        )
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
    """Parse, validate, and (if allowed) write a new issue.

    Returns ``(issue_or_None, message)`` where ``message`` is the success or
    error string the caller should propagate to the agent. On the happy path
    the message is ``"created issue #N"``; on policy rejection it is the
    error string from :func:`check_create_allowed` or :func:`parse_type`.
    """
    try:
        type_enum = parse_type(type_str)
    except ValueError:
        return None, (
            f"error: type must be one of {[t.value for t in IssueType]}, "
            f"got '{type_str}'"
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


def format_issue_short(issue: Issue) -> str:
    """One-line summary used by ``list_issues`` / ``search_issues``."""
    return f"#{issue.id} [{issue.type.value}] [{issue.status.value}] {issue.title}"


def format_issue_full(issue: Issue) -> str:
    """Full markdown body used by ``get_issue``."""
    return (
        f"## Issue #{issue.id}\n"
        f"- type: {issue.type.value}\n"
        f"- status: {issue.status.value}\n"
        f"- created_by: {issue.created_by} (iter {issue.created_iter})\n"
        f"- attempts: {issue.attempts}\n"
        f"\n"
        f"### Title\n{issue.title}\n"
        f"\n"
        f"### Description\n{issue.description}\n"
    )
